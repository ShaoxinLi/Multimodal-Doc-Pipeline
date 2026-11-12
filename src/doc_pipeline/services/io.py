import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from collections.abc import Callable
from contextlib import closing, contextmanager, nullcontext
from contextvars import ContextVar
from fcntl import LOCK_EX, LOCK_NB, flock
from pathlib import Path
from stat import S_ISREG
from urllib.parse import urljoin, urlsplit

import httpx

from doc_pipeline.contracts.core import PipelineError
from doc_pipeline.contracts.observations import RequestCounts, ServiceIssueCallback


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_jsonl(path: Path, rows) -> None:
    with path.open("wb") as stream:
        for row in rows:
            stream.write(json_bytes(row))


def provider_reason(value, *, secrets: tuple[str, ...] = ()) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    reason = value.splitlines()[0]
    for secret in secrets:
        if secret:
            reason = reason.replace(secret, "[redacted]")
    reason = re.sub(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)[^\s<>\"']+", "[redacted]", reason)
    reason = re.sub(r"(?i)\b(?:bearer|basic)\s+[^\s,;]+", "[redacted]", reason)
    reason = re.sub(
        r'''(?ix)\b(?:authorization|api[-_ ]?key|access[-_ ]?token|token|password|signature)\b["']?
            \s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)''',
        "[redacted]", reason,
    )
    reason = " ".join(reason.split())
    return reason[:512] or None


def status_error(response: httpx.Response, stage: str, *, secrets: tuple[str, ...] = (),
                 include_reason: bool = True) -> PipelineError:
    status = response.status_code
    code = "authentication_failed" if status in {401, 403} else f"http_{status}"
    reason = None
    if include_reason:
        try:
            value = response.json()
        except (ValueError, RecursionError, httpx.ResponseNotRead):
            pass
        else:
            if isinstance(value, dict):
                detail = value.get("err_msg", value.get("msg"))
                if detail is None and isinstance(value.get("error"), dict):
                    detail = value["error"].get("message")
                reason = provider_reason(detail, secrets=secrets)
    return PipelineError(stage, code, status in {408, 429} or status >= 500, reason=reason)


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
                 retry_http: bool = True, include_error_reason: bool = True,
                 retry_response_once: Callable[[dict], PipelineError | None] | None = None,
                 requests: RequestCounts | None = None,
                 on_response: Callable[[dict], None] | None = None,
                 on_issue: ServiceIssueCallback | None = None,
                 **kwargs):
    response_retried = False
    retry_issue = None
    for attempt in range(RETRY_ATTEMPTS + int(retry_response_once is not None)):
        try:
            with suppress_http_logging():
                request = client.build_request(method, url, timeout=bounded_timeout(timeout, deadline), **kwargs)
                if retry_issue is not None and on_issue is not None:
                    on_issue(retry_issue.code, retry_issue.reason, "retried", retry_issue.retryable)
                if requests is not None:
                    requests.requests += 1
                with closing(client.send(request, auth=None, follow_redirects=False, stream=True)) as response:
                    if requests is not None:
                        requests.responses += 1
                    response.read()
            try:
                value = response.json()
            except (ValueError, RecursionError):
                value = None
            if isinstance(value, dict) and on_response is not None:
                on_response(value)
            if not response.is_success:
                authorization = request.headers.get("Authorization", "")
                raise status_error(response, stage, secrets=(authorization, authorization.removeprefix("Bearer ")),
                                   include_reason=include_error_reason)
            if not isinstance(value, dict):
                raise PipelineError(stage, "invalid_provider_response")
            retry = retry_response_once(value) if retry_response_once is not None else None
            if retry is not None and not response_retried:
                response_retried = True
                retry_issue = retry
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
            if not retry_http:
                raise
        if not error.retryable or response_retried or attempt + 1 == RETRY_ATTEMPTS:
            raise error
        retry_issue = error
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


def source_url(value: str, stage: str) -> httpx.URL:
    try:
        parsed = urlsplit(value)
        url = httpx.URL(value)
        query = tuple(part.encode("ascii") for part in value.partition("?")[1:])
        if (
            parsed.scheme not in {"http", "https"} or not url.host or "@" in parsed.netloc or "%" in url.host
            or any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in value)
            or parsed.port == 0
            or url.raw_path.partition(b"?")[1:] != query
        ):
            raise ValueError
    except (ValueError, httpx.InvalidURL):
        raise PipelineError(stage, "unsafe_source_url") from None
    return url


def _open_source(client: httpx.Client, url: httpx.URL, deadline: float) -> httpx.Response:
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise PipelineError("acquire", "source_download_timeout", True)
    request = httpx.Request(
        "GET", url, headers={"Accept-Encoding": "identity"},
        extensions={"timeout": httpx.Timeout(timeout).as_dict()},
    )
    return client.send(request, auth=None, follow_redirects=False, stream=True)


def capture_url(value: str, target: Path, limit: int, timeout: float, client: httpx.Client | None) -> str:
    deadline = time.monotonic() + timeout
    current = value
    try:
        with suppress_http_logging():
            for redirect in range(6):
                url = source_url(current, "acquire")
                transport = nullcontext(client) if client is not None else httpx.Client(trust_env=False)
                with transport as http, closing(_open_source(http, url, deadline)) as response:
                    if response.is_redirect:
                        location = response.headers.get("Location")
                        if redirect == 5 or not location:
                            raise PipelineError("acquire", "invalid_source_redirect")
                        relative, separator, query = location.partition("#")[0].partition("?")
                        current = urljoin(current, relative)
                        if separator:
                            current = current.partition("?")[0] + separator + query
                        continue
                    if not response.is_success:
                        raise status_error(response, "acquire", include_reason=False)
                    if response.status_code == 206:
                        raise PipelineError("acquire", "invalid_source_response")
                    if response.headers.get("Content-Type", "").partition(";")[0].strip().lower() in {
                        "text/html", "application/xhtml+xml",
                    }:
                        raise PipelineError("acquire", "unsupported_html")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise PipelineError("acquire", "unsupported_source_content_encoding")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as snapshot:
                        return capture_bytes(response.iter_bytes(), snapshot, limit, "acquire", "source_too_large",
                                             deadline=deadline)
    except httpx.TimeoutException:
        raise PipelineError("acquire", "source_download_timeout", True) from None
    except httpx.TransportError:
        raise PipelineError("acquire", "source_download_failed", True) from None
    except (ValueError, httpx.InvalidURL):
        raise PipelineError("acquire", "invalid_source_response") from None
    raise PipelineError("acquire", "invalid_source_redirect")


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
    flags = os.O_RDONLY | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as source, target.open("xb") as snapshot:
        info = os.fstat(source.fileno())
        if not S_ISREG(info.st_mode):
            raise PipelineError("intake", "invalid_input_file")
        if info.st_size > limit:
            raise PipelineError("intake", error_code)
        return capture_bytes(iter(lambda: source.read(1024 * 1024), b""), snapshot, limit, "intake", error_code)


def capture_bytes(chunks, snapshot, limit: int, stage: str, error_code: str, *, deadline: float | None = None) -> str:
    checksum = hashlib.sha256()
    for data in chunks:
        if deadline is not None and time.monotonic() >= deadline:
            raise PipelineError(stage, "source_download_timeout", True)
        limit -= len(data)
        if limit < 0:
            raise PipelineError(stage, error_code)
        checksum.update(data)
        snapshot.write(data)
    return checksum.hexdigest()


@contextmanager
def install_output(staging: Path, destination: Path):
    installed = []
    try:
        for path in staging.iterdir():
            target = destination / path.name
            if target.exists() or target.is_symlink():
                raise PipelineError("write", "output_not_empty")
            installed.append(target)
            shutil.move(path, target)
        yield
    except BaseException:
        for target in reversed(installed):
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
        raise
