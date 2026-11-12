import logging
import re
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, nullcontext
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit
from zipfile import BadZipFile, ZipFile

import httpx
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from doc_pipeline.chunking.builder import make_chunks
from doc_pipeline.chunking.citations import digest, export_chunks
from doc_pipeline.contracts.chunk import Chunk, ExportedChunk
from doc_pipeline.contracts.config import Config, Limits
from doc_pipeline.contracts.core import (
    FileRef,
    InputFile,
    PagePosition,
    PdfRegion,
    PipelineError,
    SourceIdentity,
    TextSpan,
    UrlInput,
)
from doc_pipeline.contracts.document import Image as DocumentImage
from doc_pipeline.contracts.document import MarkdownMapping, Opaque, ParsedContent
from doc_pipeline.contracts.observations import EmbeddingUsage, MinerURequests
from doc_pipeline.contracts.run import (
    DOCUMENT,
    NA,
    ExecutionRecord,
    Issues,
    IssueTarget,
    ProgressCallback,
    RunClock,
    RunInstant,
)
from doc_pipeline.contracts.run import (
    NATIVE_FORMATS as TEXT_FORMATS,
)
from doc_pipeline.parsed_content.md_renderer import render_markdown
from doc_pipeline.parsers.multimodal.archive import open_validated_archive
from doc_pipeline.parsers.multimodal.client import ArchiveFailure, ArchiveSuccess, obtain_archives
from doc_pipeline.services.embeddings import embed_chunks
from doc_pipeline.services.io import (
    capture_bytes,
    capture_file,
    capture_url,
    empty_destination,
    file_digest,
    install_output,
    json_bytes,
    provider_reason,
    source_url,
    staging_directory,
    write_bytes,
    write_jsonl,
)

IMAGE_FORMATS = {"png", "jpg", "jpeg", "jp2", "webp", "gif", "bmp"}
OFFICE_MEMBERS = {"docx": "word/document.xml", "pptx": "ppt/presentation.xml", "xlsx": "xl/workbook.xml"}
REMOTE_FORMATS = {"pdf", "doc", "ppt", "xls"} | OFFICE_MEMBERS.keys() | IMAGE_FORMATS


def _asset_target(reference: str) -> IssueTarget | None:
    if not reference:
        return None
    reference = unquote(reference)
    if reference.lower().startswith("data:"):
        return None
    try:
        uri = urlsplit(reference)
        reference = urlunsplit((uri.scheme, uri.netloc.rsplit("@", 1)[-1], uri.path, "", ""))
    except ValueError:
        return None
    return IssueTarget("asset", (reference,)) if reference else None


def _locate_issues(issues: Issues, mappings: list[MarkdownMapping], chunks: list[Chunk]) -> None:
    body_chunks = defaultdict(set)
    for chunk in chunks:
        for citation in chunk.citations:
            if citation.role == "body":
                body_chunks[citation.block_id].add(chunk.id)
    issues.block_targets = {
        mapping.block_id: IssueTarget("content_line", tuple(range(mapping.first_line, mapping.last_line_inclusive + 1)))
        for mapping in mappings if mapping.markdown_range.start != mapping.markdown_range.end
    }
    issues.block_targets.update({block_id: IssueTarget("chunk", tuple(sorted(ids)))
                                 for block_id, ids in body_chunks.items()})


def _parsed_issues(issues: Issues, parsed: ParsedContent, source: FileRef | None) -> None:
    for block in parsed.blocks:
        lines, pages = set(), set()
        for location in block.locations:
            if location.source != source:
                continue
            if isinstance(location, TextSpan):
                lines.update(range(location.first_line, location.last_line_inclusive + 1))
            elif isinstance(location, PagePosition):
                pages.add(location.number)
            elif isinstance(location, PdfRegion):
                pages.add(location.page_index + 1)
        if lines:
            issues.source_targets[block.id] = IssueTarget("source_line", tuple(sorted(lines)))
        elif pages:
            issues.source_targets[block.id] = IssueTarget("source_page", tuple(sorted(pages)))
    for diagnostic in parsed.diagnostics:
        target = None
        if diagnostic.source_line is not None:
            target = IssueTarget("source_line", (diagnostic.source_line,))
        elif diagnostic.source_page is not None:
            target = IssueTarget("source_page", (diagnostic.source_page,))
        elif diagnostic.asset is not None:
            target = _asset_target(diagnostic.asset)
        elif diagnostic.block_id is not None:
            target = IssueTarget("block", (diagnostic.block_id,))
        elif diagnostic.document_wide:
            target = DOCUMENT
        issues.add("parse", diagnostic.code, provider_reason(diagnostic.reason),
                   diagnostic.action, diagnostic.retryable, target)
    for block in parsed.blocks:
        target = IssueTarget("block", (block.id,))
        if isinstance(block.payload, Opaque):
            issues.add("parse", "opaque_source", block.payload.reason, "retained_opaque", target=target)
        if block.location_error:
            issues.add("parse", "source_location_unresolved", block.location_error, "unverified", target=target)
    inline = {(use.owner_block_id, use.original_reference) for use in parsed.inline_assets}
    assets = [(use.original_reference, use.resolution) for use in parsed.inline_assets]
    assets.extend((block.payload.original_reference, block.payload.resolution) for block in parsed.blocks
                  if isinstance(block.payload, DocumentImage) and (block.id, block.payload.original_reference) not in inline)
    for reference, resolution in assets:
        if resolution != "archived":
            issues.add("parse", "asset_unavailable", resolution, "unarchived",
                       target=_asset_target(reference))


def _result_summary(chunks: list[ExportedChunk], vectors: list[dict], errors: list[dict], *, embedding: bool) -> dict:
    content = dict.fromkeys(("parsed", "partial", "unparsed"), 0)
    locations = dict.fromkeys(("known", "partial", "unavailable"), 0)
    for chunk in chunks:
        content[chunk.metadata.content_status] += 1
        locations[chunk.citations.location_status] += 1
    return {
        "chunks": content, "locations": locations,
        "embedding": {
            "succeeded": len(vectors), "failed": len(errors),
            "ineligible": sum(chunk.embedding_input is None for chunk in chunks),
        } if embedding else NA,
    }


def _source_kind(filename: str) -> str:
    if Path(filename).name != filename or "\\" in filename:
        raise PipelineError("intake", "invalid_filename")
    kind = Path(filename).suffix.lower().lstrip(".")
    if kind not in TEXT_FORMATS | REMOTE_FORMATS:
        raise PipelineError("intake", "unsupported_format")
    return kind


def validate_source(path: Path, filename: str, limits: Limits) -> str:
    kind = _source_kind(filename)
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
    original_path: Path | None
    ref: FileRef


def _capture_assets(source: InputFile | UrlInput, directory: Path, limits: Limits) -> dict[str, Path]:
    assets = {}
    for index, (key, asset) in enumerate(sorted(source.assets.items())):
        target = directory / str(index)
        capture_file(asset, target, limits.max_source_bytes, "asset_too_large")
        assets[key] = target
    return assets


def _capture_source(source: InputFile, directory: Path, limits: Limits, record: ExecutionRecord) -> _CapturedInput:
    path = source.path
    if not path.name or "\\" in path.name or path.name in {".", ".."}:
        raise PipelineError("intake", "invalid_filename")
    original_path = path.resolve()
    record.local_target = original_path
    original = directory / "source" / path.name
    checksum = capture_file(original_path, original, limits.max_source_bytes, "source_too_large")
    record.sha256, record.source_bytes = checksum, original.stat().st_size
    ref = FileRef(path=str(original_path), sha256=checksum)
    assets = _capture_assets(source, directory / "dependencies", limits)
    return _CapturedInput(source.model_copy(update={"path": original, "assets": assets}), original_path, ref)


@dataclass(frozen=True)
class _Document:
    captured: _CapturedInput | None
    kind: str
    output_dir: Path
    staging: Path
    record: ExecutionRecord
    output_root: Path


def _intake(source: InputFile | UrlInput, output_dir: Path, limits: Limits, record: ExecutionRecord,
            scope: ExitStack, *, output_root: Path | None = None) -> _Document:
    record.progress("intake")
    output_dir = scope.enter_context(empty_destination(output_dir))
    record.on_checkpoint = _ReportWriter(output_dir / "report.json")
    with record.measure("intake"):
        work = scope.enter_context(staging_directory(output_dir))
        staging = work / "out"
        staging.mkdir()
        if isinstance(source, UrlInput):
            source_url(source.request_url, "intake")
            kind = _source_kind(source.filename)
            captured = None
        else:
            captured = _capture_source(source, work / "capture", limits, record)
            kind = validate_source(captured.input.path, source.path.name, limits)
    return _Document(captured, kind, output_dir, staging, record, output_root or output_dir)


def _capture_native_url(source: UrlInput, document: _Document, limits: Limits,
                        client: httpx.Client | None, timeout: float) -> _CapturedInput:
    staging, record = document.staging, document.record
    original = staging / "source" / source.filename
    record.progress("source_download")
    with record.measure("acquire"):
        checksum = capture_url(source.request_url, original, limits.max_source_bytes, timeout, client)
        record.sha256, record.source_bytes = checksum, original.stat().st_size
        validate_source(original, source.filename, limits)
        assets = _capture_assets(source, staging.parent / "dependencies", limits)
    ref = FileRef(path=original.relative_to(staging).as_posix(), sha256=checksum)
    return _CapturedInput(InputFile(path=original, encoding=source.encoding, assets=assets), None, ref)


@dataclass(frozen=True)
class _ArchiveOrigin:
    path: Path
    ref: FileRef
    member: str


def _capture_archive_origin(archive: ZipFile, document: _Document, limits: Limits) -> _ArchiveOrigin | None:
    record, staging = document.record, document.staging
    exact, converted = [], []
    for member in archive.infolist():
        path = Path(member.filename)
        kind = path.suffix.lower().lstrip(".")
        if not member.is_dir() and path.stem.endswith("_origin") and kind in REMOTE_FORMATS:
            (exact if kind == document.kind else converted).append((member, kind))

    for candidates in (exact, converted):
        chosen = None
        for member, kind in candidates:
            if member.file_size > limits.max_source_bytes:
                continue
            candidate = staging.parent / f"candidate.{kind}"
            with archive.open(member) as stream, candidate.open("xb") as snapshot:
                checksum = capture_bytes(iter(lambda: stream.read(1024 * 1024), b""), snapshot,
                                         limits.max_source_bytes, "parse", "source_too_large")
            try:
                validate_source(candidate, candidate.name, limits)
            except (PipelineError, OSError, SyntaxError, TypeError, LookupError, AttributeError,
                    RuntimeError, OverflowError):
                candidate.unlink()
                continue
            if chosen is not None:
                record.issues.add("parse", "mineru_origin_ambiguous", action="unverified")
                return None
            selected = staging.parent / f"selected.{kind}"
            candidate.replace(selected)
            chosen = selected, checksum, member.filename
        if chosen is not None:
            selected, checksum, member_name = chosen
            original = staging / "source" / f"origin{selected.suffix}"
            original.parent.mkdir()
            selected.replace(original)
            record.sha256, record.source_bytes = checksum, original.stat().st_size
            ref = FileRef(path=original.relative_to(staging).as_posix(), sha256=checksum)
            return _ArchiveOrigin(original, ref, member_name)
    code = "mineru_origin_invalid" if exact or converted else "mineru_origin_missing"
    record.issues.add("parse", code, action="unverified")
    return None


def _finalize(document: _Document, config: Config, client: httpx.Client,
              limits: Limits, archive: Path | None = None, *, source_client: httpx.Client | None = None) -> dict:
    output_dir, record = document.output_dir, document.record
    captured, staging = document.captured, document.staging
    if archive is None and isinstance(record.source, UrlInput):
        captured = _capture_native_url(record.source, document, limits, source_client, config.request_timeout_seconds)
    source_ref = captured.ref if captured else None
    record.progress("parse")
    with record.measure("parse"):
        if archive is None:
            from doc_pipeline.parsers.text.parser import parse_text_document

            assert captured is not None
            original_dir = captured.original_path.parent if captured.original_path else None
            parsed = parse_text_document(captured.input, captured.ref, staging, source_dir=original_dir,
                                         max_asset_bytes=limits.max_source_bytes, output_root=document.output_root,
                                         resolve_local_assets=original_dir is not None)
        else:
            from doc_pipeline.parsers.multimodal.parser import decode_archive

            if captured is not None:
                parsed = decode_archive(source_ref, archive, staging, limits, source_type=document.kind,
                                        original_path=captured.input.path)
            else:
                with open_validated_archive(archive, limits) as validated:
                    origin = _capture_archive_origin(validated, document, limits)
                    source_ref = origin.ref if origin else None
                    parsed = decode_archive(source_ref, validated, staging, limits, source_type=document.kind,
                                            original_path=origin.path if origin else None,
                                            origin_member=origin.member if origin else None)
        _parsed_issues(record.issues, parsed, source_ref)
        if record.mineru is not None and parsed.provider_metadata is not None:
            for name in ("backend", "version", "ocr_enabled"):
                setattr(record.mineru, name, getattr(parsed.provider_metadata, name))
    with record.measure("render"):
        markdown, mappings = render_markdown(parsed)
    with record.measure("chunk"):
        chunks = make_chunks(parsed, mappings, config)
    _locate_issues(record.issues, mappings, chunks)
    vectors, errors = [], []
    if config.embedding is not None:
        record.embedding_usage = EmbeddingUsage()
        if not any(chunk.embedding_input is not None for chunk in chunks):
            record.stages["embed"].state, record.stages["embed"].reason = "skipped", "no_eligible_chunks"
        else:
            def embed_issue(code, reason, action, retryable, chunk_ids):
                record.issues.add("embed", code, reason, action, retryable,
                                  IssueTarget("chunk", tuple(chunk_ids)))

            def embedding_error(error):
                record.issues.add("embed", error["code"], error["reason"], "failed", error["retryable"],
                                  IssueTarget("chunk", (error["chunk_id"],)))

            def embedding_progress(completed, total, failed):
                record.progress("embedding", completed, total, failed)
                record.checkpoint()

            with record.measure("embed"):
                vectors, errors, record.embedding_usage = embed_chunks(
                    chunks, config, client, on_progress=embedding_progress,
                    on_issue=embed_issue, on_error=embedding_error, usage=record.embedding_usage,
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
    with ExitStack() as publication:
        with record.measure("write"):
            asset_digests = _finalize_assets(parsed, staging)
            exported = list(export_chunks(
                chunks, parsed, source=source_ref, identity=record.identity,
                source_type=document.kind, asset_digests=asset_digests,
            ))
            write_bytes(staging / "content.md", markdown.encode())
            write_jsonl(staging / "chunks.jsonl", (chunk.model_dump(mode="json") for chunk in exported))
            write_jsonl(staging / "embeddings.jsonl", vectors)
            publication.enter_context(install_output(staging, output_dir))
        return record.complete(_result_summary(exported, vectors, errors, embedding=config.embedding is not None))


def _finalize_assets(parsed: ParsedContent, staging: Path) -> dict[str, str]:
    refs = {
        use.asset.path: use.asset for use in parsed.inline_assets if use.asset is not None
    }
    refs.update({
        block.payload.asset.path: block.payload.asset for block in parsed.blocks
        if isinstance(block.payload, DocumentImage) and block.payload.asset is not None
    })
    for ref in refs.values():
        if file_digest(staging / ref.path) != ref.sha256:
            raise PipelineError("write", "resource_digest_mismatch")
    for candidate in (staging / "assets").glob("*"):
        if candidate.relative_to(staging).as_posix() not in refs:
            candidate.unlink()
    return {key: ref.sha256 for key, ref in refs.items()}


def _write_record(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json_bytes(value))
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


class _ReportWriter:
    def __init__(self, path: Path):
        self.path = path
        self.identity = None
        self.current = False

    def available(self) -> bool:
        try:
            info = self.path.lstat()
            return self.current and not self.path.is_symlink() and (info.st_dev, info.st_ino) == self.identity
        except OSError:
            return False

    def __call__(self, value: dict) -> None:
        self.current = False
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if self.path.is_symlink() or (info.st_dev, info.st_ino) != self.identity:
                raise PipelineError("write", "output_not_empty")
        elif self.identity is not None:
            raise PipelineError("write", "output_changed")
        _write_record(self.path, value)
        info = self.path.stat()
        self.identity = info.st_dev, info.st_ino
        self.current = True


def _fail_record(record: ExecutionRecord, error: BaseException) -> None:
    try:
        record.fail(error)
    except OSError:
        logging.warning("report_checkpoint_failed index=%s", record.index)
    except PipelineError:
        logging.warning("report_checkpoint_unavailable index=%s", record.index)


def _http_client(client: httpx.Client | None, config: Config):
    if client is not None:
        return nullcontext(client)
    return httpx.Client(timeout=config.request_timeout_seconds, follow_redirects=False)


def _finish_remote(document: _Document, outcome: ArchiveSuccess | ArchiveFailure, config: Config,
                   client: httpx.Client, limits: Limits) -> dict:
    record = document.record
    if record.failure is not None:
        raise record.failure.error
    record.mineru = outcome.observation
    record.stages["acquire"].end("failed" if isinstance(outcome, ArchiveFailure) else "succeeded",
                                 outcome.elapsed_seconds)
    if isinstance(outcome, ArchiveFailure):
        record.failed_stage = "acquire"
        record.failure_at = outcome.finished
        raise outcome.error
    record.checkpoint()
    return _finalize(document, config, client, limits, outcome.path)


def _acquire(documents: list[_Document], config: Config, client, work: Path,
             requests: MinerURequests | None = None):
    records = {document.record.index: document.record for document in documents}
    def checkpoint(record):
        if record.state == "running":
            try:
                record.checkpoint()
            except (OSError, PipelineError) as error:
                _fail_record(record, error)

    for record in records.values():
        record.stages["acquire"].start()
        checkpoint(record)

    def progress(event):
        records[event.index].progress(event.stage, event.completed, event.total, event.failed)

    def observation(index, value):
        records[index].mineru = value
        checkpoint(records[index])

    def issue(index, code, reason, action, retryable):
        if records[index].state == "running":
            records[index].issues.add("acquire", code, reason, action, retryable)

    def outcome(value):
        record = records[value.index]
        if record.state != "running":
            return
        record.mineru = value.observation
        record.stages["acquire"].end("failed" if isinstance(value, ArchiveFailure) else "succeeded",
                                     value.elapsed_seconds)
        if isinstance(value, ArchiveFailure):
            record.failed_stage, record.failure_at = "acquire", value.finished
            _fail_record(record, value.error)
        else:
            checkpoint(record)

    return obtain_archives(
        [(document.record.index, document.captured.input if document.captured else document.record.source)
         for document in documents], config, client, work,
        on_progress=progress, on_observation=observation, on_issue=issue, on_outcome=outcome, requests=requests,
    )


def process_file(path: Path | UrlInput, output_dir: Path, config: Config | None = None, *,
                 assets: dict[str, Path] | None = None, encoding: str | None = None,
                 identity: SourceIdentity | None = None, client: httpx.Client | None = None,
                 on_progress: ProgressCallback | None = None) -> dict:
    config = config or Config()
    limits = Limits()
    if isinstance(path, UrlInput):
        source = UrlInput(
            url=path.url, assets=path.assets if assets is None else assets,
            encoding=path.encoding if encoding is None else encoding,
            identity=path.identity if identity is None else identity,
        )
    else:
        source = InputFile(path=path, assets=assets or {}, encoding="utf-8" if encoding is None else encoding,
                           identity=identity)
    record = ExecutionRecord(RunClock(), 0, source, config, on_progress)
    with ExitStack() as scope:
        try:
            document = _intake(source, output_dir, limits, record, scope)
            http = scope.enter_context(_http_client(client, config))
            if document.kind in TEXT_FORMATS:
                report = _finalize(document, config, http, limits, source_client=client)
            else:
                with tempfile.TemporaryDirectory(prefix="doc-pipeline-") as temporary:
                    [outcome] = _acquire([document], config, http, Path(temporary))
                    report = _finish_remote(document, outcome, config, http, limits)
        except BaseException as error:
            _fail_record(record, error)
            record.log()
            record.progress(record.status)
            raise
        record.log()
        record.progress(record.status)
        return report


def _process_document(document: _Document, config: Config, client: httpx.Client, limits: Limits,
                      outcome: ArchiveSuccess | ArchiveFailure | None = None, *,
                      source_client: httpx.Client | None = None) -> dict | None:
    try:
        if outcome is None:
            report = _finalize(document, config, client, limits, source_client=source_client)
        else:
            report = _finish_remote(document, outcome, config, client, limits)
    except BaseException as error:
        _fail_record(document.record, error)
        if not isinstance(error, (PipelineError, OSError, ValueError)):
            raise
        report = None
    document.record.progress(document.record.status)
    return report


def process_files(paths: Iterable[Path | InputFile | UrlInput], output_dir: Path, config: Config | None = None, *,
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
    native, uploads, links, records = [], [], [], []
    requests = MinerURequests()
    writer = _ReportWriter(output_dir / "run.json")

    def snapshot(state="running", finished=None):
        files = []
        for record in records:
            report = f"{record.index + 1:04d}/report.json"
            available = record.on_checkpoint is not None and record.on_checkpoint.available()
            error = record.failure.issue if record.failure is not None else None
            files.append({
                "index": record.index, "report": report if available else None,
                "unreported": NA if available else {
                    "source_uri": record.source_snapshot()["uri"],
                    "stage": error.stage if error else "intake",
                    "code": error.code if error else "report_unavailable",
                    "reason": error.reason if error else None,
                    "retryable": error.retryable if error else None,
                },
            })
        return {
            "schema_version": 2, "run": clock.snapshot(state, finished),
            "settings": {"local_workers": config.local_workers, "mineru_batch_size": config.mineru.batch_size},
            "mineru_requests": requests.snapshot() if uploads or links else NA, "files": files,
        }

    try:
        writer(snapshot())
        for index, item in enumerate(inputs):
            source = item if isinstance(item, (InputFile, UrlInput)) else InputFile(path=item)
            record = ExecutionRecord(RunClock(run_id=clock.run_id), index, source, config, on_progress)
            records.append(record)
            try:
                document = _intake(source, output_dir / f"{index + 1:04d}", limits, record, scope,
                                   output_root=output_dir)
            except (PipelineError, OSError, ValueError) as error:
                _fail_record(record, error)
                record.progress(record.status)
            else:
                if document.kind in TEXT_FORMATS:
                    native.append(document)
                else:
                    (links if isinstance(source, UrlInput) else uploads).append(document)
            writer(snapshot())

        with _http_client(client, config) as http:
            pool = ThreadPoolExecutor(max_workers=config.local_workers)
            try:
                futures = [pool.submit(_process_document, document, config, http, limits, source_client=client)
                           for document in native]
                size = config.mineru.batch_size
                for documents in (uploads, links):
                    for start in range(0, len(documents), size):
                        group = documents[start:start + size]
                        with tempfile.TemporaryDirectory(prefix="doc-pipeline-") as temporary:
                            outcomes = _acquire(group, config, http, Path(temporary), requests)
                            for document, outcome in zip(group, outcomes, strict=True):
                                _process_document(document, config, http, limits, outcome)
                                writer(snapshot())
                for future in futures:
                    future.result()
                    writer(snapshot())
            finally:
                pool.shutdown(wait=True, cancel_futures=True)
        result = snapshot("completed", RunInstant.now())
        writer(result)
        return result
    except BaseException as error:
        for record in records:
            if record.state == "running":
                _fail_record(record, error)
        try:
            writer(snapshot("interrupted" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "failed"))
        except (OSError, PipelineError):
            logging.warning("batch_checkpoint_failed")
        raise
    finally:
        for record in records:
            record.log()
