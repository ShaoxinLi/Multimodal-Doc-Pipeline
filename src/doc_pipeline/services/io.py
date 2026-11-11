import errno
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from fcntl import LOCK_EX, LOCK_NB, flock
from pathlib import Path
from stat import S_IMODE, S_ISREG
from urllib.parse import urlsplit

import httpx

from doc_pipeline.contracts.core import PipelineError


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_json(path: Path, value) -> None:
    write_bytes(path, json_bytes(value))


def write_jsonl(path: Path, rows) -> None:
    with path.open("wb") as stream:
        for row in rows:
            stream.write(json_bytes(row))


def status_error(response: httpx.Response, stage: str) -> PipelineError:
    status = response.status_code
    code = "authentication_failed" if status in {401, 403} else f"http_{status}"
    return PipelineError(stage, code, status in {408, 429} or status >= 500)


_http_logging_suppressed = ContextVar("http_logging_suppressed", default=False)


@contextmanager
def suppress_http_logging():
    # HTTPX logs the full URL, including signatures on provider transfers.
    def allow_record(_):
        return not _http_logging_suppressed.get()

    loggers = [logging.getLogger(name) for name in ("httpx", "httpcore.http11", "httpcore.http2", "httpcore.proxy")]
    for logger in loggers:
        logger.addFilter(allow_record)
    token = _http_logging_suppressed.set(True)
    try:
        yield
    finally:
        _http_logging_suppressed.reset(token)
        for logger in loggers:
            logger.removeFilter(allow_record)


RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1


def request_json(client: httpx.Client, stage: str, method: str, url: str,
                 *, timeout: float, deadline: float | None = None, retry_transport: bool = True,
                 retry_response_once: Callable[[dict], bool] | None = None,
                 **kwargs):
    response_retried = False
    for attempt in range(RETRY_ATTEMPTS + int(retry_response_once is not None)):
        try:
            with suppress_http_logging():
                response = client.request(method, url, auth=None, follow_redirects=False,
                                          timeout=bounded_timeout(timeout, deadline), **kwargs)
            if not response.is_success:
                raise status_error(response, stage)
            try:
                value = response.json()
            except (ValueError, RecursionError):
                raise PipelineError(stage, "invalid_provider_response") from None
            if not isinstance(value, dict):
                raise PipelineError(stage, "invalid_provider_response")
            if retry_response_once is not None and retry_response_once(value) and not response_retried:
                response_retried = True
                continue
            return value
        except httpx.TransportError:
            error = PipelineError(stage, "transport_error", True)
            if not retry_transport:
                raise error
        except (httpx.InvalidURL, httpx.DecodingError):
            raise PipelineError(stage, "invalid_provider_response") from None
        except PipelineError as exc:
            error = exc
        if not error.retryable or response_retried or attempt + 1 == RETRY_ATTEMPTS:
            raise error
        time.sleep(bounded_timeout(RETRY_DELAY_SECONDS * 2 ** attempt, deadline))


def bounded_timeout(seconds: float, deadline: float | None) -> float:
    if deadline is None:
        return seconds
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PipelineError("parse", "parse_timeout", True)
    return min(seconds, remaining)


def transfer_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        parsed.port
    except (ValueError, TypeError) as exc:
        raise PipelineError("parse", "unsafe_provider_url") from exc
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise PipelineError("parse", "unsafe_provider_url")
    return url


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


def capture_file(path: Path, target: Path, limit: int, error_code: str) -> str:
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


def install_output(staging: Path, destination: Path, *, preserve_directory: bool) -> None:
    if not preserve_directory:
        mode = S_IMODE(destination.stat().st_mode)
        staging.chmod(mode)
        try:
            staging.rename(destination)
        except OSError as error:
            if error.errno not in {errno.EBUSY, errno.EXDEV}:
                raise
        else:
            return
    installed = []
    try:
        for path in staging.iterdir():
            if path.name == "report.json":
                continue
            target = destination / path.name
            if target.exists() or target.is_symlink():
                raise PipelineError("write", "output_not_empty")
            installed.append(target)
            shutil.move(path, target)
        report = destination / "report.json"
        if report.exists() or report.is_symlink():
            raise PipelineError("write", "output_not_empty")
        temporary = destination / ".report.json.tmp"
        with temporary.open("xb") as stream:
            installed.append(temporary)
            stream.write((staging / "report.json").read_bytes())
        installed.append(report)
        temporary.rename(report)
    except BaseException:
        for target in reversed(installed):
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        raise
