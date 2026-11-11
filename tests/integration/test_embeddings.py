import json

import httpx
import pytest

from doc_pipeline import Config, process_file
from tests.support import read_output


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
    assert report["embedding"]["model"] == "embed-test"
    assert report["embedding"]["dimensions"] == 2
    assert report["embedding_count"] == 2
    assert report["embedding_errors"] == []
    assert [(chunk["id"], chunk["body"]) for chunk in chunks] == [
        (0, "Alpha reference text."),
        (1, "Beta reference text."),
    ]
    assert vectors == [{"chunk_id": 0, "vector": [1, 0]}, {"chunk_id": 1, "vector": [0, 1]}]


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

    def handle(request):
        assert len(embed_request(request)["input"]) == 2
        return httpx.Response(200, content=json.dumps({"model": model, "data": rows}))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        process_file(source, output, config, client=client)

    report, chunks, vectors = read_output(output)
    assert report["chunk_count"] == len(chunks) == 2
    assert report["embedding_count"] == 0
    assert vectors == []
    assert report["embedding_errors"] == [
        {"chunk_id": chunk_id, "code": "invalid_embedding_response", "retryable": False}
        for chunk_id in (0, 1)
    ]


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
    assert report["chunk_count"] == 3
    assert report["embedding_count"] == 1
    assert vectors == [{"chunk_id": chunks[0]["id"], "vector": [1, 2]}]
    assert report["embedding_errors"] == [
        {"chunk_id": chunks[1]["id"], "code": "http_503", "retryable": True},
        {"chunk_id": chunks[2]["id"], "code": "http_413", "retryable": False},
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
            assert vectors == [{"chunk_id": 0, "vector": [index + 1, 0]}]


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
    assert vectors == report["embedding_errors"] == []


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
    assert report["embedding_errors"] == []
    assert vectors == [{"chunk_id": 0, "vector": [1.0, 2.0, 3.0]}]
    assert "api_key" not in report["embedding"]
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
    assert report["embedding_count"] == 10
    assert report["embedding_errors"] == [
        {"chunk_id": 10, "code": "invalid_embedding_response", "retryable": False},
    ]


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
    assert report["embedding_errors"] == [{"chunk_id": 0, "code": "input_too_large", "retryable": False}]


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
    assert result["embedding_count"] == 1
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
    assert run["embedding_usage"] == {
        "responses": 5,
        "prompt_tokens": {"observed_tokens": 4, "responses_with_usage": 2},
        "total_tokens": {"observed_tokens": 18, "responses_with_usage": 3},
    }
    assert run["files"][0]["status"] == "partial"
    assert run["files"][0]["parse_status"] == "complete"
    assert run["files"][0]["embedding_count"] == 3
    assert run["files"][1]["embedding_usage"]["prompt_tokens"] == {
        "observed_tokens": None, "responses_with_usage": 0,
    }
    report, _, _ = read_output(output / "0001")
    assert report["execution"]["embedding_usage"] == run["files"][0]["embedding_usage"]
