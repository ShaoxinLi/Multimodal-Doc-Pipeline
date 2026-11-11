import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta
from threading import Barrier, BrokenBarrierError, Event, Lock
from types import SimpleNamespace

import httpx
import pytest
from docutils.parsers.rst import roles

from doc_pipeline import Config, InputFile, PipelineError, pipeline, process_files
from doc_pipeline.cli import main
from doc_pipeline.contracts import run as run_contract
from doc_pipeline.contracts.observations import MinerURequests
from doc_pipeline.parsers.multimodal import client as provider_client
from doc_pipeline.parsers.multimodal.client import ArchiveFailure, ArchiveSuccess, obtain_archives
from tests.support import (
    assert_batch_output,
    document_archive,
    failure_issue,
    pdf_bytes,
    read_batch_reports,
    read_output,
    read_report,
    write_source_pdf,
)


def ok(**data):
    return httpx.Response(200, json={"code": 0, "data": data})


class BatchService:
    def __init__(self, *, poll=None, upload_failure=None, download_failure=None, archives=None):
        self.poll = poll or (lambda service: service.done())
        self.upload_failure = upload_failure
        self.download_failure = download_failure
        self.archives = archives or {}
        self.default_archive = document_archive(pdf_bytes())
        self.payloads = []
        self.calls = []
        self.polls = 0

    def row(self, index, state="done", **fields):
        row = {"data_id": self.payloads[-1]["files"][index]["data_id"], "state": state, **fields}
        if state == "done":
            row["full_zip_url"] = f"https://signed.test/download/{index}?secret=read-secret"
        return row

    def done(self, *indexes):
        return [self.row(index) for index in (indexes or range(len(self.payloads[-1]["files"])))]

    def handle(self, request):
        method, host, path = request.method, request.url.host, request.url.path
        self.calls.append((method, host, path))
        assert request.extensions["timeout"]["read"] > 0
        if host == "signed.test":
            assert "authorization" not in request.headers
            assert "cookie" not in request.headers
            assert "x-default" not in request.headers
            assert "client_default" not in request.url.params
            index = int(path.rsplit("/", 1)[1])
            if method == "PUT":
                assert request.url.params["secret"] == "write-secret"
                assert request.content.startswith(b"%PDF-")
                assert int(request.headers["Content-Length"]) == len(request.content)
                return httpx.Response(403 if index == self.upload_failure else 200)
            assert request.url.params["secret"] == "read-secret"
            if index == self.download_failure:
                return httpx.Response(404, text="private download error")
            return httpx.Response(200, content=self.archives.get(index, self.default_archive))
        assert host == "mineru.test"
        assert request.headers["Authorization"] == "Bearer mineru-secret"
        if method == "POST":
            self.payloads.append(json.loads(request.content))
            self.polls = 0
            files = self.payloads[-1]["files"]
            return ok(batch_id=f"batch-{len(self.payloads)}",
                      file_urls=[f"https://signed.test/upload/{index}?secret=write-secret"
                                 for index in range(len(files))])
        assert path == f"/api/v4/extract-results/batch/batch-{len(self.payloads)}"
        self.polls += 1
        return ok(extract_result=self.poll(self))


def run_batch(paths, output, provider, *, on_progress=None, **overrides):
    config = Config(request_timeout_seconds=12, mineru={
        "api_base_url": "https://mineru.test", "api_key": "mineru-secret", **overrides,
    })
    with httpx.Client(transport=httpx.MockTransport(provider.handle),
                      auth=("default-user", "default-key"), cookies={"default": "default-cookie"},
                      headers={"X-Default": "default"}, params={"client_default": "default"}) as http:
        run = process_files(paths, output, config, client=http, on_progress=on_progress)
        assert not http.is_closed
    assert json.loads((output / "run.json").read_text()) == run
    return run


def test_mixed_batch_uses_one_shared_poll_and_retains_order_and_progress(tmp_path, monkeypatch, caplog):
    source_pdf = write_source_pdf(tmp_path)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    monkeypatch.setattr(provider_client.time, "sleep", lambda _: None)
    caplog.set_level(logging.DEBUG)
    first, second = tmp_path / "a" / "same.pdf", tmp_path / "b" / "same.pdf"
    for path in (first, second):
        path.parent.mkdir()
        path.write_bytes(source_pdf.read_bytes())
    note = tmp_path / "note.txt"
    note.write_bytes("café".encode("latin-1"))

    def poll(service):
        if service.polls == 1:
            return [service.row(1, "running", extract_progress={"total_pages": 2}),
                    service.row(0, "running", extract_progress={"total_pages": 2, "extracted_pages": 1})]
        if service.polls == 2:
            return [service.row(1, "running", extract_progress={"extracted_pages": 1, "total_pages": True}),
                    service.row(0, "running", extract_progress={"total_pages": 1, "extracted_pages": 4})]
        return service.done(2, 0, 1)

    service = BatchService(poll=poll)
    output = tmp_path / "out"
    progress = []
    run = run_batch([first, InputFile(path=note, encoding="latin-1"), second, first], output, service,
                    model_version="pipeline", is_ocr=True, enable_formula=False, enable_table=False,
                    language="en", on_progress=progress.append)
    for index, path in ((0, first), (2, second)):
        updates = [event for event in progress if event.index == index]
        assert all(event.path == path for event in updates)
        assert updates[0].stage == "intake"
        assert updates[-1].stage == "complete"
        assert any(event.stage == "remote_parse" and (event.completed, event.total) == (1, 2)
                   for event in updates)
        assert any(event.stage == "download" for event in updates)
    assert [event.stage for event in progress if event.index == 1] == ["intake", "parse", "complete"]
    [payload] = service.payloads
    assert {name: payload[name] for name in payload if name != "files"} == {
        "model_version": "pipeline", "enable_formula": False, "enable_table": False, "language": "en",
    }
    assert [item["name"] for item in payload["files"]] == ["same.pdf"] * 3
    assert all(item["is_ocr"] is True for item in payload["files"])
    assert len({item["data_id"] for item in payload["files"]}) == 3
    assert [method for method, _, _ in service.calls] == [
        "POST", "PUT", "PUT", "PUT", "GET", "GET", "GET", "GET", "GET", "GET",
    ]
    assert run["schema_version"] == 2
    assert set(run) == {"schema_version", "run", "settings", "mineru_requests", "files"}
    assert run["run"]["state"] == "completed"
    assert run["files"] == [
        {"index": index, "report": f"{index + 1:04d}/report.json", "unreported": "n/a"}
        for index in range(4)
    ]
    assert run["mineru_requests"] == {
        "submit": {"requests": 1, "responses": 1},
        "poll": {"requests": 3, "responses": 3},
        "upload": {"requests": 3, "responses": 3},
        "download": {"requests": 3, "responses": 3},
    }
    assert (output / "0002" / "content.md").read_text().strip() == "café"
    reports = read_batch_reports(output, run)
    assert [report["source"]["uri"] for report in reports] == [path.as_uri() for path in (first, note, second, first)]
    assert reports[1]["stages"]["intake"]["encoding"] == "latin-1"
    for index in (0, 2):
        observation = reports[index]["stages"]["acquire"]["service"]
        assert observation["progress"] == {"total_pages": 2, "extracted_pages": 1}
        assert observation["requested"]["model_version"] == "pipeline"
    for index, report in enumerate(reports):
        assert report["run"]["id"] == run["run"]["id"]
        assert report["run"]["index"] == index
        assert report["run"]["elapsed_seconds"] >= 0
        stages = report["stages"]
        assert all(stages[stage]["state"] == "succeeded" for stage in ("intake", "parse", "render", "chunk", "write"))
        assert stages["acquire"]["state"] == ("not_applicable" if index == 1 else "succeeded")
    events = [json.loads(message.removeprefix("document_finished ")) for message in caplog.messages
              if message.startswith("document_finished ")]
    assert sorted(event["index"] for event in events) == [0, 1, 2, 3]
    for event in events:
        report = reports[event["index"]]
        assert event["id"] == report["run"]["id"]
        assert event["elapsed_seconds"] == report["run"]["elapsed_seconds"]
        assert event["stages"]["acquire"]["seconds"] == report["stages"]["acquire"]["seconds"]
    for private in ("mineru-secret", "default-key", "default-cookie", "write-secret", "read-secret",
                    "https://signed.test", "Pipeline probe section 1", "café"):
        assert private not in caplog.text
        assert private not in (output / "run.json").read_text()
    assert_batch_output(output, run)


def test_terminal_records_survive_delayed_batch_collection(tmp_path, monkeypatch, caplog):
    source_pdf = write_source_pdf(tmp_path)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    caplog.set_level(logging.INFO)
    elapsed = 0.0
    monkeypatch.setattr(run_contract.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(provider_client.time, "sleep", lambda _: None)
    monkeypatch.setattr(run_contract, "datetime", SimpleNamespace(
        now=lambda tz: datetime(2026, 1, 1, tzinfo=tz) + timedelta(seconds=elapsed),
    ))
    local_futures = []

    class ObservedExecutor(ThreadPoolExecutor):
        def submit(self, fn, /, *args, **kwargs):
            future = super().submit(fn, *args, **kwargs)
            local_futures.append(future)
            return future

    def poll(service):
        nonlocal elapsed
        if service.polls == 1:
            for future in local_futures:
                future.result(timeout=10)
            elapsed += 2
            return [service.row(0, "failed"), service.row(1, "running")]
        elapsed += 20
        return service.done(1)

    monkeypatch.setattr(pipeline, "ThreadPoolExecutor", ObservedExecutor)
    note, invalid = tmp_path / "note.md", tmp_path / "invalid.txt"
    note.write_text("Body")
    invalid.write_bytes(b"\xff")
    output = tmp_path / "out"
    run = run_batch([note, invalid, source_pdf, source_pdf], output, BatchService(poll=poll))
    reports = read_batch_reports(output, run)
    complete, failed, rejected, _ = reports
    events = {event["index"]: event for event in (
        json.loads(message.removeprefix("document_finished ")) for message in caplog.messages
        if message.startswith("document_finished ")
    )}

    assert complete["run"]["state"] == "completed"
    assert failure_issue(failed, "parse")["code"] == "native_decode_error"
    assert failure_issue(rejected, "acquire")["code"] == "mineru_parse_failed"
    assert run["run"]["elapsed_seconds"] == 22
    for report, second in zip(reports, (0, 0, 2, 22), strict=True):
        event = events[report["run"]["index"]]
        assert report["run"]["elapsed_seconds"] == event["elapsed_seconds"] == second
        assert report["stages"]["parse"]["seconds"] == event["stages"]["parse"]["seconds"]


def test_fifty_file_limit_and_later_group_survives_failed_allocation(tmp_path, monkeypatch):
    source_pdf = write_source_pdf(tmp_path)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)

    class BrokenFirstAllocation(BatchService):
        def handle(self, request):
            response = super().handle(request)
            if request.method == "POST" and len(self.payloads) == 1:
                return ok(batch_id="private invalid id", file_urls=[])
            return response

    service = BrokenFirstAllocation()
    output = tmp_path / "out"
    run = run_batch([source_pdf] * 51, output, service)
    assert [len(payload["files"]) for payload in service.payloads] == [50, 1]
    assert run["mineru_requests"]["submit"] == {"requests": 2, "responses": 2}
    reports = read_batch_reports(output, run)
    assert all(report["run"]["state"] == "failed" for report in reports[:50])
    assert all(failure_issue(report, "acquire")["code"] == "invalid_provider_response" for report in reports[:50])
    assert reports[50]["run"]["state"] == "completed"
    assert reports[50]["stages"]["acquire"]["service"]["batch_id"] == "batch-2"


@pytest.mark.parametrize("fault", ["unknown", "duplicate", "malformed"])
def test_untrusted_poll_rows_fail_only_unresolved_documents(tmp_path, monkeypatch, fault):
    source_pdf = write_source_pdf(tmp_path)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    monkeypatch.setattr(provider_client.time, "sleep", lambda _: None)

    def poll(service):
        if service.polls == 1:
            return service.done(1)
        row = service.row(0)
        if fault == "unknown":
            return [row, {**row, "data_id": "unexpected"}]
        if fault == "duplicate":
            return [row, row]
        return [row, None]

    service = BatchService(poll=poll)
    output = tmp_path / "out"
    run = run_batch([source_pdf] * 3, output, service)
    reports = read_batch_reports(output, run)
    assert [report["run"]["state"] for report in reports] == ["failed", "completed", "failed"]
    code = "invalid_provider_response" if fault == "malformed" else "provider_identity_mismatch"
    assert failure_issue(reports[0], "acquire")["code"] == failure_issue(reports[2], "acquire")["code"] == code
    downloads = [path for method, host, path in service.calls if method == "GET" and host == "signed.test"]
    assert downloads == ["/download/1"]


def test_timeout_preserves_ready_documents_and_acquisition_includes_download_queue(tmp_path, monkeypatch):
    source_pdf = write_source_pdf(tmp_path)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    elapsed = [0.0]
    monkeypatch.setattr(provider_client.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(provider_client.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))

    class SlowDownloadService(BatchService):
        def handle(self, request):
            expected_timeout = 1 if request.url.host == "mineru.test" or request.method == "PUT" else 12
            assert request.extensions["timeout"]["read"] == expected_timeout
            if request.url.path.startswith("/download/"):
                elapsed[0] += 10
            return super().handle(request)

    service = SlowDownloadService(poll=lambda service: service.done(0, 1))
    output = tmp_path / "out"
    run = run_batch([source_pdf] * 3, output, service, timeout_seconds=1)
    reports = read_batch_reports(output, run)
    assert [report["run"]["state"] for report in reports] == ["completed", "completed", "failed"]
    issue = failure_issue(reports[2], "acquire")
    assert (issue["code"], issue["retryable"]) == ("parse_timeout", True)
    assert [report["stages"]["acquire"]["seconds"] for report in reports] == [11, 21, 1]


def test_per_file_failures_preserve_reports_and_successful_sibling(tmp_path, monkeypatch):
    source_pdf = write_source_pdf(tmp_path)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    original_install = pipeline.install_output

    def fail_install(staging, destination, **kwargs):
        if destination == output / "0006":
            assert read_report(destination)["run"]["state"] == "running"
            assert not (destination / "content.md").exists()
            assert (staging / "content.md").is_file()
            (staging / "chunks.jsonl").write_text("unfinished")
            raise OSError("private local path or error")
        return original_install(staging, destination, **kwargs)

    monkeypatch.setattr(pipeline, "install_output", fail_install)
    service = BatchService(
        poll=lambda service: [service.row(1, "failed", err_msg="private provider error"), *service.done(2, 3, 4, 5)],
        upload_failure=0, download_failure=2, archives={3: b"invalid ZIP"},
    )
    output = tmp_path / "out"
    run = run_batch([tmp_path / "missing.pdf", *([source_pdf] * 6)], output, service)
    reports = read_batch_reports(output, run)
    for index, (stage, code) in enumerate((
        ("intake", "io_error"), ("acquire", "authentication_failed"), ("acquire", "mineru_parse_failed"),
        ("acquire", "http_404"), ("parse", "invalid_provider_archive"), ("write", "io_error"),
    ), 1):
        report = reports[index - 1]
        assert report["run"]["state"] == "failed"
        assert failure_issue(report, stage)["code"] == code
        assert report["result"] == {"chunks": None, "locations": None, "embedding": "n/a"}
        directory = output / f"{index:04d}"
        assert set(directory.iterdir()) == {directory / "report.json"}
    assert reports[6]["run"]["state"] == "completed"
    assert (output / "0007" / "content.md").is_file()
    documents = json.dumps(reports)
    assert all(message not in documents for message in ("private local path or error", "private download error"))


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_local_worker_bound_failure_isolation_and_result_order(tmp_path, monkeypatch, workers):
    from doc_pipeline.parsers.text import parser

    original_parse = parser.parse_text_document
    lock = Lock()
    active = maximum = 0
    first_group = Barrier(workers + 1)
    release = Event()

    def parse(source, *args, **kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            if int(source.path.stem) < workers:
                first_group.wait(timeout=10)
                assert release.wait(timeout=10)
            return original_parse(source, *args, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(parser, "parse_text_document", parse)
    paths = [tmp_path / f"{index}.{'md' if index % 2 else 'txt'}" for index in range(7)]
    for index, path in enumerate(paths):
        path.write_bytes(f"Body {index}".encode() if index != 3 else b"\xff")
    output = tmp_path / "out"
    with ThreadPoolExecutor(max_workers=1) as controller:
        future = controller.submit(process_files, paths, output, Config(local_workers=workers))
        try:
            first_group.wait(timeout=10)
            assert active == workers
        finally:
            release.set()
        run = future.result(timeout=10)
    assert maximum == workers
    reports = read_batch_reports(output, run)
    assert [report["source"]["uri"] for report in reports] == [path.as_uri() for path in paths]
    assert [report["run"]["state"] for report in reports].count("failed") == 1
    assert failure_issue(reports[3], "parse")["code"] == "native_decode_error"
    assert run["mineru_requests"] == "n/a"
    assert all(report["stages"]["acquire"]["service"] == "n/a" for report in reports)
    assert set((output / "0004").iterdir()) == {output / "0004" / "report.json"}


def test_local_worker_cannot_auto_archive_a_sibling_output_asset(tmp_path):
    first, second = tmp_path / "first.md", tmp_path / "second.md"
    first.write_text("![image](out/0002/assets/image.png)\n")
    second.write_text("Plain text.")
    output = tmp_path / "out"
    process_files([first, second], output)
    report, _, _ = read_output(output / "0001")
    assert report["issues"] == [{
        "stage": "parse", "code": "asset_unavailable", "reason": "blocked_reference", "action": "unarchived",
        "retryable": "n/a", "occurrences": 1, "targets": [
            {"unit": "asset", "values": ["out/0002/assets/image.png"]},
        ], "unlocated": 0,
    }]


def test_concurrent_rst_documents_keep_independent_output(tmp_path, monkeypatch):
    cleanup = Barrier(2)

    class InterleavedRoles(dict):
        def __delitem__(self, key):
            if key == "":
                try:
                    cleanup.wait(timeout=1)
                except BrokenBarrierError:
                    pass
            super().__delitem__(key)

    monkeypatch.setattr(roles, "_roles", InterleavedRoles(roles._roles))
    first, second = tmp_path / "first.rst", tmp_path / "second.rst"
    first.write_text("Read `Alpha reference`.\n")
    second.write_text("Read `Beta reference`.\n")
    output = tmp_path / "out"
    run = process_files([first, second], output, Config(local_workers=2))

    assert run["run"]["state"] == "completed"
    for item, expected in zip(run["files"], ("Alpha reference", "Beta reference"), strict=True):
        directory = (output / item["report"]).parent
        report, [chunk], _ = read_output(directory)
        assert report["run"]["state"] == "completed"
        assert report["result"]["chunks"] == {"parsed": 1, "partial": 0, "unparsed": 0}
        assert chunk["body"] == f"Read {expected}."
        assert expected in (directory / "content.md").read_text()


def test_interrupted_batch_finishes_running_local_work_and_cancels_queue(tmp_path, monkeypatch, capsys):
    source_pdf = write_source_pdf(tmp_path)
    from doc_pipeline.parsers.text import parser

    entered, release = Event(), Event()
    active, queued = tmp_path / "active.txt", tmp_path / "queued.txt"
    active.write_text("Running document.")
    queued.write_text("Queued document.")
    output = tmp_path / "out"
    config = tmp_path / "config.toml"
    config.write_text("local_workers = 1\n")
    original_parse = parser.parse_text_document

    def parse(source, *args, **kwargs):
        if source.path.name == active.name:
            entered.set()
            assert release.wait(timeout=10)
        return original_parse(source, *args, **kwargs)

    def interrupt(*args, **kwargs):
        assert entered.wait(timeout=10)
        raise KeyboardInterrupt

    class ObservedExecutor(ThreadPoolExecutor):
        def shutdown(self, wait=True, *, cancel_futures=False):
            super().shutdown(wait=False, cancel_futures=cancel_futures)
            release.set()
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(parser, "parse_text_document", parse)
    monkeypatch.setattr(pipeline, "obtain_archives", interrupt)
    monkeypatch.setattr(pipeline, "ThreadPoolExecutor", ObservedExecutor)
    arguments = [str(active), str(queued), str(source_pdf),
                 "--out", str(output), "--config", str(config)]
    assert main(arguments) == 130
    assert json.loads(capsys.readouterr().out) == {"error": "interrupted"}
    run = json.loads((output / "run.json").read_text())
    assert run["run"]["state"] == "interrupted"
    reports = read_batch_reports(output, run)
    assert [report["run"]["state"] for report in reports] == ["completed", "interrupted", "interrupted"]
    for directory in (output / "0002", output / "0003"):
        assert set(directory.iterdir()) == {directory / "report.json"}


def test_empty_batch_rejected(tmp_path):
    output = tmp_path / "out"
    with pytest.raises(PipelineError, match="no_input_files"):
        process_files([], output)
    assert not output.exists()


@pytest.mark.parametrize("model", ["pipeline", "vlm"])
def test_batch_sends_model_option_and_keeps_unverified_pages_unavailable(tmp_path, monkeypatch, model):
    source_pdf = write_source_pdf(tmp_path)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    service = BatchService()
    output = tmp_path / "out"
    run = run_batch([source_pdf], output, service, model_version=model)
    [payload] = service.payloads
    assert payload["model_version"] == model
    assert run["run"]["state"] == "completed"
    bundle = output / "0001"
    report, chunks, _ = read_output(bundle)
    assert report["run"]["state"] == "completed"
    assert report["result"]["locations"] == {"known": 0, "partial": 0, "unavailable": len(chunks)}
    location_issues = [issue for issue in report["issues"] if issue["code"] == "source_location_unresolved"]
    assert [(issue["reason"], issue["action"], issue["occurrences"]) for issue in location_issues] == [
        ("original_pdf_page_unverified", "unverified", 2), ("provider_block_match_ambiguous", "unverified", 2),
    ]
    assert chunks
    for chunk in chunks:
        assert chunk["citations"]["pages"] is None
        assert chunk["citations"]["location_status"] == "unavailable"
    assert not (bundle / "native").exists()
    assert not (bundle / "source").exists()
    assert "Pipeline probe section 1" in (bundle / "content.md").read_text()


def test_batch_indexes_failed_reports_without_copying_their_facts(tmp_path):
    source = tmp_path / "good.txt"
    source.write_text("Body")
    missing = tmp_path / "missing.txt"
    output = tmp_path / "out"
    batch = process_files([source, missing], output)
    assert set(batch) == {"schema_version", "run", "settings", "mineru_requests", "files"}
    assert batch["run"]["state"] == "completed"
    assert batch["mineru_requests"] == "n/a"
    assert batch["files"] == [
        {"index": 0, "report": "0001/report.json", "unreported": "n/a"},
        {"index": 1, "report": "0002/report.json", "unreported": "n/a"},
    ]
    for index, item in enumerate(batch["files"]):
        report = json.loads((output / item["report"]).read_text())
        assert report["run"]["id"] == batch["run"]["id"]
        assert report["run"]["index"] == index
        assert report["run"]["state"] == ("completed" if index == 0 else "failed")
    assert_batch_output(output, batch)


def test_mineru_interruption_preserves_an_already_failed_sibling(tmp_path, monkeypatch):
    source = write_source_pdf(tmp_path)
    output = tmp_path / "out"
    config = Config(mineru={"api_base_url": "https://mineru.test", "api_key": "key"})
    data_ids = []
    polls = 0
    monkeypatch.setattr(provider_client, "POLL_INTERVAL_SECONDS", 0)

    def handle(request):
        nonlocal polls
        if request.method == "POST":
            data_ids.extend(item["data_id"] for item in json.loads(request.content)["files"])
            return httpx.Response(200, json={"code": 0, "data": {
                "batch_id": "batch", "file_urls": ["https://transfer.test/0", "https://transfer.test/1"],
            }})
        if request.method == "PUT":
            return httpx.Response(200)
        polls += 1
        if polls == 1:
            return httpx.Response(200, json={"code": 0, "data": {"extract_result": [
                {"data_id": data_ids[0], "state": "failed", "err_msg": "File exceeds 10 MB limit"},
                {"data_id": data_ids[1], "state": "running"},
            ]}})
        raise KeyboardInterrupt

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(KeyboardInterrupt):
            process_files([source, source], output, config, client=client)
    first, second = read_report(output / "0001"), read_report(output / "0002")
    assert first["run"]["state"] == "failed"
    assert first["issues"][-1]["code"] == "mineru_parse_failed"
    assert first["issues"][-1]["reason"] == "File exceeds 10 MB limit"
    assert second["run"]["state"] == "interrupted"
    assert not any(issue["code"] == "interrupted" for issue in first["issues"])


def test_batch_marks_stale_checkpoint_unreported_after_persistent_write_failure(tmp_path, monkeypatch):
    source = tmp_path / "note.txt"
    source.write_text("Body")
    output = tmp_path / "out"
    write = pipeline._write_record
    reject = False

    def stop_updates(path, value):
        nonlocal reject
        if path == output / "0001" / "report.json":
            reject |= value["stages"]["parse"]["state"] == "succeeded"
            if reject:
                raise OSError("read-only report directory")
        write(path, value)

    monkeypatch.setattr(pipeline, "_write_record", stop_updates)
    run = process_files([source], output)
    assert read_report(output / "0001")["run"]["state"] == "running"
    [entry] = run["files"]
    assert entry["report"] is None
    assert entry["unreported"]["stage"] == "write"
    assert entry["unreported"]["code"] == "io_error"
    assert_batch_output(output, run)


def test_remote_checkpoint_collision_does_not_fail_the_sibling(tmp_path):
    source = write_source_pdf(tmp_path)
    output = tmp_path / "out"
    service = BatchService()
    config = Config(mineru={"api_base_url": "https://mineru.test", "api_key": "mineru-secret"})

    def handle(request):
        response = service.handle(request)
        if request.method == "POST":
            replacement = tmp_path / "replacement"
            replacement.write_text("caller replacement")
            replacement.replace(output / "0001" / "report.json")
        return response

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        run = process_files([source, source], output, config, client=client)
    first, second = run["files"]
    assert first["report"] is None
    assert first["unreported"]["stage"] == "write"
    assert first["unreported"]["code"] == "output_not_empty"
    assert (output / "0001" / "report.json").read_text() == "caller replacement"
    assert second["report"] == "0002/report.json"
    assert read_report(output / "0002")["run"]["state"] == "completed"


def sources_at(tmp_path, count=2):
    result = []
    for index in range(count):
        path = tmp_path / f"{index}.pdf"
        path.write_bytes(b"source")
        result.append((index, InputFile(path=path)))
    return result


def test_mineru_observes_shared_requests_and_complete_acquisition_interval(tmp_path, monkeypatch):
    now = [0.0]
    polls, submitted, observations = [], [], []
    counts = MinerURequests()
    monkeypatch.setattr("doc_pipeline.parsers.multimodal.client.time.monotonic", lambda: now[0])
    monkeypatch.setattr("doc_pipeline.parsers.multimodal.client.time.sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    def handle(request):
        if request.url.path == "/api/v4/file-urls/batch":
            assert request.headers["Authorization"] == "Bearer private-key"
            now[0] += 2
            submitted.extend(json.loads(request.content)["files"])
            return httpx.Response(200, json={"code": 0, "data": {
                "batch_id": "batch", "file_urls": [f"https://transfer.test/upload/{index}" for index in range(2)],
            }})
        if request.method == "PUT":
            assert "Authorization" not in request.headers
            assert "X-Private" not in request.headers
            now[0] += 1
            return httpx.Response(200)
        if request.url.path == "/api/v4/extract-results/batch/batch":
            now[0] += 1
            polls.append(request)
            return httpx.Response(200, json={"code": 0, "data": {"extract_result": [
                {"data_id": submitted[0]["data_id"], "state": "done", "full_zip_url": "https://transfer.test/first"},
                {"data_id": submitted[1]["data_id"], "state": "running" if len(polls) == 1 else "done",
                 "extract_progress": {"total_pages": 5, "extracted_pages": 2},
                 "full_zip_url": "https://transfer.test/second"},
            ]}})
        assert "Authorization" not in request.headers
        assert "X-Private" not in request.headers
        now[0] += 4 if request.url.path == "/first" else 6
        return httpx.Response(200, content=b"archive")

    config = Config(mineru={"api_base_url": "https://mineru.test", "api_key": "private-key"})
    with httpx.Client(transport=httpx.MockTransport(handle), headers={"Authorization": "private-default", "X-Private": "secret"}) as client:
        results = obtain_archives(sources_at(tmp_path), config, client, tmp_path, requests=counts,
                                  on_observation=lambda index, observation: observations.append((index, asdict(observation))))
    assert all(isinstance(result, ArchiveSuccess) for result in results)
    assert [result.elapsed_seconds for result in results] == [13, 19]
    assert counts.snapshot() == {
        "submit": {"requests": 1, "responses": 1}, "poll": {"requests": 2, "responses": 2},
        "upload": {"requests": 2, "responses": 2}, "download": {"requests": 2, "responses": 2},
    }
    assert observations[0][1]["batch_id"] is None
    assert any(index == 1 and observation["extracted_pages"] == 2 for index, observation in observations)
    assert results[0].observation.total_pages is None
    assert results[1].observation.extracted_pages == 2


def test_submit_retries_count_once_and_report_impact_for_each_document(tmp_path, monkeypatch):
    monkeypatch.setattr("doc_pipeline.services.io.time.sleep", lambda _: None)
    counts, issues = MinerURequests(), []
    config = Config(mineru={"api_base_url": "https://mineru.test", "api_key": "private-key"})
    reason = "Quota exceeded for private-key at https://signed.test/object?sig=private-signature"
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429, json={"err_msg": reason}))) as client:
        results = obtain_archives(sources_at(tmp_path), config, client, tmp_path, requests=counts,
                                  on_issue=lambda *args: issues.append(args))
    assert counts.submit.snapshot() == {"requests": 3, "responses": 3}
    assert counts.poll.requests == counts.upload.requests == counts.download.requests == 0
    assert [issue[0] for issue in issues] == [0, 1, 0, 1]
    assert all(issue[1:] == ("http_429", "Quota exceeded for [redacted] at [redacted]", "retried", True)
               for issue in issues)
    assert all(isinstance(result, ArchiveFailure) and result.error.code == "http_429" for result in results)
    assert all("private" not in result.error.reason for result in results)


def test_submit_transport_error_is_not_retried(tmp_path):
    counts = MinerURequests()

    def fail(_):
        raise httpx.ConnectError("private endpoint")

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        [result] = obtain_archives(sources_at(tmp_path, 1), Config(mineru={"api_key": "private-key"}),
                                   client, tmp_path, requests=counts)
    assert result.error.code == "transport_error"
    assert counts.submit.snapshot() == {"requests": 1, "responses": 0}


def test_poll_and_download_retries_keep_counts_and_security_semantics(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("doc_pipeline.services.io.time.sleep", lambda _: None)
    caplog.set_level("DEBUG")
    counts, issues, submitted = MinerURequests(), [], []
    polls, transfers = [], []

    class InterruptedArchive(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial archive"
            raise httpx.ReadError("private transport diagnostic")

    def handle(request):
        if request.url.path == "/api/v4/file-urls/batch":
            submitted.extend(json.loads(request.content)["files"])
            return httpx.Response(200, json={"code": 0, "data": {
                "batch_id": "batch", "file_urls": ["https://transfer.test/upload?sig=private-signature"],
            }})
        if request.method == "PUT":
            assert "Authorization" not in request.headers
            return httpx.Response(200)
        if request.url.path == "/api/v4/extract-results/batch/batch":
            polls.append(request)
            if len(polls) == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{
                "data_id": submitted[0]["data_id"], "state": "done",
                "full_zip_url": "https://transfer.test/archive?sig=private-signature",
            }]}})
        assert "Authorization" not in request.headers
        transfers.append(request)
        if request.url.path == "/archive":
            return httpx.Response(302, headers={"Location": "https://cdn.test/ready?sig=private-signature"})
        if len(transfers) == 2:
            return httpx.Response(200, stream=InterruptedArchive())
        return httpx.Response(200, content=b"complete archive")

    with httpx.Client(transport=httpx.MockTransport(handle), headers={"Authorization": "private-default"}) as client:
        [result] = obtain_archives(sources_at(tmp_path, 1), Config(mineru={"api_key": "private-key"}),
                                   client, tmp_path, requests=counts, on_issue=lambda *args: issues.append(args))
    assert isinstance(result, ArchiveSuccess)
    assert result.path.read_bytes() == b"complete archive"
    assert not result.path.with_suffix(".part").exists()
    assert counts.snapshot() == {
        "submit": {"requests": 1, "responses": 1}, "poll": {"requests": 2, "responses": 2},
        "upload": {"requests": 1, "responses": 1}, "download": {"requests": 4, "responses": 4},
    }
    assert issues == [(0, "http_503", None, "retried", True),
                      (0, "transport_error", "n/a", "retried", True)]
    assert "private-" not in caplog.text
