import re
import tempfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import httpx
from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from doc_pipeline.chunking.builder import make_chunks
from doc_pipeline.chunking.citations import chunk_records
from doc_pipeline.contracts.config import Config, Limits
from doc_pipeline.contracts.core import FileRef, InputFile, PipelineError
from doc_pipeline.contracts.run import EmbeddingUsage, ExecutionRecord, RunClock
from doc_pipeline.parsed_content.md_renderer import render_markdown
from doc_pipeline.parsers.multimodal.client import ArchiveFailure, ArchiveSuccess, obtain_archives
from doc_pipeline.services.embeddings import embed_chunks
from doc_pipeline.services.io import file_digest, write_bytes, write_json, write_jsonl

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
class _Document:
    source: InputFile
    ref: FileRef
    kind: str
    output_dir: Path
    record: ExecutionRecord
    output_root: Path


def _empty_directory(directory: Path) -> Path:
    directory = directory.resolve()
    if directory.exists() and any(directory.iterdir()):
        raise PipelineError("intake", "output_not_empty")
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _intake(source: InputFile, output_dir: Path, limits: Limits, record: ExecutionRecord,
            *, output_root: Path | None = None) -> _Document:
    with record.measure("intake"):
        path = source.path
        kind = validate_source(path, path.name, limits)
        for asset in source.assets.values():
            if asset.stat().st_size > limits.max_source_bytes:
                raise PipelineError("intake", "asset_too_large")
        output_dir = _empty_directory(output_dir)
        ref = FileRef(path=str(path.resolve()), sha256=file_digest(path))
    return _Document(source, ref, kind, output_dir, record, output_root or output_dir)


def _finalize(document: _Document, config: Config, client: httpx.Client,
              limits: Limits, archive: Path | None = None) -> dict:
    output_dir, record = document.output_dir, document.record
    with record.measure("parse"):
        if archive is None:
            from doc_pipeline.parsers.text.parser import parse_text_document

            parsed = parse_text_document(document.source, document.ref, output_dir,
                                         max_asset_bytes=limits.max_source_bytes, output_root=document.output_root)
        else:
            from doc_pipeline.parsers.multimodal.parser import decode_archive

            parsed = decode_archive(document.ref, archive, output_dir, limits, source_type=document.kind)
        record.parse_status = parsed.parse_status
    with record.measure("render"):
        markdown, mappings = render_markdown(parsed)
    with record.measure("chunk"):
        chunks = make_chunks(parsed, mappings, config)
        record.chunk_count = len(chunks)
    with record.measure("write"):
        write_bytes(output_dir / "content.md", markdown.encode())
        write_jsonl(output_dir / "chunks.jsonl", chunk_records(
            chunks, source=document.ref, source_type=document.kind, parsed=parsed,
        ))
    vectors, errors = [], []
    if config.embedding is not None:
        with record.measure("embedding"):
            vectors, errors, record.embedding_usage = embed_chunks(chunks, config, client)
            record.embedding_count = len(vectors)
            record.embedding_error_count = len(errors)
    with record.measure("write"):
        write_jsonl(output_dir / "embeddings.jsonl", vectors)
    report = {
        "source": document.ref.model_dump(),
        "parse_status": parsed.parse_status,
        "chunk_count": record.chunk_count,
        "chunking": config.chunking.model_dump(),
        "embedding": config.embedding.model_dump() if config.embedding else None,
        "embedding_count": record.embedding_count,
        "embedding_errors": errors,
        "diagnostics": parsed.diagnostics,
    }
    record.finish()
    report["execution"] = record.snapshot(config.mineru.parse_options())
    with record.measure("write"):
        _write_record(output_dir / "report.json", report)
    return report


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
                 client: httpx.Client | None = None) -> dict:
    config = config or Config()
    limits = Limits()
    record = ExecutionRecord(RunClock(), 0)
    try:
        source = InputFile(path=path, assets=assets or {}, encoding=encoding)
        document = _intake(source, output_dir, limits, record)
        with _http_client(client, config) as http:
            if document.kind in TEXT_FORMATS:
                report = _finalize(document, config, http, limits)
            else:
                with tempfile.TemporaryDirectory(prefix="doc-pipeline-") as temporary:
                    [outcome] = obtain_archives([(0, source)], config, http, Path(temporary))
                    report = _finish_remote(document, outcome, config, http, limits)
    except (PipelineError, OSError, ValueError) as error:
        record.fail(error)
        record.log()
        raise
    record.log()
    return report


def _process_document(document: _Document, config: Config, client: httpx.Client, limits: Limits,
                      outcome: ArchiveSuccess | ArchiveFailure | None = None) -> dict | None:
    try:
        if outcome is None:
            return _finalize(document, config, client, limits)
        return _finish_remote(document, outcome, config, client, limits)
    except (PipelineError, OSError, ValueError) as error:
        document.record.fail(error)
        return None


def process_files(paths: Iterable[Path | InputFile], output_dir: Path, config: Config | None = None, *,
                  client: httpx.Client | None = None) -> dict:
    config = config or Config()
    inputs = list(paths)
    if not inputs:
        raise PipelineError("intake", "no_input_files")
    clock = RunClock()
    output_dir = _empty_directory(output_dir)
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
        record = ExecutionRecord(RunClock(run_id=clock.run_id), index)
        path = item.path if isinstance(item, InputFile) else item
        try:
            source = item if isinstance(item, InputFile) else InputFile(path=item)
            document = _intake(source, output_dir / f"{index + 1:04d}", limits, record,
                               output_root=output_dir)
        except (PipelineError, OSError, ValueError) as error:
            record.fail(error)
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
                    outcomes = obtain_archives([(item.record.index, item.source) for item in group],
                                               config, http, Path(temporary))
                    for document, outcome in zip(group, outcomes, strict=True):
                        report = _process_document(document, config, http, limits, outcome)
                        collect(document.record, document.source.path, report)
            for document, future in zip(local, local_futures, strict=True):
                collect(document.record, document.source.path, future.result())
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
