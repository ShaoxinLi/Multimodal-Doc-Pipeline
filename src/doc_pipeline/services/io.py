import hashlib
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
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
                 **kwargs):
    for attempt in range(RETRY_ATTEMPTS):
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
            return value
        except httpx.TransportError:
            error = PipelineError(stage, "transport_error", True)
            if not retry_transport:
                raise error
        except (httpx.InvalidURL, httpx.DecodingError):
            raise PipelineError(stage, "invalid_provider_response") from None
        except PipelineError as exc:
            error = exc
        if not error.retryable or attempt + 1 == RETRY_ATTEMPTS:
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
