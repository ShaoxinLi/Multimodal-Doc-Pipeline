import re
import tempfile
import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import httpx
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from doc_pipeline.chunking.builder import make_chunks
from doc_pipeline.chunking.citations import digest, export_chunks
from doc_pipeline.contracts.config import Config, Limits
from doc_pipeline.contracts.core import FileRef, InputFile, PipelineError, SourceIdentity
from doc_pipeline.contracts.document import Image as DocumentImage
from doc_pipeline.contracts.document import ParsedContent
from doc_pipeline.contracts.run import (
    EmbeddingUsage,
    ExecutionRecord,
    ProgressCallback,
    ProgressEvent,
    RunClock,
)
from doc_pipeline.parsed_content.md_renderer import render_markdown
from doc_pipeline.parsers.multimodal.client import ArchiveFailure, ArchiveSuccess, obtain_archives
from doc_pipeline.services.embeddings import embed_chunks
from doc_pipeline.services.io import (
    capture_file,
    empty_destination,
    file_digest,
    install_output,
    staging_directory,
    write_bytes,
    write_json,
    write_jsonl,
)

TEXT_FORMATS = {"md", "markdown", "txt", "rst", "tex", "latex"}
IMAGE_FORMATS = {"png", "jpg", "jpeg", "jp2", "webp", "gif", "bmp"}
OFFICE_MEMBERS = {"docx": "word/document.xml", "pptx": "ppt/presentation.xml", "xlsx": "xl/workbook.xml"}
REMOTE_FORMATS = {"pdf", "doc", "ppt", "xls"} | OFFICE_MEMBERS.keys() | IMAGE_FORMATS


def validate_source(path: Path, filename: str, limits: Limits) -> str:
    if Path(filename).name != filename or "\\" in filename or not filename:
        raise PipelineError("intake", "invalid_filename")
    kind = Path(filename).suffix.lower().lstrip(".")
    if kind not in TEXT_FORMATS | REMOTE_FORMATS:
        raise PipelineError("intake", "unsupported_format")
    if path.stat().st_size > limits.max_source_bytes:
        raise PipelineError("intake", "source_too_large")
    with path.open("rb") as stream:
        head = stream.read(8192)
    if re.match(rb"\s*(?:<!doctype\s+html\b|<html(?:\s|>))", head.removeprefix(b"\xef\xbb\xbf"), re.I):
        raise PipelineError("intake", "unsupported_html")
    pdf = b"%PDF-" in head[:1024]
    ole = head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    zipped = head.startswith(b"PK\x03\x04")
    image_magic = head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"BM", b"\x00\x00\x00\x0cjP"))
    try:
        if kind in TEXT_FORMATS:
            if pdf or ole or zipped or image_magic:
                raise PipelineError("intake", "format_mismatch")
        elif kind == "pdf":
            if not pdf:
                raise PipelineError("intake", "format_mismatch")
            reader = PdfReader(path, strict=False)
            if reader.is_encrypted:
                raise PipelineError("intake", "encrypted_source")
            if len(reader.pages) > limits.max_pdf_pages:
                raise PipelineError("intake", "page_limit_exceeded")
        elif kind in OFFICE_MEMBERS:
            if not zipped:
                raise PipelineError("intake", "format_mismatch")
            with ZipFile(path) as archive:
                names = archive.namelist()
                if OFFICE_MEMBERS[kind] not in names or "[Content_Types].xml" not in names:
                    raise PipelineError("intake", "format_mismatch")
        elif kind in {"doc", "ppt", "xls"}:
            if not ole:
                raise PipelineError("intake", "format_mismatch")
        else:
            with Image.open(path) as image:
                expected = {"jpg": "JPEG", "jpeg": "JPEG", "jp2": "JPEG2000"}.get(kind, kind.upper())
                if image.format != expected:
                    raise PipelineError("intake", "format_mismatch")
                image.verify()
    except (BadZipFile, PdfReadError, UnidentifiedImageError, ValueError, Image.DecompressionBombError) as exc:
        raise PipelineError("intake", "corrupt_source") from exc
    return kind


@dataclass(frozen=True)
class _CapturedInput:
    input: InputFile
    original_path: Path
    ref: FileRef
    identity: SourceIdentity


def _capture_source(source: InputFile, directory: Path, limits: Limits) -> _CapturedInput:
    path = source.path
    if not path.name or "\\" in path.name or path.name in {".", ".."}:
        raise PipelineError("intake", "invalid_filename")
    original_path = path.resolve()
    original = directory / "source" / path.name
    checksum = capture_file(original_path, original, limits.max_source_bytes, "source_too_large")
    ref = FileRef(path=str(original_path), sha256=checksum)
    identity = source.identity or SourceIdentity(document_id=str(uuid.uuid5(uuid.NAMESPACE_URL, original_path.as_uri())))
    identity = SourceIdentity(
        document_id=identity.document_id, version=identity.version,
        uri=identity.uri or original_path.as_uri(), name=identity.name or path.name,
    )
    assets = {}
    for index, (key, asset) in enumerate(sorted(source.assets.items())):
        target = directory / "dependencies" / str(index)
        capture_file(asset, target, limits.max_source_bytes, "asset_too_large")
        assets[key] = target
    return _CapturedInput(source.model_copy(update={"path": original, "assets": assets}), original_path, ref, identity)


@dataclass(frozen=True)
class _Document:
    captured: _CapturedInput
    kind: str
    output_dir: Path
    staging: Path
    preserve_directory: bool
    record: ExecutionRecord
    output_root: Path


def _intake(source: InputFile, output_dir: Path, limits: Limits, record: ExecutionRecord,
            scope: ExitStack, *, output_root: Path | None = None) -> _Document:
    record.progress("intake")
    with record.measure("intake"):
        output_dir = output_dir.resolve()
        existed = output_dir.exists()
        if existed:
            scope.enter_context(empty_destination(output_dir))
        work = scope.enter_context(staging_directory(output_dir))
        staging = work / "out"
        staging.mkdir()
        captured = _capture_source(source, work / "capture", limits)
        kind = validate_source(captured.input.path, source.path.name, limits)
        if not existed:
            scope.enter_context(empty_destination(output_dir))
    return _Document(captured, kind, output_dir, staging, existed, record, output_root or output_dir)


def _remote_progress(documents: list[_Document], callback: ProgressCallback | None):
    if callback is None:
        return None
    paths = {document.record.index: document.record.path for document in documents}

    def emit(event: ProgressEvent) -> None:
        callback(ProgressEvent(event.index, paths[event.index], event.stage, event.completed, event.total, event.failed))

    return emit


def _finalize(document: _Document, config: Config, client: httpx.Client,
              limits: Limits, archive: Path | None = None) -> dict:
    output_dir, record = document.output_dir, document.record
    captured, staging = document.captured, document.staging
    record.progress("parse")
    with record.measure("parse"):
        if archive is None:
            from doc_pipeline.parsers.text.parser import parse_text_document

            parsed = parse_text_document(captured.input, captured.ref, staging, source_dir=captured.original_path.parent,
                                         max_asset_bytes=limits.max_source_bytes, output_root=document.output_root)
        else:
            from doc_pipeline.parsers.multimodal.parser import decode_archive

            parsed = decode_archive(captured.ref, archive, staging, limits, source_type=document.kind,
                                    original_path=captured.input.path)
        record.parse_status = parsed.parse_status
    with record.measure("render"):
        markdown, mappings = render_markdown(parsed)
    with record.measure("chunk"):
        chunks = make_chunks(parsed, mappings, config)
        record.chunk_count = len(chunks)
    with record.measure("write"):
        asset_digests = _finalize_assets(parsed, staging)
        write_bytes(staging / "content.md", markdown.encode())
        write_jsonl(staging / "chunks.jsonl", (chunk.model_dump(mode="json") for chunk in export_chunks(
            chunks, parsed, source=captured.ref, identity=captured.identity,
            source_type=document.kind, asset_digests=asset_digests,
        )))
    vectors, errors = [], []
    if config.embedding is not None:
        with record.measure("embedding"):
            vectors, errors, record.embedding_usage = embed_chunks(
                chunks, config, client, on_progress=partial(record.progress, "embedding"),
            )
            profile = config.embedding
            for vector in vectors:
                chunk = chunks[vector["chunk_id"]]
                vector.update(
                    input_sha256=digest(chunk.embedding_input.encode()),
                    profile={
                        "model": profile.model, "dimensions": len(vector["vector"]),
                        "requested_dimensions": profile.dimensions, "encoding_format": "float",
                        "endpoint_sha256": digest(profile.api_base_url.encode()),
                    },
                )
            record.embedding_count = len(vectors)
            record.embedding_error_count = len(errors)
    with record.measure("write"):
        write_jsonl(staging / "embeddings.jsonl", vectors)
    report = {
        "source": {"path": str(captured.original_path), "sha256": captured.ref.sha256},
        "parse_status": parsed.parse_status,
        "chunk_count": record.chunk_count,
        "chunking": config.chunking.model_dump(),
        "embedding": config.embedding.model_dump(include={"model", "dimensions"}) if config.embedding else None,
        "embedding_count": record.embedding_count,
        "embedding_errors": errors,
        "diagnostics": parsed.diagnostics,
    }
    record.finish()
    report["execution"] = record.snapshot(config.mineru.parse_options())
    with record.measure("write"):
        _write_record(staging / "report.json", report)
        install_output(staging, output_dir, preserve_directory=document.preserve_directory)
    return report


def _finalize_assets(parsed: ParsedContent, staging: Path) -> dict[str, str]:
    refs = {
        use.asset.path: use.asset for use in parsed.inline_assets if use.asset is not None
    }
    refs.update({
        block.payload.asset.path: block.payload.asset for block in parsed.blocks
        if isinstance(block.payload, DocumentImage) and block.payload.asset is not None
    })
    asset_digests = {}
    for key, ref in refs.items():
        if file_digest(staging / ref.path) != ref.sha256:
            raise PipelineError("write", "resource_digest_mismatch")
        asset_digests[key] = ref.sha256
    for candidate in (staging / "assets").glob("*"):
        if candidate.relative_to(staging).as_posix() not in refs:
            candidate.unlink()
    return asset_digests


def _write_record(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        write_json(temporary, value)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _http_client(client: httpx.Client | None, config: Config):
    if client is not None:
        return nullcontext(client)
    return httpx.Client(timeout=config.request_timeout_seconds, follow_redirects=False)


def _finish_remote(document: _Document, outcome: ArchiveSuccess | ArchiveFailure, config: Config,
                   client: httpx.Client, limits: Limits) -> dict:
    record = document.record
    record.mineru = outcome.observation
    record.stage_seconds["remote_acquisition"] = outcome.elapsed_seconds
    if isinstance(outcome, ArchiveFailure):
        record.failed_stage = "remote_acquisition"
        record.fail(outcome.error, at=outcome.finished)
        raise outcome.error
    return _finalize(document, config, client, limits, outcome.path)


def process_file(path: Path, output_dir: Path, config: Config | None = None, *,
                 assets: dict[str, Path] | None = None, encoding: str = "utf-8",
                 identity: SourceIdentity | None = None, client: httpx.Client | None = None,
                 on_progress: ProgressCallback | None = None) -> dict:
    config = config or Config()
    limits = Limits()
    record = ExecutionRecord(RunClock(), 0, path=path, on_progress=on_progress)
    try:
        with ExitStack() as scope:
            source = InputFile(path=path, assets=assets or {}, encoding=encoding, identity=identity)
            document = _intake(source, output_dir, limits, record, scope)
            http = scope.enter_context(_http_client(client, config))
            if document.kind in TEXT_FORMATS:
                report = _finalize(document, config, http, limits)
            else:
                with tempfile.TemporaryDirectory(prefix="doc-pipeline-") as temporary:
                    [outcome] = obtain_archives(
                        [(0, document.captured.input)], config, http, Path(temporary),
                        on_progress=_remote_progress([document], on_progress),
                    )
                    report = _finish_remote(document, outcome, config, http, limits)
    except (PipelineError, OSError, ValueError) as error:
        record.fail(error)
        record.log()
        record.progress(record.status)
        raise
    record.log()
    record.progress(record.status)
    return report


def _process_document(document: _Document, config: Config, client: httpx.Client, limits: Limits,
                      outcome: ArchiveSuccess | ArchiveFailure | None = None) -> dict | None:
    try:
        if outcome is None:
            report = _finalize(document, config, client, limits)
        else:
            report = _finish_remote(document, outcome, config, client, limits)
    except (PipelineError, OSError, ValueError) as error:
        document.record.fail(error)
        report = None
    document.record.progress(document.record.status)
    return report


def process_files(paths: Iterable[Path | InputFile], output_dir: Path, config: Config | None = None, *,
                  client: httpx.Client | None = None,
                  on_progress: ProgressCallback | None = None) -> dict:
    config = config or Config()
    inputs = list(paths)
    if not inputs:
        raise PipelineError("intake", "no_input_files")
    clock = RunClock()
    with ExitStack() as scope:
        output_dir = scope.enter_context(empty_destination(output_dir))
        return _process_batch(inputs, output_dir, config, client, on_progress, clock, scope)


def _process_batch(inputs, output_dir, config, client, on_progress, clock, scope) -> dict:
    limits = Limits()
    files: dict[int, dict] = {}
    local: list[_Document] = []
    remote: list[_Document] = []
    aggregate_usage = EmbeddingUsage() if config.embedding is not None else None

    def collect(record: ExecutionRecord, path: Path, report: dict | None = None) -> None:
        record.log()
        files[record.index] = {
            "index": record.index,
            "input": str(path),
            "report": f"{record.index + 1:04d}/report.json" if report else None,
            **(report["execution"] if report else record.snapshot(config.mineru.parse_options())),
        }
        if aggregate_usage is not None and record.embedding_usage is not None:
            aggregate_usage.add(record.embedding_usage)

    for index, item in enumerate(inputs):
        path = item.path if isinstance(item, InputFile) else item
        record = ExecutionRecord(RunClock(run_id=clock.run_id), index, path=path, on_progress=on_progress)
        try:
            source = item if isinstance(item, InputFile) else InputFile(path=item)
            document = _intake(source, output_dir / f"{index + 1:04d}", limits, record, scope,
                               output_root=output_dir)
        except (PipelineError, OSError, ValueError) as error:
            record.fail(error)
            record.progress(record.status)
            collect(record, path)
            continue
        documents = local if document.kind in TEXT_FORMATS else remote
        documents.append(document)

    with _http_client(client, config) as http:
        pool = ThreadPoolExecutor(max_workers=config.local_workers)
        try:
            local_futures = [pool.submit(_process_document, document, config, http, limits) for document in local]
            size = config.mineru.batch_size
            for start in range(0, len(remote), size):
                group = remote[start:start + size]
                with tempfile.TemporaryDirectory(prefix="doc-pipeline-") as temporary:
                    outcomes = obtain_archives([(item.record.index, item.captured.input) for item in group],
                                               config, http, Path(temporary),
                                               on_progress=_remote_progress(group, on_progress))
                    for document, outcome in zip(group, outcomes, strict=True):
                        report = _process_document(document, config, http, limits, outcome)
                        collect(document.record, document.record.path, report)
            for document, future in zip(local, local_futures, strict=True):
                collect(document.record, document.record.path, future.result())
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    ordered = [files[index] for index in range(len(inputs))]
    counts = {
        "file_count": len(ordered),
        **{f"{status}_file_count": sum(item["status"] == status for item in ordered)
           for status in ("complete", "partial", "failed")},
        **{key: sum(item[key] for item in ordered)
           for key in ("chunk_count", "embedding_count", "embedding_error_count")},
    }
    status = "complete"
    if counts["failed_file_count"] == len(ordered):
        status = "failed"
    elif counts["failed_file_count"] or counts["partial_file_count"]:
        status = "partial"
    aggregate = {**clock.snapshot(), "status": status, **counts, "files": ordered}
    if aggregate_usage is not None:
        aggregate["embedding_usage"] = aggregate_usage.snapshot()
    _write_record(output_dir / "run.json", aggregate)
    return aggregate
