import hashlib
import json
from zipfile import ZipFile

import httpx
import pytest

from doc_pipeline import Config, PipelineError, process_file
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
    tmp_path, config, converted
):
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
    citations = [citation for chunk in chunks for citation in chunk["citations"]]
    locations = [location for citation in citations for location in citation["locations"]]
    if converted:
        assert locations == []
        assert any(citation.get("reason") == "original_source_location_unverified" for citation in citations)
    else:
        assert {(location["kind"], location["path"]) for location in locations} == {("pdf", str(source.resolve()))}
        assert {location["page_index"] for location in locations} >= {0, 1}
    for chunk in chunks:
        metadata = chunk["metadata"]
        assert metadata["source"] == report["source"]
        if converted:
            assert metadata["source_locations"] == []
            assert metadata["page_indices"] is None
            assert metadata["page_coordinate_space"] is None
            assert metadata["page_status"] == "unavailable"
        else:
            for ref in metadata["source_locations"]:
                assert ref["source"] == report["source"]
                assert ref["coordinate_space"] == "original"
    assert all("sha256" not in json.dumps(citation) for citation in citations)
    for location in locations:
        x1, y1, x2, y2 = location["bbox"]
        width, height = location["page_size"]
        assert 0 <= x1 < x2 <= width
        assert 0 <= y1 < y2 <= height
    assert {path.name for path in output.iterdir()} == {"content.md", "chunks.jsonl", "embeddings.jsonl", "report.json"}


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
        locations = [location for block in parsed.blocks for location in block.locations]
        assert {location.page_index for location in locations} == {0, 1}
        assert all(location.source.path == "provider.pdf" for location in locations)
        raise PipelineError("render", "test_render_failure")

    monkeypatch.setattr(pipeline, "render_markdown", fail_render)
    with httpx.Client(transport=httpx.MockTransport(provider.handle)) as client:
        with pytest.raises(PipelineError, match="test_render_failure"):
            process_file(source_pdf, output, config, client=client)

    assert list(output.iterdir()) == []


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
