import os
import re
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

from doc_pipeline.contracts.config import Config, Limits, api_key
from doc_pipeline.contracts.core import InputFile, PipelineError
from doc_pipeline.contracts.run import MinerUObservation, RunInstant
from doc_pipeline.services.io import (
    RETRY_ATTEMPTS,
    RETRY_DELAY_SECONDS,
    bounded_timeout,
    request_json,
    status_error,
    suppress_http_logging,
    transfer_url,
)

POLL_INTERVAL_SECONDS = 3
_PENDING_STATES = {"waiting-file", "uploading", "pending", "running", "converting"}


@dataclass(frozen=True)
class _MinerUErrorSpec:
    code: str
    retryable: bool


_MINERU_REQUEST_REJECTED = _MinerUErrorSpec("mineru_request_rejected", False)
_MINERU_ERRORS: dict[str, _MinerUErrorSpec] = {
    "A0202": _MinerUErrorSpec("mineru_invalid_token", False),
    "A0211": _MinerUErrorSpec("mineru_expired_token", False),
    "-500": _MinerUErrorSpec("mineru_invalid_parameters", False),
    "-10001": _MinerUErrorSpec("mineru_service_error", True),
    "-10002": _MinerUErrorSpec("mineru_invalid_parameters", False),
    "-60001": _MinerUErrorSpec("mineru_upload_url_failed", True),
    "-60002": _MinerUErrorSpec("mineru_unsupported_format", False),
    "-60003": _MinerUErrorSpec("mineru_file_read_failed", False),
    "-60004": _MinerUErrorSpec("mineru_empty_file", False),
    "-60005": _MinerUErrorSpec("mineru_file_size_limit", False),
    "-60006": _MinerUErrorSpec("mineru_page_limit", False),
    "-60007": _MinerUErrorSpec("mineru_service_unavailable", True),
    "-60008": _MinerUErrorSpec("mineru_file_read_timeout", False),
    "-60009": _MinerUErrorSpec("mineru_queue_full", True),
    "-60010": _MinerUErrorSpec("mineru_processing_failed", True),
    "-60011": _MinerUErrorSpec("mineru_file_missing", False),
    "-60012": _MinerUErrorSpec("mineru_task_missing", False),
    "-60013": _MinerUErrorSpec("mineru_task_forbidden", False),
    "-60014": _MinerUErrorSpec("mineru_task_running", False),
    "-60015": _MinerUErrorSpec("mineru_file_conversion_failed", False),
    "-60016": _MinerUErrorSpec("mineru_export_conversion_failed", False),
    "-60017": _MinerUErrorSpec("mineru_retry_limit", False),
    "-60018": _MinerUErrorSpec("mineru_daily_quota_exceeded", False),
    "-60019": _MinerUErrorSpec("mineru_html_quota_exceeded", False),
    "-60020": _MinerUErrorSpec("mineru_file_split_failed", True),
    "-60021": _MinerUErrorSpec("mineru_page_count_failed", True),
    "-60022": _MinerUErrorSpec("mineru_webpage_fetch_failed", True),
}


@dataclass(frozen=True)
class ArchiveResult:
    index: int
    observation: MinerUObservation
    elapsed_seconds: float


@dataclass(frozen=True)
class ArchiveSuccess(ArchiveResult):
    path: Path


@dataclass(frozen=True)
class ArchiveFailure(ArchiveResult):
    error: PipelineError
    finished: RunInstant


@dataclass
class _Submission:
    index: int
    source: InputFile
    observation: MinerUObservation = field(default_factory=lambda: MinerUObservation(uuid.uuid4().hex))


def _api(client, config: Config, deadline: float, method, route, **kwargs):
    key = api_key("MINERU_API_KEY", config.mineru.api_key)
    if not key:
        raise PipelineError("parse", "missing_mineru_credentials")
    value = request_json(client, "parse", method,
                         config.mineru.api_base_url + route,
                         timeout=config.request_timeout_seconds, deadline=deadline,
                         retry_transport=method != "POST",
                         headers={"Authorization": "Bearer " + key}, **kwargs)
    provider_code = value.get("code")
    if type(provider_code) is not int or provider_code != 0:
        error = _MINERU_REQUEST_REJECTED
        if type(provider_code) is int or type(provider_code) is str:
            error = _MINERU_ERRORS.get(str(provider_code), error)
        raise PipelineError("parse", error.code, error.retryable)
    if not isinstance(value.get("data"), dict):
        raise PipelineError("parse", "invalid_provider_response")
    return value["data"]


def _transfer(client, method, url, timeout, *, stream=False, **kwargs):
    # Build the request directly so client defaults never reach signed endpoints.
    request = httpx.Request(method, transfer_url(url),
                            extensions={"timeout": httpx.Timeout(timeout).as_dict()}, **kwargs)
    with suppress_http_logging():
        return client.send(request, auth=None, follow_redirects=False, stream=stream)


def _upload(client, url: str, path: Path, timeout: float) -> None:
    try:
        with path.open("rb") as stream:
            with closing(_transfer(client, "PUT", url, timeout, content=stream,
                                   headers={"Content-Length": str(os.fstat(stream.fileno()).st_size)})) as response:
                if not response.is_success:
                    raise status_error(response, "parse")
    except httpx.TransportError:
        raise PipelineError("parse", "upload_failed", True) from None
    except httpx.DecodingError:
        raise PipelineError("parse", "invalid_upload_response") from None
    except OSError:
        raise PipelineError("parse", "upload_io_error") from None


def _download(client, url: str, target: Path, timeout: float) -> None:
    partial = target.with_suffix(".part")
    max_bytes = Limits().max_zip_bytes
    for attempt in range(RETRY_ATTEMPTS):
        try:
            current = url
            for redirect in range(6):
                with closing(_transfer(client, "GET", current, timeout, stream=True)) as response:
                    if response.is_redirect:
                        if redirect == 5 or "location" not in response.headers:
                            raise PipelineError("parse", "invalid_download_redirect")
                        current = str(response.url.join(response.headers["location"]))
                        continue
                    if not response.is_success:
                        raise status_error(response, "parse")
                    size = 0
                    with partial.open("wb") as output:
                        for chunk in response.iter_bytes(1024 * 1024):
                            size += len(chunk)
                            if size > max_bytes:
                                raise PipelineError("parse", "provider_archive_limit_exceeded")
                            output.write(chunk)
                    partial.replace(target)
                    return
        except httpx.TransportError:
            error = PipelineError("parse", "transport_error", True)
        except (httpx.InvalidURL, httpx.DecodingError):
            error = PipelineError("parse", "invalid_download_response")
        except PipelineError as exc:
            error = exc
        finally:
            partial.unlink(missing_ok=True)
        if not error.retryable or attempt + 1 == RETRY_ATTEMPTS:
            raise error
        time.sleep(RETRY_DELAY_SECONDS * 2 ** attempt)


def _poll_rows(data: dict, submitted: dict[str, _Submission]) -> dict[str, dict]:
    rows = data.get("extract_result")
    if not isinstance(rows, list):
        raise PipelineError("parse", "invalid_provider_response")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict):
            raise PipelineError("parse", "invalid_provider_response")
        data_id = row.get("data_id")
        if not isinstance(data_id, str) or data_id not in submitted or data_id in indexed:
            raise PipelineError("parse", "provider_identity_mismatch")
        state = row.get("state")
        if not isinstance(state, str) or state not in _PENDING_STATES | {"done", "failed"}:
            raise PipelineError("parse", "invalid_provider_response")
        indexed[data_id] = row
    return indexed


def obtain_archives(sources: list[tuple[int, InputFile]], config: Config, client: httpx.Client,
                    work: Path) -> list[ArchiveSuccess | ArchiveFailure]:
    start = time.monotonic()
    deadline = start + config.mineru.timeout_seconds
    submitted = {item.observation.data_id: item for item in (_Submission(*source) for source in sources)}
    pending = submitted.copy()
    downloads: list[tuple[_Submission, str, float]] = []
    results: dict[int, ArchiveSuccess | ArchiveFailure] = {}
    options = config.mineru.parse_options()
    is_ocr = options.pop("is_ocr")
    files = [{"name": item.source.path.name, "data_id": data_id, "is_ocr": is_ocr}
             for data_id, item in submitted.items()]

    def fail(item: _Submission, error: PipelineError, elapsed: float) -> None:
        results[item.index] = ArchiveFailure(item.index, item.observation, elapsed, error, RunInstant.now())

    try:
        data = _api(client, config, deadline, "POST", "/api/v4/file-urls/batch",
                    json={**options, "files": files})
        urls, batch_id = data.get("file_urls"), data.get("batch_id")
        if (not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", batch_id)
                or batch_id in {".", ".."} or not isinstance(urls, list) or len(urls) != len(files)):
            raise PipelineError("parse", "invalid_provider_response")
        for item in submitted.values():
            item.observation.batch_id = batch_id
        for (data_id, item), url in zip(submitted.items(), urls, strict=True):
            timeout = bounded_timeout(config.request_timeout_seconds, deadline)
            try:
                if not isinstance(url, str):
                    raise PipelineError("parse", "invalid_provider_response")
                _upload(client, url, item.source.path, timeout)
            except PipelineError as error:
                fail(pending.pop(data_id), error, time.monotonic() - start)
            except httpx.InvalidURL:
                fail(pending.pop(data_id), PipelineError("parse", "unsafe_provider_url"),
                     time.monotonic() - start)
        route = "/api/v4/extract-results/batch/" + quote(batch_id, safe="")
        while pending:
            data = _api(client, config, deadline, "GET", route)
            if "batch_id" in data and data["batch_id"] != batch_id:
                raise PipelineError("parse", "provider_identity_mismatch")
            for data_id, row in _poll_rows(data, submitted).items():
                if data_id not in pending:
                    continue
                item = pending[data_id]
                item.observation.observe_progress(row.get("extract_progress"))
                state = row["state"]
                if state in _PENDING_STATES:
                    continue
                pending.pop(data_id)
                elapsed = time.monotonic() - start
                if state == "failed":
                    fail(item, PipelineError("parse", "mineru_parse_failed"), elapsed)
                elif not isinstance(row.get("full_zip_url"), str):
                    fail(item, PipelineError("parse", "invalid_provider_response"), elapsed)
                else:
                    downloads.append((item, row["full_zip_url"], elapsed))
            if pending:
                time.sleep(bounded_timeout(POLL_INTERVAL_SECONDS, deadline))
    except PipelineError as error:
        elapsed = time.monotonic() - start
        for item in pending.values():
            fail(item, error, elapsed)

    for item, url, elapsed in downloads:
        archive = work / f"{item.index}.zip"
        started = time.monotonic()
        try:
            _download(client, url, archive, config.request_timeout_seconds)
        except PipelineError as error:
            fail(item, error, elapsed + time.monotonic() - started)
        except OSError:
            fail(item, PipelineError("parse", "download_io_error"), elapsed + time.monotonic() - started)
        else:
            results[item.index] = ArchiveSuccess(
                item.index, item.observation, elapsed + time.monotonic() - started, archive,
            )
    return [results[index] for index, _ in sources]
