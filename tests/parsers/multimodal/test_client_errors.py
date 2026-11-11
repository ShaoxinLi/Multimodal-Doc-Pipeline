import json

import httpx
import pytest

from doc_pipeline import Config, InputFile, PipelineError, process_file
from doc_pipeline.parsers.multimodal.client import ArchiveFailure, obtain_archives
from doc_pipeline.services.io import provider_reason
from tests.support import write_source_pdf


@pytest.fixture
def allocation_error(tmp_path, monkeypatch, caplog):
    source_pdf = write_source_pdf(tmp_path)
    monkeypatch.setenv("MINERU_API_KEY", "private-mineru-key")
    config = Config(mineru={"api_base_url": "https://mineru.test"})
    output = tmp_path / "out"

    def error_for(**response_fields):
        requests = []

        def handle(request):
            requests.append((request.method, request.url.path))
            return httpx.Response(200, json={"msg": "private-provider-message", **response_fields})

        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises(PipelineError) as error:
                process_file(source_pdf, output, config, client=client)

        assert requests == [("POST", "/api/v4/file-urls/batch")]
        report = json.loads((output / "report.json").read_text())
        assert report["run"]["state"] == "failed"
        assert report["issues"][-1]["code"] == error.value.code
        assert report["issues"][-1]["stage"] == "acquire"
        assert {path.name for path in output.iterdir()} == {"report.json"}
        for private in ("private-provider-message", "private-mineru-key", "private-unknown-code"):
            assert private not in str(error.value) + caplog.text
        return error.value

    return error_for


@pytest.mark.parametrize(("provider_code", "code", "retryable"), [
    ("A0202", "mineru_invalid_token", False),
    ("A0211", "mineru_expired_token", False),
    (-500, "mineru_invalid_parameters", False),
    (-10001, "mineru_service_error", True),
    (-10002, "mineru_invalid_parameters", False),
    (-60001, "mineru_upload_url_failed", True),
    (-60002, "mineru_unsupported_format", False),
    (-60003, "mineru_file_read_failed", False),
    (-60004, "mineru_empty_file", False),
    (-60005, "mineru_file_size_limit", False),
    (-60006, "mineru_page_limit", False),
    (-60007, "mineru_service_unavailable", True),
    (-60008, "mineru_file_read_timeout", False),
    (-60009, "mineru_queue_full", True),
    (-60010, "mineru_processing_failed", True),
    (-60011, "mineru_file_missing", False),
    (-60012, "mineru_task_missing", False),
    (-60013, "mineru_task_forbidden", False),
    (-60014, "mineru_task_running", False),
    (-60015, "mineru_file_conversion_failed", False),
    (-60016, "mineru_export_conversion_failed", False),
    (-60017, "mineru_retry_limit", False),
    (-60018, "mineru_daily_quota_exceeded", False),
    (-60019, "mineru_html_quota_exceeded", False),
    (-60020, "mineru_file_split_failed", True),
    (-60021, "mineru_page_count_failed", True),
    (-60022, "mineru_webpage_fetch_failed", True),
    ("-60009", "mineru_queue_full", True),
    ("-60018", "mineru_daily_quota_exceeded", False),
])
def test_mineru_classifies_documented_response_codes(allocation_error, provider_code, code, retryable):
    error = allocation_error(code=provider_code)
    assert (error.stage, error.code, error.retryable) == ("parse", code, retryable)


@pytest.mark.parametrize("provider_code", [
    None, True, False, 0.0, "0", "-0", -60009.0, [-60009], {"code": -60009},
    "", " -60009", "-60009.0", "-060009", "a0202", -99999, "private-unknown-code",
])
def test_mineru_rejects_unknown_and_malformed_response_codes(allocation_error, provider_code):
    error = allocation_error(code=provider_code, data={})
    assert (error.stage, error.code, error.retryable) == ("parse", "mineru_request_rejected", False)


def test_mineru_rejects_a_missing_response_code(allocation_error):
    error = allocation_error(data={})
    assert (error.stage, error.code, error.retryable) == ("parse", "mineru_request_rejected", False)


def test_mineru_checks_response_data_after_integer_zero(allocation_error):
    error = allocation_error(code=0, data=[])
    assert (error.stage, error.code, error.retryable) == ("parse", "invalid_provider_response", False)


def test_provider_reason_retains_specific_message_and_removes_sensitive_details():
    assert provider_reason("The file exceeds 10 MB; Authorization: Bearer private-token; "
                           "https://signed.test/archive?token=secret\nRequest body: private-input") == (
        "The file exceeds 10 MB; [redacted]; [redacted]"
    )
    assert provider_reason(None) is None
    assert provider_reason({"msg": "not text"}) is None


def test_mineru_terminal_error_preserves_sanitized_concrete_reason(tmp_path):
    source = tmp_path / "0.pdf"
    source.write_bytes(b"source")
    submitted = []

    def handle(request):
        if request.method == "POST":
            submitted.extend(json.loads(request.content)["files"])
            return httpx.Response(200, json={"code": 0, "data": {
                "batch_id": "batch", "file_urls": ["https://transfer.test/upload"],
            }})
        if request.method == "PUT":
            return httpx.Response(200)
        return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{
            "data_id": submitted[0]["data_id"], "state": "failed",
            "err_msg": "File exceeds 10 MB for private-key. https://signed.test/?sig=private-signature\nraw source",
            "extract_progress": {"total_pages": 1, "extracted_pages": 0},
        }]}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        [result] = obtain_archives([(0, InputFile(path=source))], Config(mineru={"api_key": "private-key"}), client, tmp_path)
    assert isinstance(result, ArchiveFailure)
    assert result.error.code == "mineru_parse_failed"
    assert result.error.reason == "File exceeds 10 MB for [redacted]. [redacted]"
    assert result.observation.total_pages == 1
    assert result.observation.extracted_pages == 0
