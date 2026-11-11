import argparse
import json
import logging
import signal
from pathlib import Path

from pydantic import ValidationError

from doc_pipeline.contracts.config import Config
from doc_pipeline.contracts.core import InputFile, PipelineError
from doc_pipeline.pipeline import process_file, process_files


def _asset(value: str) -> tuple[str, Path]:
    name, separator, path = value.partition("=")
    if not separator or not name or not path:
        raise argparse.ArgumentTypeError("asset must be REFERENCE=PATH")
    return name, Path(path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="doc-pipeline", description="Process files into an empty directory")
    parser.add_argument("input", type=Path, nargs="+")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", type=Path, help="TOML configuration shared with the Python API")
    parser.add_argument("--encoding", default="utf-8")
    parser.add_argument("--asset", type=_asset, action="append", default=[], metavar="REFERENCE=PATH")
    args = parser.parse_args(argv)
    if len(args.input) > 1 and args.asset:
        parser.error("--asset requires exactly one input")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    previous_handler = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        config = Config.from_toml(args.config) if args.config else Config()
        if len(args.input) == 1:
            report = process_file(args.input[0], args.out, config, assets=dict(args.asset), encoding=args.encoding)
            code = 75 if report["embedding_errors"] else 0
        else:
            report = process_files([InputFile(path=path, encoding=args.encoding) for path in args.input],
                                   args.out, config)
            errors = [item["error"] for item in report["files"] if "error" in item]
            if any(not error["retryable"] for error in errors):
                code = 65
            else:
                code = 75 if errors or report["embedding_error_count"] else 0
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return code
    except PipelineError as error:
        print(json.dumps({"error": error.code, "stage": error.stage, "retryable": error.retryable}), flush=True)
        return 75 if error.retryable else 65
    except ValidationError as error:
        details = [{"field": ".".join(map(str, item["loc"])), "message": item["msg"]}
                   for item in error.errors(include_input=False, include_context=False)]
        print(json.dumps({"error": "invalid_configuration_or_input", "details": details}), flush=True)
        return 65
    except (ValueError, OSError) as error:
        code = "io_error" if isinstance(error, OSError) else "invalid_configuration_or_input"
        print(json.dumps({"error": code}), flush=True)
        return 65
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted"}), flush=True)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
