import errno
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from zipfile import ZipFile

import httpx
import pytest

from doc_pipeline import Config, PipelineError, process_file
from doc_pipeline.services import io
from tests.support import document_archive, pdf_bytes, read_output, write_source_pdf


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
    assert report["source"] == {
        "path": str(source.resolve()),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    assert report["parse_status"] == "complete"
    assert report["chunk_count"] == len(chunks) > 0
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
    assert not (output / "report.json").exists()


def test_multimodal_render_failure_leaves_no_converted_pdf(tmp_path, config, monkeypatch):
    source_pdf = write_source_pdf(tmp_path)
    from doc_pipeline import pipeline

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

    assert not output.exists()


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
    assert not (output / "report.json").exists()


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
    assert report["parse_status"] == "complete"
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
    assert not root.exists()
    assert not list(tmp_path.glob(".doc-pipeline-*"))


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
            assert not (root / "report.json").exists()
            with pytest.raises(PipelineError, match="output_in_use"):
                process_file(path, root)
        finally:
            release.set()
        first.result()
    assert read_output(root)[1][0]["body"] == "Body."


@pytest.mark.parametrize("existing", [False, True], ids=["fresh-fallback", "existing-mount"])
@pytest.mark.parametrize("failure", [None, "move", "report"])
def test_directory_install_preserves_inode_and_publishes_report_last(tmp_path, monkeypatch, existing, failure):
    source = tmp_path / "guide.md"
    source.write_text("Body.")
    output = tmp_path / "out"
    if existing:
        output.mkdir()
    inode = []
    original_rename, original_move = Path.rename, io.shutil.move

    def rename(path, target):
        if target == output:
            assert not existing
            inode.append(output.stat().st_ino)
            raise OSError(errno.EBUSY, "mounted")
        if target == output / "report.json":
            assert {entry.name for entry in output.iterdir() if not entry.name.startswith(".")} == {
                "content.md", "chunks.jsonl", "embeddings.jsonl",
            }
            if failure == "report":
                raise OSError("injected report failure")
        return original_rename(path, target)

    def move(path, target):
        assert not (output / "report.json").exists()
        if failure == "move" and path.name == "chunks.jsonl":
            raise OSError("injected move failure")
        return original_move(path, target)

    if existing:
        inode.append(output.stat().st_ino)
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(io.shutil, "move", move)
    if failure:
        with pytest.raises(OSError, match=f"injected {failure} failure"):
            process_file(source, output)
        assert list(output.iterdir()) == [] if existing else not output.exists()
    else:
        process_file(source, output)
        assert output.stat().st_ino == inode[0]
        assert read_output(output)[1][0]["body"] == "Body."
    assert not list(tmp_path.glob(".doc-pipeline-*"))


def test_fresh_directory_rename_publishes_all_outputs_as_last_operation(tmp_path, monkeypatch):
    source = tmp_path / "guide.txt"
    source.write_text("Body.")
    output = tmp_path / "out"
    original_rename = Path.rename
    published = []

    def rename(path, target):
        if target == output:
            assert list(output.iterdir()) == []
            assert {entry.name for entry in path.iterdir()} == {
                "content.md", "chunks.jsonl", "embeddings.jsonl", "report.json",
            }
            assert not path.samefile(output)
            published.append(True)
        return original_rename(path, target)

    monkeypatch.setattr(Path, "rename", rename)
    process_file(source, output)
    assert published == [True]
    assert read_output(output)[0]["chunk_count"] == 1
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
    assert list(output.iterdir()) == [output / "content.md"]


@pytest.mark.parametrize("name", [".report.json.tmp", "report.json", "content.md"])
def test_install_does_not_replace_late_caller_symlinks(tmp_path, name):
    source = tmp_path / "guide.txt"
    source.write_text("Body.")
    output = tmp_path / "out"
    output.mkdir()
    caller = tmp_path / "caller"

    def collide(event):
        if event.stage == "parse":
            (output / name).symlink_to(caller)

    with pytest.raises((OSError, PipelineError)):
        process_file(source, output, on_progress=collide)
    assert (output / name).is_symlink()
    assert not caller.exists()
    assert list(output.iterdir()) == [output / name]


@pytest.mark.parametrize("file", ["embeddings.jsonl", "report.json"])
@pytest.mark.parametrize("existing", [False, True])
def test_prepublication_write_failure_leaves_no_output(tmp_path, monkeypatch, file, existing):
    from doc_pipeline import pipeline

    source = tmp_path / "guide.txt"
    source.write_text("Body.")
    output = tmp_path / "out"
    if existing:
        output.mkdir()
    original = pipeline.write_jsonl if file.endswith("jsonl") else pipeline._write_record

    def fail(path, rows):
        if path.name == file:
            path.write_bytes(b"partial")
            raise OSError("injected write failure")
        return original(path, rows)

    monkeypatch.setattr(pipeline, "write_jsonl" if file.endswith("jsonl") else "_write_record", fail)
    with pytest.raises(OSError, match="injected write failure"):
        process_file(source, output)
    assert list(output.iterdir()) == [] if existing else not output.exists()
    assert not list(tmp_path.glob(".doc-pipeline-*"))
