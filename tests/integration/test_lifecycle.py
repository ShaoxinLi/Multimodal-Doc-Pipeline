import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from zipfile import ZipFile

import httpx
import pytest

from doc_pipeline import Config, PipelineError, pipeline, process_file
from doc_pipeline.contracts.run import Issues, IssueTarget
from doc_pipeline.services import io
from tests.support import (
    assert_document_output,
    document_archive,
    failure_issue,
    pdf_bytes,
    read_output,
    read_report,
    write_source_pdf,
)


class DocumentService:
    def __init__(self, source, archive, *, redirect="https://archive.test/file"):
        self.source = source
        self.archive = archive
        self.redirect = redirect
        self.data_id = None
        self.uploaded = False

    def handle(self, request):
        method, host, path = request.method, request.url.host, request.url.path
        if host == "document.test":
            assert request.headers.get("Authorization") == "Bearer document-token"
        elif host in {"upload.test", "download.test", "archive.test"}:
            assert "Authorization" not in request.headers
            assert "Cookie" not in request.headers

        if (method, host, path) == ("POST", "document.test", "/api/v4/file-urls/batch"):
            [file] = json.loads(request.content)["files"]
            assert file["name"] == self.source.name
            self.data_id = file["data_id"]
            assert self.data_id
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "batch_id": "batch-1",
                        "file_urls": ["https://upload.test/input?signature=write"],
                    },
                },
            )
        if (method, host, path) == ("PUT", "upload.test", "/input"):
            assert request.url.params["signature"] == "write"
            assert request.content == self.source.read_bytes()
            self.uploaded = True
            return httpx.Response(200)
        if (method, host, path) == ("GET", "document.test", "/api/v4/extract-results/batch/batch-1"):
            assert self.uploaded
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "extract_result": [
                            {
                                "data_id": self.data_id,
                                "state": "done",
                                "full_zip_url": "https://download.test/result?signature=read",
                            }
                        ]
                    },
                },
            )
        if (method, host, path) == ("GET", "download.test", "/result"):
            assert request.url.params["signature"] == "read"
            return httpx.Response(302, headers={"Location": self.redirect})
        if (method, host, path) == ("GET", "archive.test", "/file") and request.url.scheme == "https":
            return httpx.Response(200, content=self.archive)
        pytest.fail(f"unexpected provider request: {request.method} {request.url}")


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("MINERU_API_KEY", "document-token")
    return Config(mineru={"api_base_url": "https://document.test"})


@pytest.mark.parametrize("converted", [False, True], ids=["source-pdf", "converted-office-pdf"])
def test_multimodal_citations_target_the_original_file_and_transfers_are_credential_free(
    tmp_path, config, converted, monkeypatch
):
    from doc_pipeline.parsers.multimodal import parser

    decoded = []
    decode = parser.decode_archive

    def observe(*args, **kwargs):
        result = decode(*args, **kwargs)
        decoded.append(result)
        return result

    monkeypatch.setattr(parser, "decode_archive", observe)
    pdf = pdf_bytes()
    if converted:
        source = tmp_path / "input.docx"
        with ZipFile(source, "w") as office:
            office.writestr("[Content_Types].xml", "<Types/>")
            office.writestr("word/document.xml", "<document/>")
    else:
        source = tmp_path / "input.pdf"
        source.write_bytes(pdf)
    output = tmp_path / "out"
    provider = DocumentService(source, document_archive(pdf))
    with httpx.Client(
        transport=httpx.MockTransport(provider.handle),
        headers={"Authorization": "Bearer client-default-token"},
        auth=("client-user", "client-password"),
        cookies={"session": "private-client-cookie"},
        follow_redirects=True,
    ) as client:
        result = process_file(source, output, config, client=client)

    report, chunks, vectors = read_output(output)
    assert result == report
    assert report["source"]["uri"] == source.resolve().as_uri()
    assert report["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert report["source"]["bytes"] == source.stat().st_size
    assert report["source"]["document_version"] == "n/a"
    assert report["run"]["state"] == "completed"
    assert report["result"]["chunks"] == {"parsed": len(chunks), "partial": 0, "unparsed": 0}
    assert chunks
    assert vectors == []
    assert "Pipeline probe section 1" in (output / "content.md").read_text()
    [parsed] = decoded
    locations = [location for block in parsed.blocks for location in block.locations]
    assert {path.name for path in output.iterdir()} == {"content.md", "chunks.jsonl", "embeddings.jsonl", "report.json"}
    assert all(chunk["citations"]["source_uri"] == source.resolve().as_uri() for chunk in chunks)
    if converted:
        assert {location.source.path for location in locations} == {"result_origin.pdf"}
        assert all(chunk["citations"]["pages"] is None for chunk in chunks)
        assert all(chunk["citations"]["location_status"] == "unavailable" for chunk in chunks)
    else:
        assert {location.source.path for location in locations} == {str(source.resolve())}
        assert {location.page_index for location in locations} >= {0, 1}
        assert {page.basis for page in parsed.pdf_pages} == {"identical_bytes"}
    assert all(chunk["metadata"]["source_sha256"] == report["source"]["sha256"] for chunk in chunks)
    for location in locations:
        x1, y1, x2, y2 = location.bbox
        width, height = location.page_size
        assert 0 <= x1 < x2 <= width
        assert 0 <= y1 < y2 <= height


def test_multimodal_signed_download_rejects_unsafe_redirect(tmp_path, config):
    source_pdf = write_source_pdf(tmp_path)
    output = tmp_path / "out"
    provider = DocumentService(source_pdf, b"", redirect="http://archive.test/file")
    with httpx.Client(transport=httpx.MockTransport(provider.handle), follow_redirects=True) as client:
        with pytest.raises(PipelineError) as error:
            process_file(source_pdf, output, config, client=client)

    assert (error.value.stage, error.value.code) == ("parse", "unsafe_provider_url")
    assert set(output.iterdir()) == {output / "report.json"}
    report = read_report(output)
    assert report["run"]["state"] == "failed"
    assert failure_issue(report, "acquire")["code"] == error.value.code


def test_multimodal_render_failure_leaves_no_converted_pdf(tmp_path, config, monkeypatch):
    source_pdf = write_source_pdf(tmp_path)
    output = tmp_path / "out"
    provider = DocumentService(source_pdf, document_archive(pdf_bytes()))

    def fail_render(parsed):
        locations = [location for block in parsed.blocks for location in block.locations if location.kind == "pdf_region"]
        assert {location.page_index for location in locations} == {0, 1}
        assert all(location.source.path == "result_origin.pdf" for location in locations)
        raise PipelineError("render", "test_render_failure")

    monkeypatch.setattr(pipeline, "render_markdown", fail_render)
    with httpx.Client(transport=httpx.MockTransport(provider.handle)) as client:
        with pytest.raises(PipelineError, match="test_render_failure"):
            process_file(source_pdf, output, config, client=client)

    assert set(output.iterdir()) == {output / "report.json"}
    report = read_report(output)
    assert report["run"]["state"] == "failed"
    assert failure_issue(report, "render")["code"] == "test_render_failure"
    assert report["stages"]["write"]["state"] == "not_reached"


@pytest.mark.parametrize(
    ("lost_response", "code", "retryable"),
    [
        (False, "unsafe_provider_url", False),
        (True, "transport_error", True),
    ],
)
def test_multimodal_allocation_failure_does_not_repeat_allocation(
    tmp_path, config, lost_response, code, retryable
):
    source_pdf = write_source_pdf(tmp_path)
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        if lost_response:
            raise httpx.ReadTimeout("lost allocation response", request=request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "batch_id": "batch-1",
                    "file_urls": ["https://example.test:bad/upload"],
                },
            },
        )

    output = tmp_path / "out"
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(PipelineError) as error:
            process_file(source_pdf, output, config, client=client)

    assert (error.value.stage, error.value.code, error.value.retryable) == ("parse", code, retryable)
    assert calls == [("POST", "/api/v4/file-urls/batch")]
    assert set(output.iterdir()) == {output / "report.json"}
    report = read_report(output)
    assert report["run"]["state"] == "failed"
    assert failure_issue(report, "acquire")["code"] == error.value.code


@pytest.mark.parametrize("environment", [None, "", "document-token"])
def test_mineru_accepts_file_credentials_and_environment_override(tmp_path, monkeypatch, environment):
    source_pdf = write_source_pdf(tmp_path)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    if environment is not None:
        monkeypatch.setenv("MINERU_API_KEY", environment)
    path = tmp_path / "config.toml"
    key = "overridden-file-token" if environment else "document-token"
    path.write_text(f'[mineru]\napi_base_url="https://document.test"\napi_key="{key}"\n')
    provider = DocumentService(source_pdf, document_archive(pdf_bytes()))
    with httpx.Client(transport=httpx.MockTransport(provider.handle)) as client:
        report = process_file(source_pdf, tmp_path / "out", Config.from_toml(path), client=client)
    assert report["run"]["state"] == "completed"
    assert report["stages"]["parse"]["state"] == "succeeded"
    assert key not in str(report)


def test_configured_mineru_deadline_stops_polling(tmp_path, monkeypatch):
    source_pdf = write_source_pdf(tmp_path)
    import doc_pipeline.parsers.multimodal.client as provider_client

    monkeypatch.setenv("MINERU_API_KEY", "file-test-key")
    elapsed = [0.0]
    monkeypatch.setattr(provider_client.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(provider_client.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    polls = []

    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"code": 0, "data": {
                "file_urls": ["https://upload.test/file"], "batch_id": "one",
            }})
        if request.method == "PUT":
            return httpx.Response(200)
        polls.append(request)
        return httpx.Response(200, json={"code": 0, "data": {"extract_result": []}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(PipelineError) as caught:
            process_file(source_pdf, tmp_path / "out",
                         Config(mineru={"timeout_seconds": 1}), client=client)
    assert caught.value.code == "parse_timeout"
    assert len(polls) == 1 and elapsed[0] == 1


def test_remote_upload_uses_the_capture_even_when_input_changes(tmp_path, config):
    source = write_source_pdf(tmp_path)
    original = source.read_bytes()
    output = tmp_path / "out"
    provider = DocumentService(source, document_archive(original))

    def handle(request):
        if request.method == "PUT":
            assert request.content == original
            assert source.read_bytes() != original
            provider.uploaded = True
            return httpx.Response(200)
        return provider.handle(request)

    def mutate(event):
        assert event.path == source
        if event.stage == "upload":
            source.write_bytes(pdf_bytes(3))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        process_file(source, output, config, client=client, on_progress=mutate)
    report, chunks, _ = read_output(output)
    assert report["source"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert all(chunk["metadata"]["source_sha256"] == report["source"]["sha256"] for chunk in chunks)


def test_fatal_embedding_failure_leaves_no_completed_output(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("Keep the parsed text.")
    root = tmp_path / "out"
    config = Config(embedding={"model": "alpha", "api_base_url": "https://provider.test"})

    def fail_embedding(event):
        if event.stage == "embedding":
            raise OSError("injected failure before publication")

    with pytest.raises(OSError, match="before publication"):
        process_file(source, root, config, on_progress=fail_embedding)
    assert set(root.iterdir()) == {root / "report.json"}
    report = read_report(root)
    assert report["run"]["state"] == "failed"
    assert report["result"] == {"chunks": None, "locations": None, "embedding": None}
    assert failure_issue(report, "embed")["code"] == "io_error"
    assert report["stages"]["write"]["state"] == "not_reached"


def test_concurrent_publishers_cannot_share_destination(tmp_path):
    path = tmp_path / "guide.md"
    path.write_text("Body.")
    root = tmp_path / "out"
    entered, release = Event(), Event()

    def pause(event):
        if event.stage == "parse":
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor() as pool:
        first = pool.submit(process_file, path, root, on_progress=pause)
        try:
            assert entered.wait(5)
            assert read_report(root)["run"]["state"] == "running"
            assert not (root / "content.md").exists()
            with pytest.raises(PipelineError, match="output_in_use"):
                process_file(path, root)
        finally:
            release.set()
        first.result()
    assert read_output(root)[1][0]["body"] == "Body."


@pytest.mark.parametrize("existing", [False, True], ids=["fresh", "existing-mount"])
@pytest.mark.parametrize("failure", [None, "move", "report"])
def test_publication_preserves_inode_and_rolls_back_payload_on_failure(tmp_path, monkeypatch, existing, failure):
    source = tmp_path / "guide.md"
    source.write_text("Body.")
    output = tmp_path / "out"
    if existing:
        output.mkdir()
    inode = output.stat().st_ino if existing else None
    original_replace, original_move = Path.replace, io.shutil.move
    payload = {"content.md", "chunks.jsonl", "embeddings.jsonl"}
    moves = []

    def replace(path, target):
        if target == output / "report.json":
            if json.loads(path.read_text())["run"]["state"] == "completed":
                assert read_report(output)["run"]["state"] == "running"
                assert all((output / name).is_file() for name in payload)
                if failure == "report":
                    raise OSError("injected report failure")
        return original_replace(path, target)

    def move(path, target):
        nonlocal inode
        if inode is None:
            inode = output.stat().st_ino
        assert read_report(output)["run"]["state"] == "running"
        if failure == "move" and moves:
            raise OSError("injected move failure")
        result = original_move(path, target)
        moves.append(target)
        return result

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(io.shutil, "move", move)
    if failure:
        with pytest.raises(OSError, match=f"injected {failure} failure"):
            process_file(source, output)
        assert moves
        assert set(output.iterdir()) == {output / "report.json"}
        report = read_report(output)
        assert report["run"]["state"] == "failed"
        assert failure_issue(report, "write")["code"] == "io_error"
        assert report["result"]["chunks"] is None
    else:
        process_file(source, output)
        report, [chunk], _ = read_output(output)
        assert report["run"]["state"] == "completed"
        assert chunk["body"] == "Body."
        assert {entry.name for entry in output.iterdir()} == payload | {"report.json"}
    assert output.stat().st_ino == inode
    assert not list(tmp_path.glob(".doc-pipeline-*"))


@pytest.mark.parametrize("existing", [False, True])
def test_late_destination_collision_does_not_overwrite_user_files(tmp_path, existing):
    source = tmp_path / "guide.txt"
    source.write_text("Body.")
    output = tmp_path / "out"
    if existing:
        output.mkdir()

    def collide(event):
        if event.stage == "parse":
            (output / "content.md").write_text("caller content")

    with pytest.raises((OSError, PipelineError)):
        process_file(source, output, on_progress=collide)
    assert (output / "content.md").read_text() == "caller content"
    assert set(output.iterdir()) == {output / "content.md", output / "report.json"}
    assert failure_issue(read_report(output), "write")["code"] == "output_not_empty"


@pytest.mark.parametrize("name", ["report.json", "content.md"])
def test_install_does_not_replace_late_caller_symlinks(tmp_path, name):
    source = tmp_path / "guide.txt"
    source.write_text("Body.")
    output = tmp_path / "out"
    output.mkdir()
    caller = tmp_path / "caller"

    def collide(event):
        if event.stage == "parse":
            if name == "report.json":
                (output / name).unlink()
            (output / name).symlink_to(caller)

    with pytest.raises((OSError, PipelineError)):
        process_file(source, output, on_progress=collide)
    assert (output / name).is_symlink()
    assert not caller.exists()
    assert set(output.iterdir()) == {output / name, output / "report.json"}


@pytest.mark.parametrize("existing", [False, True])
def test_payload_write_failure_leaves_only_failed_report(tmp_path, monkeypatch, existing):
    source = tmp_path / "guide.txt"
    source.write_text("Body.")
    output = tmp_path / "out"
    if existing:
        output.mkdir()
    original = pipeline.write_jsonl

    def fail(path, rows):
        if path.name == "embeddings.jsonl":
            path.write_bytes(b"partial")
            raise OSError("injected write failure")
        return original(path, rows)

    monkeypatch.setattr(pipeline, "write_jsonl", fail)
    with pytest.raises(OSError, match="injected write failure"):
        process_file(source, output)
    assert set(output.iterdir()) == {output / "report.json"}
    assert failure_issue(read_report(output), "write")["code"] == "io_error"


def test_first_checkpoint_failure_leaves_existing_output_empty(tmp_path, monkeypatch):
    source = tmp_path / "guide.txt"
    source.write_text("Body.")
    output = tmp_path / "out"
    output.mkdir()

    def fail(path, target):
        assert target == output / "report.json"
        raise OSError("injected checkpoint failure")

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="injected checkpoint failure"):
        process_file(source, output)
    assert not list(output.iterdir())


def test_report_has_one_owner_for_each_fact_and_preserves_zero_vs_inapplicable(tmp_path):
    source = tmp_path / "empty.txt"
    source.write_text("")
    output = tmp_path / "out"
    report = process_file(source, output)
    assert report == read_report(output)
    assert set(report) == {"schema_version", "pipeline", "run", "source", "stages", "result", "issues"}
    assert report["schema_version"] == 2
    assert report["run"]["state"] == "completed"
    assert report["result"] == {
        "chunks": {"parsed": 0, "partial": 0, "unparsed": 0},
        "locations": {"known": 0, "partial": 0, "unavailable": 0}, "embedding": "n/a",
    }
    assert report["source"]["bytes"] == 0
    assert report["source"]["document_version"] == "n/a"
    assert report["stages"]["acquire"] == {
        "state": "not_applicable", "seconds": "n/a", "reason": "n/a", "service": "n/a",
    }
    assert report["stages"]["embed"] == {
        "state": "skipped", "seconds": "n/a", "reason": "disabled", "profile": "n/a", "usage": "n/a",
    }
    assert set(report["stages"]) == {"intake", "acquire", "parse", "render", "chunk", "embed", "write"}
    assert {path.name for path in output.iterdir()} == {"content.md", "chunks.jsonl", "embeddings.jsonl", "report.json"}
    assert assert_document_output(output) == report


def test_failure_after_capture_retains_identity_and_unpublished_results(tmp_path):
    source = tmp_path / "bad.pdf"
    source.write_text("This is not a PDF")
    output = tmp_path / "out"
    with pytest.raises(PipelineError, match="format_mismatch"):
        process_file(source, output)
    report = read_report(output)
    assert report["run"]["state"] == "failed"
    assert report["source"]["sha256"] is not None
    assert report["source"]["bytes"] == source.stat().st_size
    assert report["stages"]["intake"]["state"] == "failed"
    assert report["stages"]["parse"]["state"] == "not_reached"
    assert report["result"] == {"chunks": None, "locations": None, "embedding": "n/a"}
    assert report["issues"][0]["code"] == "format_mismatch"
    assert set(output.iterdir()) == {output / "report.json"}
    assert assert_document_output(output) == report


def test_grouping_preserves_occurrences_and_all_targets_without_internal_ids():
    issues = Issues()
    for number in range(45):
        issues.add("parse", "opaque_source", "unknown_syntax", "retained_opaque",
                   target=IssueTarget("chunk", (number,)))
    issues.add("parse", "opaque_source", "unknown_syntax", "retained_opaque", target=IssueTarget("chunk", (4,)))
    issues.add("parse", "opaque_source", "unknown_syntax", "retained_opaque", target=IssueTarget("block", ("empty",)))
    [issue] = issues.snapshot()
    assert issue["occurrences"] == 47
    assert issue["targets"] == [{"unit": "chunk", "values": [[0, 44]]}]
    assert issue["unlocated"] == 1
    assert "empty" not in json.dumps(issue)
    issues.block_targets["empty"] = IssueTarget("content_line", (52,))
    [issue] = issues.snapshot()
    assert issue["unlocated"] == 0
    assert issue["targets"][-1] == {"unit": "content_line", "values": [[52, 52]]}


def test_parse_issues_survive_a_later_render_failure(tmp_path, monkeypatch):
    source = tmp_path / "unsupported.tex"
    source.write_text(r"\unknowncommand{body}")
    output = tmp_path / "out"

    def fail(_):
        raise PipelineError("render", "injected_render_failure")

    monkeypatch.setattr(pipeline, "render_markdown", fail)
    with pytest.raises(PipelineError, match="injected_render_failure"):
        process_file(source, output)
    report = read_report(output)
    assert any(issue["code"] == "opaque_source" and issue["targets"] == [
        {"unit": "source_line", "values": [[1, 1]]},
    ] for issue in report["issues"])
    assert report["issues"][-1]["stage"] == "render"
    assert report["issues"][-1]["code"] == "injected_render_failure"


def test_configured_embedding_with_no_inputs_records_known_zero_requests(tmp_path):
    source = tmp_path / "empty.txt"
    source.write_text("")
    config = Config(embedding={"model": "test", "api_base_url": "https://embedding.test"})
    report = process_file(source, tmp_path / "out", config)
    stage = report["stages"]["embed"]
    assert stage["state"] == "skipped"
    assert stage["reason"] == "no_eligible_chunks"
    assert stage["seconds"] == "n/a"
    assert stage["usage"] == {"requests": 0, "responses": 0,
                              "prompt_tokens": {"observed": 0, "reported_responses": 0},
                              "total_tokens": {"observed": 0, "reported_responses": 0}}
    assert report["result"]["embedding"] == {"succeeded": 0, "failed": 0, "ineligible": 0}


def test_interruption_retains_attempt_counts_and_does_not_publish_payload(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("Body")
    output = tmp_path / "out"
    config = Config(embedding={"model": "test", "api_base_url": "https://embedding.test", "api_key": "key"})

    def interrupt(request):
        running = read_report(output)
        assert running["run"]["state"] == "running"
        assert running["stages"]["embed"]["state"] == "running"
        assert running["result"]["chunks"] is None
        raise KeyboardInterrupt

    with httpx.Client(transport=httpx.MockTransport(interrupt)) as client:
        with pytest.raises(KeyboardInterrupt):
            process_file(source, output, config, client=client)
    report = read_report(output)
    assert report["run"]["state"] == "interrupted"
    assert report["stages"]["embed"]["state"] == "interrupted"
    assert report["stages"]["embed"]["usage"]["requests"] == 1
    assert report["stages"]["embed"]["usage"]["responses"] == 0
    assert report["stages"]["write"]["state"] == "not_reached"
    assert set(output.iterdir()) == {output / "report.json"}
    assert assert_document_output(output) == report


def test_failed_final_checkpoint_rolls_back_payload_and_preserves_failure(tmp_path, monkeypatch):
    source = tmp_path / "note.txt"
    source.write_text("Body")
    output = tmp_path / "out"
    write = pipeline._write_record

    def fail_completed(path, value):
        if value["run"]["state"] == "completed":
            assert (output / "content.md").is_file()
            raise OSError("final report write rejected")
        return write(path, value)

    monkeypatch.setattr(pipeline, "_write_record", fail_completed)
    with pytest.raises(OSError, match="final report write rejected"):
        process_file(source, output)
    report = read_report(output)
    assert report["run"]["state"] == "failed"
    assert report["stages"]["write"]["state"] == "failed"
    assert report["result"]["chunks"] is None
    assert set(output.iterdir()) == {output / "report.json"}


def test_failed_publication_does_not_point_to_removed_chunks(tmp_path, monkeypatch):
    source = tmp_path / "note.txt"
    source.write_text("Body")
    output = tmp_path / "out"
    config = Config(embedding={"model": "test", "api_base_url": "https://embedding.test", "api_key": "key"})
    write = pipeline.write_jsonl

    def fail_vectors(path, rows):
        if path.name == "embeddings.jsonl":
            raise OSError("cannot write vectors")
        write(path, rows)

    monkeypatch.setattr(pipeline, "write_jsonl", fail_vectors)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(400))) as client:
        with pytest.raises(OSError, match="cannot write vectors"):
            process_file(source, output, config, client=client)
    report = read_report(output)
    [embedding_error] = [issue for issue in report["issues"] if issue["stage"] == "embed"]
    assert embedding_error["targets"] == []
    assert embedding_error["unlocated"] == embedding_error["occurrences"] == 1
    assert set(output.iterdir()) == {output / "report.json"}


def test_asset_targets_do_not_embed_inline_bytes_or_signed_credentials(tmp_path):
    source = tmp_path / "images.md"
    source.write_text("![picture](https://user:password@host.test/image.png?signature=secret#token)\n\n"
                      "![inline](data:image/png;base64,DEADBEEF)")
    report = process_file(source, tmp_path / "out")
    text = json.dumps(report)
    assert "password" not in text and "secret" not in text and "DEADBEEF" not in text
    assert any(target == {"unit": "asset", "values": ["https://host.test/image.png"]}
               for issue in report["issues"] for target in issue["targets"])


def test_rst_error_reason_does_not_expose_option_credentials(tmp_path):
    source = tmp_path / "bad.rst"
    source.write_text(".. image:: picture.png\n   :width: https://example.test/file?signature=SECRET\n")
    report = process_file(source, tmp_path / "out")
    text = json.dumps(report)
    assert "SECRET" not in text and "https://example.test" not in text
    assert any(issue["code"] == "docutils_diagnostic" for issue in report["issues"])


@pytest.mark.parametrize("reference", [
    "/image.png?signature=SECRET", "image.png?access_token=SECRET",
    "https%3A%2F%2Fhost.test%2Fimage%3Ftoken%3DSECRET",
])
def test_asset_target_removes_credentials_from_relative_and_encoded_uris(tmp_path, reference):
    source = tmp_path / "image.md"
    source.write_text(f"![image]({reference})")
    report = process_file(source, tmp_path / "out")
    assert "SECRET" not in json.dumps(report)


def test_checkpoint_write_error_preserves_completed_parse_and_diagnostics(tmp_path, monkeypatch):
    source = tmp_path / "bad.tex"
    source.write_text(r"\unknowncommand{body}")
    output = tmp_path / "out"
    write = pipeline._write_record
    injected = False

    def fail_once(path, value):
        nonlocal injected
        if not injected and value["stages"]["parse"]["state"] == "succeeded":
            injected = True
            raise OSError("checkpoint write failed")
        write(path, value)

    monkeypatch.setattr(pipeline, "_write_record", fail_once)
    with pytest.raises(OSError, match="checkpoint write failed"):
        process_file(source, output)
    report = read_report(output)
    assert report["stages"]["intake"]["state"] == "succeeded"
    assert report["stages"]["parse"]["state"] == "succeeded"
    assert report["stages"]["write"]["state"] == "failed"
    assert report["issues"][-1]["stage"] == "write"
    assert any(issue["code"] == "opaque_source" for issue in report["issues"])


def test_embedding_interruption_preserves_earlier_terminal_failures(tmp_path, monkeypatch):
    from doc_pipeline.contracts.chunk import Chunk

    source = tmp_path / "note.txt"
    source.write_text("Body")
    output = tmp_path / "out"
    chunks = [Chunk(id=i, body=str(i), heading_path=[], embedding_input=str(i), citations=[]) for i in range(11)]
    monkeypatch.setattr(pipeline, "make_chunks", lambda *_: chunks)
    config = Config(embedding={"model": "test", "api_base_url": "https://embedding.test", "api_key": "key"})
    calls = 0

    def handle(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(401)
        raise KeyboardInterrupt

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(KeyboardInterrupt):
            process_file(source, output, config, client=client)
    report = read_report(output)
    [failure] = [issue for issue in report["issues"] if issue["code"] == "authentication_failed"]
    assert failure["occurrences"] == failure["unlocated"] == 10
    assert report["stages"]["embed"]["usage"]["requests"] == 2
    assert report["stages"]["embed"]["usage"]["responses"] == 1
