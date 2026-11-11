import hashlib
import os
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from stat import S_ISREG

from pydantic import ValidationError

from doc_pipeline.contracts.artifact import (
    BlockEvidence,
    ChunkEvidence,
    ExportedChunk,
    Manifest,
    canonical,
    chunk_digest,
    digest,
    jsonl,
)
from doc_pipeline.contracts.core import line_ends, line_window, merge_line_ranges
from doc_pipeline.contracts.document import Image
from doc_pipeline.parsed_content.selection import canonical_text

_REQUIRED = {"content.md", "blocks.jsonl", "provenance.jsonl"}
_SEALED_DIRECTORIES = {"assets", "native", "source"}
_DIRECTORY_FLAGS = os.O_DIRECTORY | os.O_NOFOLLOW | os.O_RDONLY


class ArtifactError(ValueError):
    pass


@contextmanager
def _open_file(root: Path, name: str):
    relative = PurePosixPath(name)
    if (not relative.parts or relative.is_absolute() or ".." in relative.parts or "\\" in name
            or relative.as_posix() != name):
        raise ArtifactError(f"invalid artifact path {name!r}")
    try:
        descriptor = os.open(root, _DIRECTORY_FLAGS)
        try:
            for component in relative.parts[:-1]:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            with os.fdopen(os.open(relative.name, flags, dir_fd=descriptor), "rb") as stream:
                if not S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ArtifactError(f"artifact path is not a file {name!r}")
                yield stream
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ArtifactError(f"cannot read artifact file {name!r}") from error


def _read(root: Path, name: str) -> bytes:
    with _open_file(root, name) as stream:
        return stream.read()


def _verified(root: Path, name: str, expected: str) -> bytes:
    data = _read(root, name)
    if digest(data) != expected:
        raise ArtifactError(f"artifact file checksum mismatch {name!r}")
    return data


def _validate_directory(descriptor: int, prefix: str, files: dict[str, str]) -> None:
    with os.scandir(descriptor) as entries:
        for entry in entries:
            name = f"{prefix}/{entry.name}"
            if entry.is_dir(follow_symlinks=False):
                child = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                try:
                    _validate_directory(child, name, files)
                finally:
                    os.close(child)
            elif name not in files or entry.is_symlink():
                raise ArtifactError("unexpected file in artifact directory")


def _decode_rows(data: bytes, model):
    try:
        rows = [model.model_validate_json(line, strict=True) for line in data.split(b"\n")[:-1]]
        if data != jsonl(row.model_dump(mode="json") for row in rows):
            raise ArtifactError("invalid canonical artifact rows")
        return rows
    except ValidationError as error:
        raise ArtifactError("invalid artifact row") from error


class Artifact:
    def __init__(self, root: Path, manifest: Manifest, rows: list[ExportedChunk]):
        self._root = root
        self._manifest = manifest
        self._rows = rows

    @property
    def manifest(self) -> Manifest:
        return self._manifest.model_copy(deep=True)

    @classmethod
    def open(cls, root: Path, expected_id: str | None = None) -> "Artifact":
        root = Path(os.path.abspath(root))
        raw = _read(root, "document.json")
        try:
            manifest = Manifest.model_validate_json(raw, strict=True)
        except ValidationError as error:
            raise ArtifactError("invalid artifact manifest") from error
        identity = manifest.model_dump(mode="json", exclude={"artifact_id"})
        if (raw != canonical(manifest.model_dump(mode="json")) + b"\n"
                or manifest.artifact_id != digest(canonical(identity))
                or expected_id is not None and manifest.artifact_id != expected_id):
            raise ArtifactError("artifact identity mismatch")
        rows = _decode_rows(_read(root, "chunks.jsonl"), ExportedChunk)
        if (manifest.chunk_count != len(rows)
                or any(row.artifact_id != manifest.artifact_id or row.id != i for i, row in enumerate(rows))
                or manifest.chunk_rows_sha256 != digest(jsonl(
                    row.model_dump(mode="json", exclude={"artifact_id"}) for row in rows
                ))):
            raise ArtifactError("artifact chunk identity mismatch")

        cls._validate_inventory(root, manifest)
        evidence = {}
        for name, checksum in manifest.files.items():
            if name in _REQUIRED:
                evidence[name] = _verified(root, name, checksum)
            else:
                with _open_file(root, name) as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                        raise ArtifactError(f"artifact file checksum mismatch {name!r}")
        artifact = cls(root, manifest, rows)
        artifact._validate_evidence(evidence)
        return artifact

    @staticmethod
    def _validate_inventory(root: Path, manifest: Manifest) -> None:
        refs = [manifest.source, *manifest.dependencies.values(), *(r.file for r in manifest.resources.values())]
        if (not _REQUIRED <= manifest.files.keys() or not manifest.source.path.startswith("source/")
                or any(name not in _REQUIRED and name.split("/", 1)[0] not in _SEALED_DIRECTORIES
                       for name in manifest.files)
                or any(manifest.files.get(ref.path) != ref.sha256 for ref in refs)
                or any(not key.startswith("assets/") or key != resource.file.path
                       for key, resource in manifest.resources.items())):
            raise ArtifactError("invalid artifact file inventory")
        try:
            descriptor = os.open(root, _DIRECTORY_FLAGS)
            try:
                for directory in _SEALED_DIRECTORIES:
                    try:
                        child = os.open(directory, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    except FileNotFoundError:
                        continue
                    try:
                        _validate_directory(child, directory, manifest.files)
                    finally:
                        os.close(child)
            finally:
                os.close(descriptor)
        except OSError as error:
            raise ArtifactError("invalid artifact directory") from error

    def _validate_evidence(self, files: dict[str, bytes]) -> None:
        blocks = _decode_rows(files["blocks.jsonl"], BlockEvidence)
        evidence = _decode_rows(files["provenance.jsonl"], ChunkEvidence)
        indexed = {row.block.id: row for row in blocks}
        if len(indexed) != len(blocks) or len(evidence) != len(self._rows):
            raise ArtifactError("invalid artifact evidence index")
        try:
            markdown = files["content.md"].decode()
        except UnicodeDecodeError as error:
            raise ArtifactError("invalid rendered text") from error
        starts = [0, *line_ends(markdown)]
        line_count = len(starts)
        for block in blocks:
            self._validate_block(block, len(markdown), line_count)

        for row, selection in zip(self._rows, evidence, strict=True):
            if selection.id != row.id:
                raise ArtifactError("invalid chunk evidence ordinal")
            self._validate_chunk(row, selection, indexed, len(markdown), line_count)

        self._markdown, self._line_starts = markdown, starts
        self._blocks, self._selections = indexed, evidence

    def _validate_block(self, row: BlockEvidence, length: int, line_count: int) -> None:
        block, mapping = row.block, row.mapping
        assets = [use.asset for use in row.resource_uses if use.asset is not None]
        if isinstance(block.payload, Image) and block.payload.asset is not None:
            assets.append(block.payload.asset)
        if (mapping.block_id != block.id or mapping.markdown_range.end > length
                or not mapping.first_line <= mapping.last_line_inclusive <= line_count
                or row.canonical_text != canonical_text(block)
                or any(use.owner_block_id != block.id or (
                    use.codepoints and use.codepoints.end > len(row.canonical_text)
                ) for use in row.resource_uses)
                or any(ref.path not in self._manifest.resources or self._manifest.resources[ref.path].file != ref
                       for ref in assets)
                or any(self._manifest.files.get(loc.source.path) != loc.source.sha256 for loc in block.locations)):
            raise ArtifactError("invalid block evidence reference")

    def _validate_chunk(self, row: ExportedChunk, evidence: ChunkEvidence,
                        blocks: dict[str, BlockEvidence], length: int, line_count: int) -> None:
        body, assets = [], []
        for selected in evidence.selections:
            block = blocks.get(selected.block_id)
            span = selected.block_text_range
            if (block is None or selected.kind != block.block.payload.kind or span.end > len(block.canonical_text)
                    or selected.markdown.codepoints.end > length
                    or any(key not in self._manifest.resources for key in selected.assets)):
                raise ArtifactError("invalid chunk evidence reference")
            fragment = block.canonical_text[span.start:span.end]
            if selected.role == "body":
                bounds = selected.body_range
                if bounds is None or bounds.end > len(row.body) or row.body[bounds.start:bounds.end] != fragment:
                    raise ArtifactError("invalid body evidence range")
                body.append(fragment)
                assets.extend(selected.assets)
            if selected.input_range is not None:
                bounds = selected.input_range
                if row.embedding_input is None or bounds.end > len(row.embedding_input) or (
                    row.embedding_input[bounds.start:bounds.end] != fragment
                ):
                    raise ArtifactError("invalid embedding evidence range")
        identity = self._manifest.source_identity
        if ("\n\n".join(body) != row.body or list(dict.fromkeys(assets)) != row.assets
                or row.metadata.chunk_sha256 != chunk_digest(row.body, row.heading_path, row.embedding_input,
                    [self._manifest.resources[key].file.sha256 for key in assets])
                or row.metadata.source_sha256 != self._manifest.source.sha256
                or row.metadata.source_type != self._manifest.source_type
                or row.metadata.document_id != identity.document_id
                or row.metadata.document_version != identity.version
                or row.citations.source_uri != identity.uri or row.citations.source_name != identity.name):
            raise ArtifactError("chunk evidence or source mismatch")
        if any(not 1 <= first <= last <= line_count for first, last in row.citations.content_ranges):
            raise ArtifactError("invalid context range")

    def _file(self, path: str) -> bytes:
        return _verified(self._root, path, self._manifest.files[path])

    def chunk(self, ordinal: int) -> ExportedChunk:
        if type(ordinal) is not int or not 0 <= ordinal < len(self._rows):
            raise IndexError("chunk ordinal outside artifact")
        return self._rows[ordinal].model_copy(deep=True)

    def neighbors(self, ordinal: int, radius: int = 1) -> list[ExportedChunk]:
        self.chunk(ordinal)
        if type(radius) is not int or radius < 0:
            raise ValueError("radius must be a nonnegative integer")
        first, stop = max(0, ordinal - radius), min(len(self._rows), ordinal + radius + 1)
        return [self.chunk(i) for i in range(first, stop) if i != ordinal]

    def content(self) -> str:
        return self._markdown

    def context(self, ordinal: int, *, before: int = 0, after: int = 0) -> list[str]:
        if any(type(value) is not int or value < 0 for value in (before, after)):
            raise ValueError("context expansion must use nonnegative line counts")
        ranges = self.chunk(ordinal).citations.content_ranges
        starts = self._line_starts
        windows = merge_line_ranges([(max(1, first - before), min(len(starts), last + after))
                                     for first, last in ranges])
        return [line_window(self._markdown, starts, first, last) for first, last in windows]

    def evidence(self, ordinal: int) -> tuple[ChunkEvidence, list[BlockEvidence]]:
        self.chunk(ordinal)
        selection = self._selections[ordinal]
        ids = dict.fromkeys(entry.block_id for entry in selection.selections)
        return selection.model_copy(deep=True), [self._blocks[key].model_copy(deep=True) for key in ids]

    def source(self) -> bytes:
        return self._file(self._manifest.source.path)

    def asset(self, key: str) -> bytes:
        return self._file(self._manifest.resources[key].file.path)
