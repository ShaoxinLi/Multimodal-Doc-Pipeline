import math
from collections.abc import Callable

from doc_pipeline.contracts.chunk import Chunk
from doc_pipeline.contracts.config import Config, EmbeddingConfig, api_key
from doc_pipeline.contracts.core import PipelineError
from doc_pipeline.contracts.observations import EmbeddingIssueCallback, EmbeddingUsage
from doc_pipeline.services.io import request_json


def validate_vectors(value, size: int, profile: EmbeddingConfig,
                     dimensions: int | None = None) -> list[list[float]]:
    data = value.get("data")
    if value.get("model") != profile.model or not isinstance(data, list) or len(data) != size:
        raise PipelineError("embedding", "invalid_embedding_response")
    ordered = {}
    dimensions = profile.dimensions or dimensions
    for row in data:
        if not isinstance(row, dict):
            raise PipelineError("embedding", "invalid_embedding_response")
        index, vector = row.get("index"), row.get("embedding")
        if (type(index) is not int or index in ordered or index not in range(size)
                or not isinstance(vector, list) or not vector
                or (dimensions is not None and len(vector) != dimensions)
                or any(type(number) not in {int, float} for number in vector)):
            raise PipelineError("embedding", "invalid_embedding_response")
        dimensions = len(vector)
        try:
            vector = [float(number) for number in vector]
        except OverflowError as exc:
            raise PipelineError("embedding", "invalid_embedding_response") from exc
        if not all(math.isfinite(number) for number in vector):
            raise PipelineError("embedding", "invalid_embedding_response")
        ordered[index] = vector
    return [ordered[index] for index in range(size)]


def embed_chunks(chunks: list[Chunk], config: Config, client, *,
                 on_progress: Callable[[int, int, int], None] | None = None,
                 on_issue: EmbeddingIssueCallback | None = None,
                 on_error: Callable[[dict], None] | None = None,
                 usage: EmbeddingUsage | None = None,
                 ) -> tuple[list[dict], list[dict], EmbeddingUsage]:
    usage = usage if usage is not None else EmbeddingUsage()
    profile = config.embedding
    if profile is None:
        return [], [], usage
    dimensions = profile.dimensions
    vectors, errors, eligible = [], [], []

    def failed(chunk, code, reason, retryable):
        error = {"chunk_id": chunk.id, "code": code, "reason": reason, "retryable": retryable}
        errors.append(error)
        if on_error is not None:
            on_error(error)

    for chunk in chunks:
        if chunk.embedding_input is None:
            continue
        if len(chunk.embedding_input.encode("utf-8")) > config.chunking.max_input_bytes:
            failed(chunk, "input_too_large", "n/a", False)
        else:
            eligible.append(chunk)

    def request_batch(batch):
        nonlocal dimensions
        incomplete = False

        def issue(code: str, reason: str | None, action: str, retryable: bool) -> None:
            if on_issue is not None:
                on_issue(code, reason, action, retryable, [chunk.id for chunk in batch])

        try:
            key = api_key("EMBEDDING_API_KEY", profile.api_key)
            if not key:
                raise PipelineError("embedding", "missing_embedding_credentials")
            payload = {"model": profile.model, "input": [chunk.embedding_input for chunk in batch],
                       "encoding_format": "float"}
            if profile.dimensions is not None:
                payload["dimensions"] = profile.dimensions

            def observe_response(response: dict) -> PipelineError | None:
                nonlocal incomplete
                rows = response.get("data")
                if response.get("model") != profile.model or not isinstance(rows, list):
                    return None
                indices = [row.get("index") for row in rows if isinstance(row, dict)]
                valid_indices = [index for index in indices if type(index) is int and index in range(len(batch))]
                incomplete = len(rows) < len(batch) or len(set(valid_indices)) < len(valid_indices)
                if incomplete:
                    return PipelineError("embedding", "invalid_embedding_response", True, reason="incomplete_vectors")
                return None

            response = request_json(client, "embedding", "POST", profile.api_base_url + "/embeddings",
                                    timeout=config.request_timeout_seconds,
                                    retry_response_once=observe_response,
                                    requests=usage, on_response=usage.observe, on_issue=issue,
                                    headers={"Authorization": "Bearer " + key}, json=payload)
            values = validate_vectors(response, len(batch), profile, dimensions)
            dimensions = len(values[0])
        except PipelineError as error:
            if not incomplete and error.code in {"http_400", "http_413", "http_422"} and len(batch) > 1:
                issue(error.code, error.reason, "split", error.retryable)
                middle = len(batch) // 2
                request_batch(batch[:middle])
                request_batch(batch[middle:])
            else:
                reason = "incomplete_vectors" if incomplete and error.code == "invalid_embedding_response" else error.reason
                for chunk in batch:
                    failed(chunk, error.code, reason, error.retryable)
            return
        vectors.extend({"chunk_id": chunk.id, "vector": value}
                       for chunk, value in zip(batch, values, strict=True))

    total = len(eligible) + len(errors)
    if on_progress is not None:
        on_progress(len(errors), total, len(errors))
    for index in range(0, len(eligible), 10):
        request_batch(eligible[index:index + 10])
        if on_progress is not None:
            on_progress(len(vectors) + len(errors), total, len(errors))
    return vectors, errors, usage
