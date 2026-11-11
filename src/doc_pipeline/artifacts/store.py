import errno
import hashlib
import os
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from fcntl import LOCK_EX, LOCK_NB, flock
from pathlib import Path
from stat import S_IMODE, S_ISREG

from doc_pipeline import __version__
from doc_pipeline.chunking.citations import export_chunks
from doc_pipeline.contracts.artifact import (
    BlockEvidence,
    ChunkEvidence,
    Manifest,
    ManifestIdentity,
    Resource,
    canonical,
    digest,
    jsonl,
)
from doc_pipeline.contracts.chunk import Chunk
from doc_pipeline.contracts.config import Config, Limits
from doc_pipeline.contracts.core import FileRef, InputFile, PipelineError, SourceIdentity
from doc_pipeline.contracts.document import Image, MarkdownMapping, ParsedContent
from doc_pipeline.parsed_content.selection import canonical_text
from doc_pipeline.services.io import file_digest, write_bytes

_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff", ".ico": "image/x-icon",
    ".jp2": "image/jp2", ".svg": "image/svg+xml",
}


@dataclass(frozen=True)
class CapturedInput:
    input: InputFile
    original_path: Path
    ref: FileRef
    identity: SourceIdentity
    document_id_origin: str
    dependencies: dict[str, FileRef]


@contextmanager
def empty_destination(destination: Path):
    destination = destination.resolve()
    try:
        destination.mkdir(parents=True)
        created = True
    except FileExistsError:
        created = False
    descriptor = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    acquired = False
    try:
        try:
            flock(descriptor, LOCK_EX | LOCK_NB)
        except BlockingIOError as error:
            raise PipelineError("intake", "output_in_use") from error
        acquired = True
        if any(destination.iterdir()):
            raise PipelineError("intake", "output_not_empty")
        yield destination
    finally:
        if created and acquired:
            try:
                current, opened = destination.stat(), os.fstat(descriptor)
                if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                    destination.rmdir()
            except OSError:
                pass
        os.close(descriptor)


@contextmanager
def staging_directory(destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent = destination if destination.is_dir() else destination.parent
    try:
        temporary = tempfile.TemporaryDirectory(prefix=".doc-pipeline-", dir=parent)
    except PermissionError:
        temporary = tempfile.TemporaryDirectory(prefix=".doc-pipeline-", dir=destination)
    with temporary as directory:
        yield Path(directory)


def _capture(path: Path, target: Path, limit: int, error_code: str) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    checksum = hashlib.sha256()
    flags = os.O_RDONLY | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as source, target.open("xb") as snapshot:
        info = os.fstat(source.fileno())
        if not S_ISREG(info.st_mode):
            raise PipelineError("intake", "invalid_input_file")
        if info.st_size > limit:
            raise PipelineError("intake", error_code)
        remaining = limit
        while data := source.read(min(1024 * 1024, remaining + 1)):
            remaining -= len(data)
            if remaining < 0:
                raise PipelineError("intake", error_code)
            checksum.update(data)
            snapshot.write(data)
    return checksum.hexdigest()


def capture_source(source: InputFile, staging: Path, limits: Limits) -> CapturedInput:
    path = source.path
    if not path.name or "\\" in path.name or path.name in {".", ".."}:
        raise PipelineError("intake", "invalid_filename")
    original_path = path.resolve()
    original = staging / ".capture" / path.name
    relative = f"source/original{path.suffix.lower()}"
    ref = FileRef(path=relative, sha256=_capture(original_path, original, limits.max_source_bytes, "source_too_large"))
    identity = source.identity
    if identity is None:
        identity = SourceIdentity(document_id=str(uuid.uuid5(uuid.NAMESPACE_URL, original_path.as_uri())))
    identity = SourceIdentity(
        document_id=identity.document_id, version=identity.version,
        uri=identity.uri or relative, name=identity.name or path.name,
    )
    assets, dependencies = {}, {}
    for index, (key, asset) in enumerate(sorted(source.assets.items())):
        relative_asset = f"source/dependencies/{index:04d}"
        target = staging / relative_asset
        checksum = _capture(asset, target, limits.max_source_bytes, "asset_too_large")
        assets[key] = target
        dependencies[key] = FileRef(path=relative_asset, sha256=checksum)
    return CapturedInput(source.model_copy(update={"path": original, "assets": assets}), original_path,
                         ref, identity, "supplied" if source.identity else "path", dependencies)


def retain_archive(archive: Path, staging: Path, limits: Limits) -> Path:
    target = staging / "native/mineru.zip"
    _capture(archive, target, limits.max_zip_bytes, "archive_too_large")
    return target


def _finalize_resources(parsed: ParsedContent, staging: Path) -> dict[str, Resource]:
    refs = {
        use.asset.path: use.asset for use in parsed.inline_assets if use.asset is not None
    }
    refs.update({
        block.payload.asset.path: block.payload.asset for block in parsed.blocks
        if isinstance(block.payload, Image) and block.payload.asset is not None
    })
    resources = {}
    for key, ref in refs.items():
        if file_digest(staging / ref.path) != ref.sha256:
            raise PipelineError("write", "resource_digest_mismatch")
        resources[key] = Resource(file=ref, media_type=_MEDIA_TYPES[Path(ref.path).suffix])
    for candidate in (staging / "assets").glob("*"):
        if candidate.relative_to(staging).as_posix() not in refs:
            candidate.unlink()
    return resources


def _install(staging: Path, destination: Path) -> None:
    if staging.parent != destination:
        mode = S_IMODE(destination.stat().st_mode)
        try:
            staging.rename(destination)
        except OSError as error:
            if error.errno not in {errno.EBUSY, errno.EXDEV}:
                raise
        else:
            destination.chmod(mode)
            return
    # A mounted output can reject directory replacement with EBUSY or EXDEV.
    installed = []
    try:
        for path in staging.iterdir():
            if path.name == "document.json":
                continue
            target = destination / path.name
            if target.exists():
                raise PipelineError("write", "output_not_empty")
            installed.append(target)
            shutil.move(path, target)
        manifest = destination / "document.json"
        if manifest.exists():
            raise PipelineError("write", "output_not_empty")
        temporary = destination / ".document.json.tmp"
        installed.extend([manifest, temporary])
        write_bytes(temporary, (staging / "document.json").read_bytes())
        temporary.rename(manifest)
    except BaseException:
        for target in reversed(installed):
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        raise


def publish(staging: Path, destination: Path, captured: CapturedInput, source_type: str,
            parsed: ParsedContent, markdown: str, mappings: list[MarkdownMapping], chunks: list[Chunk],
            config: Config, *, remote: bool) -> Manifest:
    source_path = staging / captured.ref.path
    source_path.parent.mkdir(parents=True, exist_ok=True)
    captured.input.path.rename(source_path)
    captured.input.path.parent.rmdir()
    resources = _finalize_resources(parsed, staging)
    uses = {}
    for use in parsed.inline_assets:
        uses.setdefault(use.owner_block_id, []).append(use)
    write_bytes(staging / "content.md", markdown.encode())
    write_bytes(staging / "blocks.jsonl", jsonl(
        BlockEvidence(block=block, canonical_text=canonical_text(block), mapping=mapping,
                      resource_uses=uses.get(block.id, [])).model_dump(mode="json")
        for block, mapping in zip(parsed.blocks, mappings, strict=True)
    ))
    write_bytes(staging / "provenance.jsonl", jsonl(
        ChunkEvidence(id=chunk.id, selections=chunk.citations).model_dump(mode="json") for chunk in chunks
    ))
    rows = [chunk.model_dump(mode="json") for chunk in export_chunks(
        chunks, parsed, source=captured.ref, identity=captured.identity, source_type=source_type, resources=resources,
    )]
    files = {path.relative_to(staging).as_posix(): file_digest(path)
             for path in staging.rglob("*") if path.is_file()}
    identity = ManifestIdentity(
        producer=f"multimodal-doc-pipeline/{__version__}", source=captured.ref,
        source_identity=captured.identity, document_id_origin=captured.document_id_origin, source_type=source_type,
        input_encoding=captured.input.encoding, dependencies=captured.dependencies,
        parse_options=config.mineru.parse_options() if remote else {}, native_profile=parsed.native_profile,
        chunking=config.chunking, parse_status=parsed.parse_status, pdf_pages=parsed.pdf_pages,
        diagnostics=parsed.diagnostics, resources=resources, native_members=parsed.native_members,
        files=files, chunk_count=len(rows), chunk_rows_sha256=digest(jsonl(rows)),
    ).model_dump(mode="json")
    artifact_id = digest(canonical(identity))
    manifest = Manifest.model_validate({**identity, "artifact_id": artifact_id})
    write_bytes(staging / "chunks.jsonl", jsonl({**row, "artifact_id": artifact_id} for row in rows))
    write_bytes(staging / "document.json", canonical(manifest.model_dump(mode="json")) + b"\n")
    _install(staging, destination)
    return manifest
