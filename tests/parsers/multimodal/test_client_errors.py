import httpx
import pytest

from doc_pipeline import Config, PipelineError, process_file
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
        assert not (output / "report.json").exists()
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
