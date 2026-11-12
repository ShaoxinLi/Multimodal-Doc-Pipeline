import os
import re
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import httpx

from doc_pipeline.contracts.config import Config, Limits, api_key
from doc_pipeline.contracts.core import InputFile, PipelineError, UrlInput
from doc_pipeline.contracts.observations import (
    MinerUIssueCallback,
    MinerUObservation,
    MinerURequests,
    RequestCounts,
    ServiceIssueCallback,
)
from doc_pipeline.contracts.run import (
    ProgressCallback,
    ProgressEvent,
    ProgressStage,
    RunInstant,
)
from doc_pipeline.services.io import (
    RETRY_ATTEMPTS,
    RETRY_DELAY_SECONDS,
    bounded_timeout,
    provider_reason,
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
    "-60023": _MinerUErrorSpec("mineru_url_fetch_failed", False),
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


class _RejectedSourceUrls(PipelineError):
    pass


@dataclass
class _Submission:
    index: int
    source: InputFile | UrlInput
    observation: MinerUObservation = field(default_factory=lambda: MinerUObservation(uuid.uuid4().hex))


@dataclass
class _AcceptedBatch:
    id: str
    submitted: dict[str, _Submission]
    pending: dict[str, _Submission]


def _api(client, config: Config, deadline: float, method, route, *,
         requests: RequestCounts, on_issue: ServiceIssueCallback | None = None,
         direct: bool = False, **kwargs):
    key = api_key("MINERU_API_KEY", config.mineru.api_key)
    if not key:
        raise PipelineError("parse", "missing_mineru_credentials")
    value = request_json(client, "parse", method,
                         config.mineru.api_base_url + route,
                         timeout=config.request_timeout_seconds, deadline=deadline,
                         retry_transport=method != "POST", retry_http=not direct or method != "POST",
                         include_error_reason=not direct,
                         requests=requests, on_issue=on_issue,
                         headers={"Authorization": "Bearer " + key}, **kwargs)
    provider_code, data = value.get("code"), value.get("data")
    if type(provider_code) is not int or provider_code != 0:
        error = _MINERU_REQUEST_REJECTED
        if type(provider_code) is int or type(provider_code) is str:
            error = _MINERU_ERRORS.get(str(provider_code), error)
        if (direct and method == "POST" and error.code == "mineru_url_fetch_failed"
                and (data is None or isinstance(data, dict) and data.get("batch_id") in (None, ""))):
            raise _RejectedSourceUrls("parse", error.code, error.retryable,
                                      reason="MinerU cannot fetch one or more source URLs from its region")
        reason = None if direct else provider_reason(value.get("err_msg", value.get("msg")), secrets=(key,))
        raise PipelineError("parse", error.code, error.retryable, reason=reason)
    if not isinstance(data, dict):
        raise PipelineError("parse", "invalid_provider_response")
    return data


def _transfer(client, method, url, timeout, *, requests: RequestCounts, stream=False, **kwargs):
    # Build the request directly so client defaults never reach signed endpoints.
    request = httpx.Request(method, transfer_url(url),
                            extensions={"timeout": httpx.Timeout(timeout).as_dict()}, **kwargs)
    with suppress_http_logging():
        requests.requests += 1
        response = client.send(request, auth=None, follow_redirects=False, stream=True)
        requests.responses += 1
        if not stream:
            try:
                response.read()
            except BaseException:
                response.close()
                raise
        return response


def _upload(client, url: str, path: Path, timeout: float, *, requests: RequestCounts) -> None:
    try:
        with path.open("rb") as stream:
            with closing(_transfer(client, "PUT", url, timeout, requests=requests, content=stream,
                                   headers={"Content-Length": str(os.fstat(stream.fileno()).st_size)})) as response:
                if not response.is_success:
                    raise status_error(response, "parse")
    except httpx.TransportError:
        raise PipelineError("parse", "upload_failed", True) from None
    except httpx.DecodingError:
        raise PipelineError("parse", "invalid_upload_response") from None
    except OSError:
        raise PipelineError("parse", "upload_io_error") from None


def _download(client, url: str, target: Path, timeout: float, *,
              requests: RequestCounts, on_issue: ServiceIssueCallback | None = None,
              include_error_reason: bool = True) -> None:
    partial = target.with_suffix(".part")
    max_bytes = Limits().max_zip_bytes
    retry_issue = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            if retry_issue is not None and on_issue is not None:
                on_issue(retry_issue.code, retry_issue.reason, "retried", retry_issue.retryable)
            current = url
            for redirect in range(6):
                with closing(_transfer(client, "GET", current, timeout, requests=requests, stream=True)) as response:
                    if response.is_redirect:
                        if redirect == 5 or "location" not in response.headers:
                            raise PipelineError("parse", "invalid_download_redirect")
                        current = str(response.url.join(response.headers["location"]))
                        continue
                    if not response.is_success:
                        raise status_error(response, "parse", include_reason=include_error_reason)
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
        retry_issue = error
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


def obtain_archives(sources: list[tuple[int, InputFile | UrlInput]], config: Config, client: httpx.Client,
                    work: Path, *, on_progress: ProgressCallback | None = None,
                    requests: MinerURequests | None = None,
                    on_issue: MinerUIssueCallback | None = None,
                    on_observation: Callable[[int, MinerUObservation], None] | None = None,
                    on_outcome: Callable[[ArchiveSuccess | ArchiveFailure], None] | None = None,
                    ) -> list[ArchiveSuccess | ArchiveFailure]:
    start = time.monotonic()
    requests = requests if requests is not None else MinerURequests()
    deadline = start + config.mineru.timeout_seconds
    submissions = [_Submission(*source) for source in sources]
    direct = bool(submissions and isinstance(submissions[0].source, UrlInput))
    submit_route = "/api/v4/extract/task/batch" if direct else "/api/v4/file-urls/batch"
    downloads: list[tuple[_Submission, str]] = []
    results: dict[int, ArchiveSuccess | ArchiveFailure] = {}
    options = config.mineru.parse_options()
    is_ocr = options.pop("is_ocr")

    def fail(item: _Submission, error: PipelineError, elapsed: float) -> None:
        results[item.index] = ArchiveFailure(item.index, item.observation, elapsed, error, RunInstant.now())
        if on_outcome is not None:
            on_outcome(results[item.index])

    def observe(item: _Submission) -> None:
        if on_observation is not None:
            on_observation(item.index, item.observation)

    def progress(item: _Submission, stage: ProgressStage, completed: int | None = None,
                 total: int | None = None) -> None:
        if on_progress is not None:
            path = Path(item.source.filename) if isinstance(item.source, UrlInput) else item.source.path
            on_progress(ProgressEvent(item.index, path, stage, completed, total))

    for item in submissions:
        observe(item)
        progress(item, "remote_parse" if direct else "upload")

    def issues_for(pending: dict[str, _Submission]) -> ServiceIssueCallback:
        def notify(code: str, reason: str | None, action: str, retryable: bool) -> None:
            if on_issue is not None:
                for item in pending.values():
                    on_issue(item.index, code, reason, action, retryable)
        return notify

    accepted = []

    def submit(group: list[_Submission]) -> None:
        submitted = {item.observation.data_id: item for item in group}
        pending = submitted.copy()
        notify = issues_for(pending)
        files = []
        for data_id, item in submitted.items():
            file = {"url": item.source.request_url} if isinstance(item.source, UrlInput) else {
                "name": item.source.path.name,
            }
            files.append({**file, "data_id": data_id, "is_ocr": is_ocr})

        try:
            data = _api(client, config, deadline, "POST", submit_route, requests=requests.submit,
                        on_issue=notify, direct=direct, json={**options, "files": files})
            urls, batch_id = data.get("file_urls"), data.get("batch_id")
            if (not isinstance(batch_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", batch_id)
                    or batch_id in {".", ".."}):
                raise PipelineError("parse", "invalid_provider_response")
            if not direct and (not isinstance(urls, list) or len(urls) != len(files)):
                raise PipelineError("parse", "invalid_provider_response")
            for item in submitted.values():
                item.observation.batch_id = batch_id
                observe(item)
            if not direct:
                for (data_id, item), url in zip(submitted.items(), urls, strict=True):
                    timeout = bounded_timeout(config.request_timeout_seconds, deadline)
                    try:
                        if not isinstance(url, str):
                            raise PipelineError("parse", "invalid_provider_response")
                        _upload(client, url, item.source.path, timeout, requests=requests.upload)
                        progress(item, "remote_parse")
                    except PipelineError as error:
                        fail(pending.pop(data_id), error, time.monotonic() - start)
                    except httpx.InvalidURL:
                        fail(pending.pop(data_id), PipelineError("parse", "unsafe_provider_url"),
                             time.monotonic() - start)
        except PipelineError as error:
            if isinstance(error, _RejectedSourceUrls) and len(group) > 1:
                notify(error.code, error.reason, "split", error.retryable)
                mid = len(group) // 2
                submit(group[:mid])
                submit(group[mid:])
            else:
                elapsed = time.monotonic() - start
                for item in pending.values():
                    fail(item, error, elapsed)
        else:
            if pending:
                accepted.append(_AcceptedBatch(batch_id, submitted, pending))

    def poll(batch: _AcceptedBatch) -> None:
        route = "/api/v4/extract-results/batch/" + quote(batch.id, safe="")
        pending = batch.pending
        try:
            data = _api(client, config, deadline, "GET", route, requests=requests.poll,
                        on_issue=issues_for(pending), direct=direct)
            if "batch_id" in data and data["batch_id"] != batch.id:
                raise PipelineError("parse", "provider_identity_mismatch")
            for data_id, row in _poll_rows(data, batch.submitted).items():
                if data_id not in pending:
                    continue
                item = pending[data_id]
                observation = item.observation
                previous = (observation.extracted_pages, observation.total_pages)
                observation.observe_progress(row.get("extract_progress"))
                if (observation.extracted_pages, observation.total_pages) != previous:
                    observe(item)
                state = row["state"]
                if state in _PENDING_STATES:
                    pages = (observation.extracted_pages, observation.total_pages)
                    if pages != previous:
                        progress(item, "remote_parse", *pages)
                    continue
                pending.pop(data_id)
                elapsed = time.monotonic() - start
                if state == "failed":
                    key = api_key("MINERU_API_KEY", config.mineru.api_key)
                    reason = None if direct else provider_reason(row.get("err_msg"), secrets=(key,))
                    fail(item, PipelineError("parse", "mineru_parse_failed", reason=reason), elapsed)
                elif not isinstance(row.get("full_zip_url"), str):
                    fail(item, PipelineError("parse", "invalid_provider_response"), elapsed)
                else:
                    downloads.append((item, row["full_zip_url"]))
        except PipelineError as error:
            elapsed = time.monotonic() - start
            for item in pending.values():
                fail(item, error, elapsed)
            pending.clear()

    if submissions:
        submit(submissions)
    while accepted:
        for batch in accepted:
            poll(batch)
        accepted = [batch for batch in accepted if batch.pending]
        if accepted:
            time.sleep(max(0, min(POLL_INTERVAL_SECONDS, deadline - time.monotonic())))

    for item, url in downloads:
        progress(item, "download")
        archive = work / f"{item.index}.zip"

        def download_issue(code: str, reason: str | None, action: str, retryable: bool) -> None:
            if on_issue is not None:
                on_issue(item.index, code, reason, action, retryable)

        try:
            _download(client, url, archive, config.request_timeout_seconds,
                      requests=requests.download, on_issue=download_issue, include_error_reason=not direct)
        except PipelineError as error:
            fail(item, error, time.monotonic() - start)
        except OSError:
            fail(item, PipelineError("parse", "download_io_error"), time.monotonic() - start)
        else:
            results[item.index] = ArchiveSuccess(
                item.index, item.observation, time.monotonic() - start, archive,
            )
            if on_outcome is not None:
                on_outcome(results[item.index])
        observe(item)
    return [results[index] for index, _ in sources]
