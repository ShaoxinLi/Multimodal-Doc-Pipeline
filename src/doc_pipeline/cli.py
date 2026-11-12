import argparse
import json
import logging
import signal
import sys
from collections import Counter
from contextlib import nullcontext
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic

from pydantic import ValidationError

from doc_pipeline.contracts.config import Config
from doc_pipeline.contracts.core import InputFile, PipelineError, UrlInput
from doc_pipeline.contracts.run import ProgressEvent
from doc_pipeline.pipeline import process_file, process_files

_STAGE_LABELS = {
    "intake": "Checking input",
    "source_download": "Downloading source",
    "upload": "Submitting to MinerU",
    "remote_parse": "Waiting for MinerU",
    "download": "Downloading the MinerU result",
    "parse": "Preparing document output",
    "embedding": "Generating embeddings",
}
_STATUS_LABELS = {
    "complete": "Processing complete",
    "partial": "Processing completed with issues",
    "failed": "Processing failed",
}
_DIAGNOSTICS = {
    "source_location_unresolved": "Some content could not be located in the source document.",
    "mineru_empty_block": "MinerU returned empty content blocks.",
    "mineru_heading_levels_inferred_from_numbering": "Heading levels were inferred from section numbering.",
}
_UPDATE_INTERVAL = 10


def _document_status(report: dict) -> str:
    if report["run"]["state"] != "completed":
        return "failed"
    chunks, embedding = report["result"]["chunks"], report["result"]["embedding"]
    if chunks["partial"] or chunks["unparsed"] or isinstance(embedding, dict) and embedding["failed"]:
        return "partial"
    return "complete"


def _duration(seconds: float) -> str:
    minutes, seconds = divmod(round(seconds), 60)
    return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"


class TerminalProgress:
    def __init__(self) -> None:
        self.started = monotonic()
        self.active: dict[int, tuple[ProgressEvent, float]] = {}
        self.lock = Lock()
        self.stopped = Event()
        self.worker = Thread(target=self._refresh, daemon=True)

    def __enter__(self):
        self.worker.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()
        self.worker.join()

    def __call__(self, event: ProgressEvent) -> None:
        with self.lock:
            if event.stage in _STATUS_LABELS:
                self.active.pop(event.index, None)
                return
            self._show(event, monotonic())

    def _refresh(self) -> None:
        while not self.stopped.wait(1):
            with self.lock:
                now = monotonic()
                for event, last_shown in self.active.values():
                    if now - last_shown >= _UPDATE_INTERVAL:
                        self._show(event, now)

    def _show(self, event: ProgressEvent, now: float) -> None:
        message = _STAGE_LABELS[event.stage]
        if event.completed is not None and event.total is not None:
            unit = "chunks processed" if event.stage == "embedding" else "pages extracted"
            message += f" ({event.completed}/{event.total} {unit}"
            if event.failed:
                message += f", {event.failed} failed"
            message += ")"
        print(f"[{_duration(now - self.started)}] {event.path.name} | {message}", file=sys.stderr, flush=True)
        if event.stage != "intake":
            self.active[event.index] = (event, now)


def _batch_documents(report: dict, output_dir: Path) -> list[dict | None]:
    return [
        json.loads((output_dir / item["report"]).read_text(encoding="utf-8")) if item["report"] else None
        for item in report["files"]
    ]


def _embedding_failed(report: dict) -> bool:
    embedding = report["result"]["embedding"]
    return isinstance(embedding, dict) and embedding["failed"] > 0


def _batch_exit_code(report: dict, documents: list[dict | None]) -> int:
    code = 0
    for item, document in zip(report["files"], documents, strict=True):
        if document is None:
            retryable = item["unreported"]["retryable"]
        elif document["run"]["state"] == "completed":
            if _embedding_failed(document):
                code = 75
            continue
        else:
            fatal = next(
                (issue for issue in document["issues"]
                 if issue["action"] == "failed" and issue["targets"] == [{"unit": "document", "values": "n/a"}]),
                None,
            )
            retryable = fatal["retryable"] if fatal else None
        if retryable is not True:
            return 65
        code = 75
    return code


def print_summary(report: dict, output_dir: Path, documents: list[dict | None] | None = None) -> None:
    batch = documents is not None
    if batch:
        counts = Counter(_document_status(document) if document is not None else "failed" for document in documents)
        status = "failed" if counts["failed"] == len(documents) else (
            "partial" if counts["failed"] or counts["partial"] else "complete"
        )
        results = [
            document["result"] for document in documents
            if document is not None and document["run"]["state"] == "completed"
        ]
    else:
        status = _document_status(report)
        results = [report["result"]]

    print(f"{_STATUS_LABELS[status]} in {_duration(report['run']['elapsed_seconds'])}.")
    if batch:
        print(
            f"Files: {counts['complete']} complete, {counts['partial']} partial, "
            f"{counts['failed']} failed."
        )
    else:
        chunks = report["result"]["chunks"]
        if chunks["partial"] or chunks["unparsed"]:
            print("Document parsing was partial. Review the report before using the output.")
    chunks = sum(sum(result["chunks"].values()) for result in results)
    print(f"Chunks: {chunks}.")
    embeddings = [result["embedding"] for result in results if isinstance(result["embedding"], dict)]
    if batch or embeddings:
        generated = sum(result["succeeded"] for result in embeddings)
        failed = sum(result["failed"] for result in embeddings)
        print(f"Embeddings: {generated} generated, {failed} failed.")
    if batch:
        print(f"Run report: {output_dir / 'run.json'}")
        return

    stages = report["stages"]
    remote_times = [
        f"{label} {_duration(stages[stage]['seconds'])}"
        for stage, label in (("acquire", "MinerU"), ("embed", "embeddings"))
        if isinstance(stages[stage]["seconds"], (int, float))
    ]
    if remote_times:
        print(f"Time spent: {', '.join(remote_times)}.")
    notes = {_DIAGNOSTICS.get(issue["code"]) for issue in report["issues"]}
    for note in sorted(notes - {None}):
        print(note)
    if None in notes:
        print("See the report for more diagnostics.")
    print(f"Document: {output_dir / 'content.md'}")
    print(f"Report: {output_dir / 'report.json'}")


def _asset(value: str) -> tuple[str, Path]:
    name, separator, path = value.partition("=")
    if not separator or not name or not path:
        raise argparse.ArgumentTypeError("asset must be REFERENCE=PATH")
    return name, Path(path)


class _HideCompletionLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.msg != "document_finished %s"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="doc-pipeline", description="Process files into an empty directory")
    parser.add_argument("input", nargs="+", help="Local path or direct HTTP(S) document URL")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", type=Path, help="TOML configuration shared with the Python API")
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument("--asset", type=_asset, action="append", default=[], metavar="REFERENCE=PATH")
    parser.add_argument("--format", choices=("auto", "text", "json"), default="auto",
                        help="Result format. Auto uses text in a terminal and JSON otherwise.")
    parser.add_argument("--progress", action=argparse.BooleanOptionalAction, default=None,
                        help="Show progress on stderr. Enabled by default when stderr is a terminal.")
    args = parser.parse_args(argv)
    if len(args.input) > 1 and args.asset:
        parser.error("--asset requires exactly one input")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    text_output = args.format == "text" or (args.format == "auto" and sys.stdout.isatty())
    show_progress = sys.stderr.isatty() if args.progress is None else args.progress
    logger = logging.getLogger()
    completion_filter = _HideCompletionLog()
    if text_output:
        logger.addFilter(completion_filter)

    def print_error(payload: dict) -> None:
        if not text_output:
            print(json.dumps(payload), flush=True)
            return
        reason = payload["error"].replace("_", " ")
        if "stage" in payload:
            reason += f" during {payload['stage'].replace('_', ' ')}"
        print(f"Processing stopped: {reason}.", file=sys.stderr)
        for detail in payload.get("details", ()):
            print(f"{detail['field']}: {detail['message']}", file=sys.stderr)
        if payload.get("retryable"):
            print("You can retry with a new or empty output directory.", file=sys.stderr)

    previous_handler = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        config = Config.from_toml(args.config) if args.config else Config()
        assets = dict(args.asset)
        inputs = [
            UrlInput(url=value, encoding=args.encoding, assets=assets)
            if value.lower().startswith(("http://", "https://"))
            else InputFile(path=Path(value), encoding=args.encoding, assets=assets)
            for value in args.input
        ]
        with (TerminalProgress() if show_progress else nullcontext()) as progress:
            if len(inputs) == 1:
                item = inputs[0]
                if isinstance(item, UrlInput):
                    report = process_file(item, args.out, config, on_progress=progress)
                else:
                    report = process_file(item.path, args.out, config, assets=item.assets,
                                          encoding=item.encoding, on_progress=progress)
                documents = None
                code = 75 if _embedding_failed(report) else 0
            else:
                report = process_files(inputs, args.out, config, on_progress=progress)
                documents = _batch_documents(report, args.out)
                code = _batch_exit_code(report, documents)
        if text_output:
            print_summary(report, args.out, documents)
        else:
            print(json.dumps(report, ensure_ascii=False), flush=True)
        return code
    except PipelineError as error:
        print_error({"error": error.code, "stage": error.stage, "retryable": error.retryable})
        return 75 if error.retryable else 65
    except ValidationError as error:
        details = [{"field": ".".join(map(str, item["loc"])), "message": item["msg"]}
                   for item in error.errors(include_input=False, include_context=False)]
        print_error({"error": "invalid_configuration_or_input", "details": details})
        return 65
    except (ValueError, OSError) as error:
        code = "io_error" if isinstance(error, OSError) else "invalid_configuration_or_input"
        print_error({"error": code})
        return 65
    except KeyboardInterrupt:
        print_error({"error": "interrupted"})
        return 130
    finally:
        logger.removeFilter(completion_filter)
        signal.signal(signal.SIGTERM, previous_handler)
