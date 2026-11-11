import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import httpx
import pytest

from doc_pipeline import Config, PipelineError, process_file
from doc_pipeline.contracts.chunk import Chunk
from doc_pipeline.contracts.observations import EmbeddingUsage, RequestCounts
from doc_pipeline.services.embeddings import embed_chunks
from doc_pipeline.services.io import request_json
from tests.support import assert_document_output, read_batch_reports, read_output


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("EMBEDDING_API_KEY", "private-token")
    return Config(embedding={"api_base_url": "https://embed.test/v1", "model": "embed-test", "dimensions": 2})


def source_text(tmp_path, *sections):
    path = tmp_path / "reference.md"
    path.write_text(
        "\n\n".join(f"# {section}\n\n{section} reference text." for section in sections), encoding="utf-8"
    )
    return path


def embed_request(request):
    assert request.method == "POST"
    assert request.url == "https://embed.test/v1/embeddings"
    assert request.headers.get("Authorization") == "Bearer private-token"
    return json.loads(request.content)


def row(index, vector):
    return {"index": index, "embedding": vector}


def test_provider_indices_match_vectors_to_inputs_out_of_order(tmp_path, config):
    source = source_text(tmp_path, "Alpha", "Beta")
    output = tmp_path / "out"

    def handle(request):
        assert embed_request(request) == {
            "model": "embed-test",
            "dimensions": 2,
            "encoding_format": "float",
            "input": ["Alpha\n\nAlpha reference text.", "Beta\n\nBeta reference text."],
        }
        return httpx.Response(200, json={"model": "embed-test", "data": [row(1, [0, 1]), row(0, [1, 0])]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = process_file(source, output, config, client=client)

    report, chunks, vectors = read_output(output)
    assert result == report
    assert report["stages"]["embed"]["profile"]["model"] == "embed-test"
    assert report["stages"]["embed"]["profile"]["dimensions"] == 2
    assert report["result"]["embedding"]["succeeded"] == 2
    assert report["issues"] == []
    assert [(chunk["id"], chunk["body"]) for chunk in chunks] == [
        (0, "Alpha reference text."),
        (1, "Beta reference text."),
    ]
    assert vectors == [
        {
            "chunk_id": chunk["id"],
            "vector": vector,
            "input_sha256": hashlib.sha256(chunk["embedding_input"].encode()).hexdigest(),
            "profile": {"model": "embed-test", "dimensions": 2, "requested_dimensions": 2,
                        "encoding_format": "float",
                        "endpoint_sha256": hashlib.sha256(b"https://embed.test/v1").hexdigest()},
        }
        for chunk, vector in zip(chunks, ([1, 0], [0, 1]), strict=True)
    ]
    assert assert_document_output(output) == report


@pytest.mark.parametrize(
    ("model", "rows"),
    [
        ("embed-test", [row(0, [1, 0])]),
        ("embed-test", [row(0, [1, 0]), row(0, [0, 1])]),
        ("embed-test", [row(0, [1, 0]), row(1, [1])]),
        ("embed-test", [row(0, [1, 0]), row(1, [0, float("nan")])]),
        ("embed-test", [row(0, [1, 0]), row(1, [0, 10**400])]),
        ("wrong-model", [row(0, [1, 0]), row(1, [0, 1])]),
    ],
    ids=["missing", "duplicate", "dimensions", "nan", "overflow", "model"],
)
def test_invalid_provider_response_cannot_publish_vectors(tmp_path, config, model, rows):
    source = source_text(tmp_path, "Alpha", "Beta")
    output = tmp_path / "out"
    requests = []

    def handle(request):
        assert len(embed_request(request)["input"]) == 2
        requests.append(request.content)
        return httpx.Response(200, content=json.dumps({"model": model, "data": rows}))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        process_file(source, output, config, client=client)

    report, chunks, vectors = read_output(output)
    assert sum(report["result"]["chunks"].values()) == len(chunks) == 2
    assert report["result"]["embedding"]["succeeded"] == 0
    assert vectors == []
    [failure] = [issue for issue in report["issues"] if issue["action"] == "failed"]
    assert failure["code"] == "invalid_embedding_response"
    assert failure["retryable"] is False
    assert failure["occurrences"] == 2
    assert failure["targets"] == [{"unit": "chunk", "values": [[0, 1]]}]
    assert report["result"]["embedding"]["failed"] == 2
    retry_expected = len(rows) < 2 or rows[0]["index"] == rows[1]["index"]
    assert requests == [requests[0]] * (2 if retry_expected else 1)


@pytest.mark.parametrize("recovers", [True, False])
def test_incomplete_vectors_retry_preserves_other_batches(tmp_path, config, recovers):
    source = source_text(tmp_path, *(f"Section {index}" for index in range(11)))
    output = tmp_path / "out"
    requests = []

    def handle(request):
        body = embed_request(request)
        requests.append(request.content)
        rows = [row(index, [1, 2]) for index in range(len(body["input"]))]
        if len(requests) == 1 or (len(requests) == 2 and not recovers):
            rows.pop()
        return httpx.Response(200, json={"model": body["model"], "data": rows})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        process_file(source, output, config, client=client)

    assert len(requests) == 3
    assert requests[0] == requests[1]
    report, chunks, vectors = read_output(output)
    assert len(chunks) == 11
    assert [vector["chunk_id"] for vector in vectors] == (list(range(11)) if recovers else [10])
    failures = [issue for issue in report["issues"] if issue["action"] == "failed"]
    assert report["result"]["embedding"]["failed"] == (0 if recovers else 10)
    if recovers:
        assert failures == []
    else:
        [failure] = failures
        assert failure["occurrences"] == 10
        assert failure["targets"] == [{"unit": "chunk", "values": [[0, 9]]}]
    [retry] = [issue for issue in report["issues"] if issue["action"] == "retried"]
    assert retry["reason"] == "incomplete_vectors"


@pytest.mark.parametrize("retry_status", [200, 413, 503])
def test_incomplete_vectors_get_one_resend_after_network_retries(tmp_path, config, monkeypatch, retry_status):
    monkeypatch.setattr("doc_pipeline.services.io.time.sleep", lambda _: None)
    source = source_text(tmp_path, "Alpha", "Beta")
    output = tmp_path / "out"
    requests = []

    def handle(request):
        embed_request(request)
        requests.append(request.content)
        if len(requests) <= 2:
            return httpx.Response(503)
        if len(requests) == 3:
            return httpx.Response(200, json={"model": "embed-test", "data": [row(0, [1, 2])]})
        assert len(requests) == 4
        return httpx.Response(retry_status, json={"model": "embed-test", "data": [row(0, [1, 2]), row(1, [2, 1])]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        report = process_file(source, output, config, client=client)

    assert requests == [requests[0]] * 4
    assert report["result"]["embedding"]["succeeded"] == (2 if retry_status == 200 else 0)
    assert [issue["code"] for issue in report["issues"] if issue["action"] == "failed"] == (
        [] if retry_status == 200 else [f"http_{retry_status}"]
    )


def test_bad_batch_preserves_successful_vectors_and_failure_reasons(tmp_path, config):
    source = source_text(tmp_path, "Ready", "Retry", "Rejected")
    output = tmp_path / "out"

    def handle(request):
        body = embed_request(request)
        names = [value.splitlines()[0] for value in body["input"]]
        if len(names) > 1:
            return httpx.Response(400)
        [name] = names
        if name == "Ready":
            return httpx.Response(200, json={"model": body["model"], "data": [row(0, [1, 2])]})
        return httpx.Response({"Retry": 503, "Rejected": 413}[name])

    progress = []
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        process_file(source, output, config, client=client, on_progress=progress.append)

    counts = [(event.completed, event.total, event.failed)
              for event in progress if event.stage == "embedding"]
    assert counts == [(0, 3, 0), (3, 3, 2)]
    assert progress[-1].stage == "partial"

    report, chunks, vectors = read_output(output)
    assert [chunk["body"] for chunk in chunks] == [
        "Ready reference text.",
        "Retry reference text.",
        "Rejected reference text.",
    ]
    assert sum(report["result"]["chunks"].values()) == 3
    assert report["result"]["embedding"]["succeeded"] == 1
    assert [(item["chunk_id"], item["vector"]) for item in vectors] == [(chunks[0]["id"], [1, 2])]
    failures = [issue for issue in report["issues"] if issue["action"] == "failed"]
    assert [(issue["code"], issue["retryable"], issue["targets"]) for issue in failures] == [
        ("http_503", True, [{"unit": "chunk", "values": [[chunks[1]["id"], chunks[1]["id"]]]}]),
        ("http_413", False, [{"unit": "chunk", "values": [[chunks[2]["id"], chunks[2]["id"]]]}]),
    ]


def test_each_invocation_fetches_a_fresh_vector(tmp_path, config):
    source = source_text(tmp_path, "Alpha")
    responses = iter(([1, 0], [2, 0]))

    def handle(request):
        body = embed_request(request)
        return httpx.Response(200, json={"model": body["model"], "data": [row(0, next(responses))]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        for index in range(2):
            output = tmp_path / str(index)
            process_file(source, output, config, client=client)
            _, _, vectors = read_output(output)
            assert [(item["chunk_id"], item["vector"]) for item in vectors] == [(0, [index + 1, 0])]


def test_opaque_content_is_retained_without_sending_it_for_embedding(tmp_path, config):
    source = tmp_path / "opaque.md"
    source.write_text("<div>Uninterpreted content.</div>\n")
    output = tmp_path / "out"

    def handle(request):
        pytest.fail(f"unexpected embedding request to {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        process_file(source, output, config, client=client)

    report, [chunk], vectors = read_output(output)
    assert "Uninterpreted content." in chunk["body"]
    assert chunk["embedding_input"] is None
    assert vectors == []
    assert report["result"]["embedding"] == {"succeeded": 0, "failed": 0, "ineligible": 1}
    assert report["stages"]["embed"]["reason"] == "no_eligible_chunks"
    assert report["stages"]["embed"]["usage"] == {
        "requests": 0, "responses": 0,
        "prompt_tokens": {"observed": 0, "reported_responses": 0},
        "total_tokens": {"observed": 0, "reported_responses": 0},
    }


@pytest.mark.parametrize("environment,expected", [
    (None, "file-token"), ("", "file-token"), ("environment-token", "environment-token"),
])
def test_file_key_and_environment_override_with_optional_dimensions(tmp_path, monkeypatch, environment, expected):
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    if environment is not None:
        monkeypatch.setenv("EMBEDDING_API_KEY", environment)
    path = tmp_path / "config.toml"
    path.write_text('''[embedding]
api_base_url = "https://other-provider.test/v1"
model = "other-embedding-model"
api_key = "file-token"
''')
    source = source_text(tmp_path, "Hello")
    output = tmp_path / "out"

    def handle(request):
        assert str(request.url) == "https://other-provider.test/v1/embeddings"
        assert request.headers["Authorization"] == "Bearer " + expected
        body = json.loads(request.content)
        assert body["model"] == "other-embedding-model"
        assert "dimensions" not in body
        return httpx.Response(200, json={"model": body["model"], "data": [row(0, [1, 2, 3])]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        process_file(source, output, Config.from_toml(path), client=client)
    report, _, vectors = read_output(output)
    assert report["issues"] == []
    [result] = vectors
    assert (result["chunk_id"], result["vector"]) == (0, [1.0, 2.0, 3.0])
    assert result["profile"]["dimensions"] == 3
    assert result["profile"]["requested_dimensions"] is None
    assert report["stages"]["embed"]["profile"] == {
        "model": "other-embedding-model", "dimensions": "n/a",
        "endpoint_sha256": hashlib.sha256(b"https://other-provider.test/v1").hexdigest(),
    }
    assert "file-token" not in (output / "report.json").read_text()
    assert "environment-token" not in (output / "report.json").read_text()


@pytest.mark.parametrize("width", [0, 3])
def test_inferred_dimensions_remain_consistent_across_batches(tmp_path, monkeypatch, width):
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    config = Config(embedding={"api_base_url": "https://embed.test/v1", "model": "m", "api_key": "file-key"})
    source = source_text(tmp_path, *[str(index) for index in range(11)])
    requests = []

    def handle(request):
        inputs = json.loads(request.content)["input"]
        requests.append(inputs)
        dimensions = 2 if len(requests) == 1 else width
        return httpx.Response(200, json={"model": "m", "data": [
            row(index, [1] * dimensions) for index in range(len(inputs))
        ]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        report = process_file(source, tmp_path / "out", config, client=client)
    assert len(requests) == 2
    assert report["result"]["embedding"]["succeeded"] == 10
    [failure] = report["issues"]
    assert (failure["code"], failure["retryable"], failure["action"]) == (
        "invalid_embedding_response", False, "failed",
    )
    assert failure["targets"] == [{"unit": "chunk", "values": [[10, 10]]}]


def test_configured_maximum_keeps_atomic_content_but_does_not_send_it(tmp_path, monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    source = tmp_path / "source.md"
    source.write_text("```python\n" + "x" * 200 + "\n```\n")
    config = Config(chunking={"target_input_bytes": 64, "max_input_bytes": 100},
                    embedding={"api_base_url": "https://embed.test/v1", "model": "m", "api_key": "file-key"})

    def handle(request):
        pytest.fail("oversized indivisible input must not be sent")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        process_file(source, tmp_path / "out", config, client=client)
    report, [chunk], vectors = read_output(tmp_path / "out")
    assert "x" * 200 in chunk["body"]
    assert vectors == []
    assert report["result"]["embedding"] == {"succeeded": 0, "failed": 1, "ineligible": 0}
    [failure] = report["issues"]
    assert (failure["code"], failure["retryable"], failure["action"]) == ("input_too_large", False, "failed")
    assert failure["targets"] == [{"unit": "chunk", "values": [[0, 0]]}]


def test_owned_http_client_uses_configured_request_timeout(tmp_path, monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    source = source_text(tmp_path, "One")
    original_client = httpx.Client
    seen = []

    def handle(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"model": "m", "data": [row(0, [1])]})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    config = Config(request_timeout_seconds=7,
                    embedding={"api_base_url": "https://embed.test/v1", "model": "m", "api_key": "file-key"})
    result = process_file(source, tmp_path / "out", config)
    assert result["result"]["embedding"]["succeeded"] == 1
    assert seen == [{"connect": 7, "read": 7, "write": 7, "pool": 7}]


def test_batch_usage_keeps_observations_with_independent_coverage(tmp_path, config):
    from doc_pipeline import process_files

    source = source_text(tmp_path, "Observed", "Missing", "Invalid", "Malformed")
    other = tmp_path / "other.txt"
    other.write_text("Other document.")
    output = tmp_path / "out"

    def handle(request):
        body = embed_request(request)
        inputs = body["input"]
        if len(inputs) > 1:
            return httpx.Response(413)
        [value] = inputs
        response = {"model": body["model"], "data": [row(0, [1, 0])]}
        match value.splitlines()[0]:
            case "Observed":
                response["usage"] = {"prompt_tokens": 0, "total_tokens": 3}
            case "Invalid":
                response["usage"] = {"prompt_tokens": 4, "total_tokens": 6}
                response["model"] = "unexpected-model"
            case "Malformed":
                response["usage"] = {"prompt_tokens": True, "total_tokens": -3}
            case "Other document.":
                response["usage"] = {"prompt_tokens": "7", "total_tokens": 9}
        return httpx.Response(200, json=response)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        run = process_files([source, other], output, config, client=client)
    assert set(run) == {"schema_version", "run", "settings", "mineru_requests", "files"}
    report, other_report = read_batch_reports(output, run)
    assert report["stages"]["embed"]["usage"] == {
        "requests": 7, "responses": 7,
        "prompt_tokens": {"observed": 4, "reported_responses": 2},
        "total_tokens": {"observed": 9, "reported_responses": 2},
    }
    assert report["result"]["chunks"] == {"parsed": 4, "partial": 0, "unparsed": 0}
    assert report["result"]["embedding"] == {"succeeded": 3, "failed": 1, "ineligible": 0}
    assert other_report["stages"]["embed"]["usage"] == {
        "requests": 1, "responses": 1,
        "prompt_tokens": {"observed": None, "reported_responses": 0},
        "total_tokens": {"observed": 9, "reported_responses": 1},
    }


def embedding_config(**kwargs):
    return Config(embedding={"api_base_url": "https://embed.test/v1", "model": "test", "api_key": "private-key"},
                  **kwargs)


def chunks(*texts):
    return [Chunk(id=index, body=text or "", heading_path=[], embedding_input=text, citations=[])
            for index, text in enumerate(texts)]


def test_usage_counts_attempts_responses_and_reported_coverage(monkeypatch):
    monkeypatch.setattr("doc_pipeline.services.io.time.sleep", lambda _: None)
    calls, issues = [], []
    usage = EmbeddingUsage()

    def handle(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"usage": {"prompt_tokens": 2, "total_tokens": False}})
        if len(calls) == 2:
            raise httpx.ConnectError("unavailable")
        return httpx.Response(200, json={"usage": {"prompt_tokens": 0, "total_tokens": 4}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        request_json(client, "embedding", "POST", "https://test/embeddings", timeout=1,
                     requests=usage, on_response=usage.observe, on_issue=lambda *args: issues.append(args))
    assert usage.snapshot() == {
        "requests": 3, "responses": 2,
        "prompt_tokens": {"observed": 2, "reported_responses": 2},
        "total_tokens": {"observed": 4, "reported_responses": 1},
    }
    assert issues == [("http_503", None, "retried", True), ("transport_error", "n/a", "retried", True)]


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={}),
    httpx.Response(200, json={"usage": {"prompt_tokens": True, "total_tokens": -1}}),
    httpx.Response(400, content="not JSON"),
    httpx.Response(200, content="not JSON"),
])
def test_usage_without_reported_counts_is_unknown_after_a_request(response):
    usage = EmbeddingUsage()
    assert usage.snapshot() == {
        "requests": 0, "responses": 0,
        "prompt_tokens": {"observed": 0, "reported_responses": 0},
        "total_tokens": {"observed": 0, "reported_responses": 0},
    }
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as client:
        try:
            request_json(client, "embedding", "POST", "https://test/embeddings", timeout=1,
                         requests=usage, on_response=usage.observe)
        except PipelineError:
            pass
    assert usage.snapshot() == {
        "requests": 1, "responses": 1,
        "prompt_tokens": {"observed": None, "reported_responses": 0},
        "total_tokens": {"observed": None, "reported_responses": 0},
    }


def test_no_request_is_counted_when_deadline_expires_before_send(monkeypatch):
    monkeypatch.setattr("doc_pipeline.services.io.time.monotonic", lambda: 5)
    counts = RequestCounts()
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("expired request"))) as client:
        with pytest.raises(PipelineError, match="parse_timeout"):
            request_json(client, "parse", "GET", "https://test", timeout=1, deadline=4, requests=counts)
    assert counts.snapshot() == {"requests": 0, "responses": 0}


def test_headers_count_as_response_even_when_body_read_fails():
    class BrokenBody(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadError("body failed")
            yield b""

    counts = RequestCounts()
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=BrokenBody()))) as client:
        with pytest.raises(PipelineError, match="transport_error"):
            request_json(client, "parse", "POST", "https://test", timeout=1,
                         retry_transport=False, requests=counts)
    assert counts.snapshot() == {"requests": 1, "responses": 1}


def test_split_and_terminal_embedding_failures_keep_chunk_targets(monkeypatch):
    monkeypatch.setattr("doc_pipeline.services.io.time.sleep", lambda _: None)
    issues = []

    def handle(request):
        payload = json.loads(request.content)
        if len(payload["input"]) > 1:
            return httpx.Response(413)
        if payload["input"] == ["bad"]:
            return httpx.Response(503)
        return httpx.Response(200, json={"model": "test", "data": [{"index": 0, "embedding": [1]}],
                                        "usage": {"prompt_tokens": 0}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        vectors, errors, usage = embed_chunks(chunks("good", "bad"), embedding_config(), client,
                                              on_issue=lambda *args: issues.append(args))
    assert vectors == [{"chunk_id": 0, "vector": [1.0]}]
    assert errors == [{"chunk_id": 1, "code": "http_503", "reason": None, "retryable": True}]
    assert issues == [("http_413", None, "split", False, [0, 1]),
                      ("http_503", None, "retried", True, [1]),
                      ("http_503", None, "retried", True, [1])]
    assert usage.snapshot() == {
        "requests": 5, "responses": 5,
        "prompt_tokens": {"observed": 0, "reported_responses": 1},
        "total_tokens": {"observed": None, "reported_responses": 0},
    }


@pytest.mark.parametrize("recovers", [True, False])
def test_incomplete_response_retry_is_reported_once_with_all_targets(recovers):
    calls, issues = [], []

    def handle(request):
        calls.append(request)
        rows = [{"index": 0, "embedding": [1]}]
        if len(calls) == 2 and recovers:
            rows.append({"index": 1, "embedding": [2]})
        return httpx.Response(200, json={"model": "test", "data": rows,
                                        "usage": {"prompt_tokens": 2, "total_tokens": 3}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        vectors, errors, usage = embed_chunks(chunks("first", "second"), embedding_config(), client,
                                              on_issue=lambda *args: issues.append(args))
    assert len(vectors) == (2 if recovers else 0)
    assert [error["chunk_id"] for error in errors] == ([] if recovers else [0, 1])
    assert issues == [("invalid_embedding_response", "incomplete_vectors", "retried", True, [0, 1])]
    assert usage.requests == usage.responses == 2
    assert usage.snapshot()["total_tokens"] == {"observed": 6, "reported_responses": 2}


def test_live_usage_retains_attempts_when_embedding_is_interrupted():
    usage = EmbeddingUsage()

    def interrupt(_):
        raise KeyboardInterrupt

    with httpx.Client(transport=httpx.MockTransport(interrupt)) as client:
        with pytest.raises(KeyboardInterrupt):
            embed_chunks(chunks("first"), embedding_config(), client, usage=usage)
    assert usage.requests == 1
    assert usage.responses == 0
    assert usage.snapshot()["prompt_tokens"] == {"observed": None, "reported_responses": 0}


def test_ineligible_and_oversized_inputs_do_not_invent_usage():
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected request"))) as client:
        vectors, errors, usage = embed_chunks(chunks(None, "x" * 101), embedding_config(
            chunking={"target_input_bytes": 64, "max_input_bytes": 100}), client)
    assert vectors == []
    assert errors == [{"chunk_id": 1, "code": "input_too_large", "reason": "n/a", "retryable": False}]
    assert usage.snapshot()["prompt_tokens"] == {"observed": 0, "reported_responses": 0}
    assert usage.requests == usage.responses == 0


def test_expired_retry_does_not_report_a_resend_that_never_happened(monkeypatch):
    now, issues, counts = [0.0], [], RequestCounts()
    monkeypatch.setattr("doc_pipeline.services.io.time.monotonic", lambda: now[0])

    def handle(_):
        now[0] = 2
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(PipelineError, match="parse_timeout"):
            request_json(client, "parse", "GET", "https://test", timeout=1, deadline=1,
                         requests=counts, on_issue=lambda *args: issues.append(args))
    assert counts.snapshot() == {"requests": 1, "responses": 1}
    assert issues == []


def test_usage_is_not_cross_attributed_between_calls_sharing_a_client():
    barrier = Barrier(2)

    def handle(request):
        barrier.wait(timeout=5)
        amount = int(json.loads(request.content)["input"][0])
        return httpx.Response(200, json={"model": "test", "data": [{"index": 0, "embedding": [amount]}],
                                        "usage": {"prompt_tokens": amount}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(embed_chunks, chunks(str(amount)), embedding_config(), client) for amount in (2, 7)]
        results = [future.result() for future in futures]
    assert [result[2].snapshot()["prompt_tokens"] for result in results] == [
        {"observed": 2, "reported_responses": 1}, {"observed": 7, "reported_responses": 1},
    ]
    assert all(result[2].requests == result[2].responses == 1 for result in results)
