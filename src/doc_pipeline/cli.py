import argparse
import json
import logging
import signal
import sys
from contextlib import nullcontext
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic

from pydantic import ValidationError

from doc_pipeline.contracts.config import Config
from doc_pipeline.contracts.core import InputFile, PipelineError
from doc_pipeline.contracts.run import ProgressEvent
from doc_pipeline.pipeline import process_file, process_files

_STAGE_LABELS = {
    "intake": "Checking input",
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
    "mineru_source_locations_unresolved": "Some content could not be located in the source document.",
    "mineru_source_location_unresolved": "Some content could not be located in the source document.",
    "mineru_empty_block": "MinerU returned empty content blocks.",
    "mineru_heading_levels_inferred_from_numbering": "Heading levels were inferred from section numbering.",
}
_UPDATE_INTERVAL = 10


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


def print_summary(report: dict, output_dir: Path) -> None:
    batch = "files" in report
    execution = report if batch else report["execution"]
    print(f"{_STATUS_LABELS[execution['status']]} in {_duration(execution['elapsed_seconds'])}.")
    if batch:
        print(
            f"Files: {report['complete_file_count']} complete, {report['partial_file_count']} partial, "
            f"{report['failed_file_count']} failed."
        )
    elif report["parse_status"] == "partial":
        print("Document parsing was partial. Review the report before using the output.")
    print(f"Chunks: {report['chunk_count']}.")
    if batch or report["embedding"] is not None:
        print(f"Embeddings: {report['embedding_count']} generated, {execution['embedding_error_count']} failed.")
    if batch:
        print(f"Run report: {output_dir / 'run.json'}")
        return

    stages = execution["stage_seconds"]
    remote_times = [
        f"{label} {_duration(stages[stage])}"
        for stage, label in (("remote_acquisition", "MinerU"), ("embedding", "embeddings"))
        if stage in stages
    ]
    if remote_times:
        print(f"Time spent: {', '.join(remote_times)}.")
    notes = {_DIAGNOSTICS.get(item.split(' ', 1)[0]) for item in report["diagnostics"]}
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
    parser.add_argument("input", type=Path, nargs="+")
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
        with (TerminalProgress() if show_progress else nullcontext()) as progress:
            if len(args.input) == 1:
                report = process_file(args.input[0], args.out, config, assets=dict(args.asset), encoding=args.encoding,
                                      on_progress=progress)
                code = 75 if report["embedding_errors"] else 0
            else:
                report = process_files([InputFile(path=path, encoding=args.encoding) for path in args.input],
                                       args.out, config, on_progress=progress)
                errors = [item["error"] for item in report["files"] if "error" in item]
                if any(not error["retryable"] for error in errors):
                    code = 65
                else:
                    code = 75 if errors or report["embedding_error_count"] else 0
        if text_output:
            print_summary(report, args.out)
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
