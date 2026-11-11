import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier, BrokenBarrierError, Event, Lock
from types import SimpleNamespace

import httpx
import pytest
from docutils.parsers.rst import roles

from doc_pipeline import Artifact, Config, InputFile, PipelineError, pipeline, process_files
from doc_pipeline.cli import main
from doc_pipeline.contracts import run as run_contract
from doc_pipeline.parsers.multimodal import client as provider_client
from tests.support import document_archive, pdf_bytes, read_output, write_source_pdf


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
    assert run["file_count"] == run["complete_file_count"] == 4
    assert run["status"] == "complete"
    assert [item["input"] for item in run["files"]] == [str(first), str(note), str(second), str(first)]
    assert [item["report"] for item in run["files"]] == [f"{index:04d}/report.json" for index in range(1, 5)]
    assert (output / "0002" / "content.md").read_text().strip() == "café"
    for index in (0, 2):
        observation = run["files"][index]["mineru"]
        assert (observation["total_pages"], observation["extracted_pages"]) == (2, 1)
        assert observation["options"]["model_version"] == "pipeline"
    for index, item in enumerate(run["files"], 1):
        report, _, _ = read_output(output / f"{index:04d}")
        assert report["execution"]["run_id"] == run["run_id"] == item["run_id"]
        assert item["elapsed_seconds"] >= 0
        assert {"intake", "parse", "render", "chunk", "write"} <= item["stage_seconds"].keys()
        assert ("remote_acquisition" in item["stage_seconds"]) == (index != 2)
    events = [json.loads(message.removeprefix("document_finished ")) for message in caplog.messages
              if message.startswith("document_finished ")]
    assert sorted(event["index"] for event in events) == [0, 1, 2, 3]
    assert all(event["run_id"] == run["run_id"] and "intake" in event["stage_seconds"] for event in events)
    for event in events:
        assert event["stage_seconds"] == run["files"][event["index"]]["stage_seconds"]
    for private in ("mineru-secret", "default-key", "default-cookie", "write-secret", "read-secret",
                    "https://signed.test", "Pipeline probe section 1", "café"):
        assert private not in caplog.text
        assert private not in (output / "run.json").read_text()


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
    complete, failed, rejected, _ = run["files"]
    report, _, _ = read_output(output / "0001")
    events = {event["index"]: event for event in (
        json.loads(message.removeprefix("document_finished ")) for message in caplog.messages
        if message.startswith("document_finished ")
    )}

    assert complete["status"] == "complete"
    assert failed["error"] == {"stage": "parse", "code": "native_decode_error", "retryable": False}
    assert rejected["error"] == {"stage": "remote_acquisition", "code": "mineru_parse_failed", "retryable": False}
    assert run["elapsed_seconds"] == 22
    for item, second in zip(run["files"], (0, 0, 2, 22), strict=True):
        event = events[item["index"]]
        assert item["elapsed_seconds"] == event["elapsed_seconds"] == second
        assert item["finished_at"] == event["finished_at"] == f"2026-01-01T00:00:{second:02d}+00:00"
        assert item["stage_seconds"] == event["stage_seconds"]
    assert all(report["execution"][key] == complete[key] for key in report["execution"])


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
    run = run_batch([source_pdf] * 51, tmp_path / "out", service)
    assert [len(payload["files"]) for payload in service.payloads] == [50, 1]
    assert run["complete_file_count"] == 1
    assert run["failed_file_count"] == 50
    assert all(item["error"]["code"] == "invalid_provider_response" for item in run["files"][:50])
    assert run["files"][50]["status"] == "complete"
    assert run["files"][50]["mineru"]["batch_id"] == "batch-2"


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
    run = run_batch([source_pdf] * 3, tmp_path / "out", service)
    assert [item["status"] for item in run["files"]] == ["failed", "complete", "failed"]
    code = "invalid_provider_response" if fault == "malformed" else "provider_identity_mismatch"
    assert run["files"][0]["error"]["code"] == run["files"][2]["error"]["code"] == code
    downloads = [path for method, host, path in service.calls if method == "GET" and host == "signed.test"]
    assert downloads == ["/download/1"]


def test_timeout_preserves_completed_and_does_not_charge_downloads(tmp_path, monkeypatch):
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
    run = run_batch([source_pdf] * 3, tmp_path / "out", service, timeout_seconds=1)
    assert [item["status"] for item in run["files"]] == ["complete", "complete", "failed"]
    assert run["files"][2]["error"] == {
        "stage": "remote_acquisition", "code": "parse_timeout", "retryable": True,
    }
    assert [item["stage_seconds"]["remote_acquisition"] for item in run["files"]] == [10, 10, 1]


def test_per_file_failures_leave_successful_sibling_and_clean_completion_markers(tmp_path, monkeypatch):
    from doc_pipeline.artifacts import store

    source_pdf = write_source_pdf(tmp_path)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    original_install = store._install

    def fail_install(staging, destination):
        if destination == output / "0006":
            assert list(destination.iterdir()) == []
            assert (staging / "content.md").is_file()
            (staging / "chunks.jsonl").write_text("unfinished")
            raise OSError("private local path or error")
        original_install(staging, destination)

    monkeypatch.setattr(store, "_install", fail_install)
    service = BatchService(
        poll=lambda service: [service.row(1, "failed", err_msg="private provider error"), *service.done(2, 3, 4, 5)],
        upload_failure=0, download_failure=2, archives={3: b"invalid ZIP"},
    )
    output = tmp_path / "out"
    run = run_batch([tmp_path / "missing.pdf", *([source_pdf] * 6)], output, service)
    assert run["complete_file_count"] == 1 and run["failed_file_count"] == 6
    assert [item["error"]["code"] for item in run["files"][:6]] == [
        "io_error", "authentication_failed", "mineru_parse_failed", "http_404",
        "invalid_provider_archive", "io_error",
    ]
    assert [item["error"]["stage"] for item in run["files"][:6]] == [
        "intake", "remote_acquisition", "remote_acquisition", "remote_acquisition", "parse", "write",
    ]
    record = (output / "run.json").read_text()
    assert all(message not in record for message in (
        "private local path or error", "private provider error", "private download error",
    ))
    assert list(output.glob("*/report.json")) == [output / "0007" / "report.json"]
    assert {path.name for path in output.iterdir()} == {"0007", "run.json"}


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
    assert [item["input"] for item in run["files"]] == list(map(str, paths))
    assert run["failed_file_count"] == 1
    assert run["files"][3]["error"] == {"stage": "parse", "code": "native_decode_error", "retryable": False}
    assert all("mineru" not in item for item in run["files"])
    assert not (output / "0004" / "report.json").exists()


def test_local_worker_cannot_auto_archive_a_sibling_output_asset(tmp_path):
    first, second = tmp_path / "first.md", tmp_path / "second.md"
    first.write_text("![image](out/0002/assets/image.png)\n")
    second.write_text("Plain text.")
    output = tmp_path / "out"
    process_files([first, second], output)
    report, _, _ = read_output(output / "0001")
    assert report["diagnostics"] == ["blocked_reference: out/0002/assets/image.png"]


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

    assert run["complete_file_count"] == 2
    for index, expected in enumerate(("Alpha reference", "Beta reference"), 1):
        directory = output / f"{index:04d}"
        report, [chunk], _ = read_output(directory)
        assert report["parse_status"] == "complete"
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
    assert (output / "0001" / "report.json").is_file()
    assert not (output / "0002").exists()
    assert not (output / "run.json").exists()


def test_empty_batch_rejected(tmp_path):
    output = tmp_path / "out"
    with pytest.raises(PipelineError, match="no_input_files"):
        process_files([], output)
    assert not output.exists()


@pytest.mark.parametrize("model", ["pipeline", "vlm"])
def test_batch_sends_model_option_and_keeps_converted_geometry_in_evidence(tmp_path, monkeypatch, model):
    source_pdf = write_source_pdf(tmp_path)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    service = BatchService()
    output = tmp_path / "out"
    run = run_batch([source_pdf], output, service, model_version=model)
    [payload] = service.payloads
    assert payload["model_version"] == model
    assert run["complete_file_count"] == 1
    bundle = output / "0001"
    report, chunks, _ = read_output(bundle)
    assert report["parse_status"] == "complete"
    assert report["diagnostics"] == [
        "mineru_source_locations_unresolved",
        "mineru_source_location_unresolved g0000-b0001 provider_block_match_ambiguous",
        "mineru_source_location_unresolved g0001-b0001 provider_block_match_ambiguous",
    ]
    assert chunks
    artifact = Artifact.open(bundle)
    for chunk in chunks:
        assert chunk["citations"]["pages"] is None
        assert chunk["citations"]["location_status"] == "unavailable"
        selections, _ = artifact.evidence(chunk["id"])
        [heading] = [item for item in selections.selections if item.role == "heading"]
        assert heading.locations[0].path == "native/provider.pdf"
        assert all(not item.locations for item in selections.selections if item.role == "body")
    assert (bundle / "native" / "mineru.zip").is_file()
    assert (bundle / "native" / "provider.pdf").is_file()
    assert "Pipeline probe section 1" in (bundle / "content.md").read_text()
