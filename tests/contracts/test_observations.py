from doc_pipeline.contracts.observations import (
    EmbeddingUsage,
    MinerUObservation,
    MinerURequests,
    RequestCounts,
)


def test_request_counts_snapshot_keeps_attempts_and_responses_separate():
    counts = RequestCounts()
    assert counts.snapshot() == {"requests": 0, "responses": 0}

    counts.requests = 2
    counts.responses = 1
    assert counts.snapshot() == {"requests": 2, "responses": 1}


def test_embedding_usage_preserves_zero_unknown_and_reported_coverage():
    usage = EmbeddingUsage()
    assert usage.snapshot() == {
        "requests": 0,
        "responses": 0,
        "prompt_tokens": {"observed": 0, "reported_responses": 0},
        "total_tokens": {"observed": 0, "reported_responses": 0},
    }

    usage.requests = usage.responses = 1
    usage.observe({"usage": {"prompt_tokens": True, "total_tokens": -1}})
    assert usage.snapshot() == {
        "requests": 1,
        "responses": 1,
        "prompt_tokens": {"observed": None, "reported_responses": 0},
        "total_tokens": {"observed": None, "reported_responses": 0},
    }

    other = EmbeddingUsage(requests=2, responses=1)
    other.observe({"usage": {"prompt_tokens": 0, "total_tokens": 4}})
    usage.add(other)
    assert usage.snapshot() == {
        "requests": 3,
        "responses": 2,
        "prompt_tokens": {"observed": 0, "reported_responses": 1},
        "total_tokens": {"observed": 4, "reported_responses": 1},
    }

    other.observe({"usage": {"prompt_tokens": 9, "total_tokens": 9}})
    assert usage.snapshot()["prompt_tokens"] == {"observed": 0, "reported_responses": 1}
    assert usage.snapshot()["total_tokens"] == {"observed": 4, "reported_responses": 1}


def test_mineru_request_counters_are_independent_by_operation_and_instance():
    counts = MinerURequests()
    before = counts.snapshot()
    counts.poll.requests = 1
    counts.poll.responses = 1

    assert counts.snapshot() == {**before, "poll": {"requests": 1, "responses": 1}}
    assert MinerURequests().snapshot() == before == dict.fromkeys(
        ("submit", "poll", "upload", "download"), {"requests": 0, "responses": 0}
    )


def test_mineru_progress_keeps_known_counts_when_an_update_is_invalid():
    observation = MinerUObservation(data_id="document")
    observation.observe_progress({"total_pages": 4, "extracted_pages": 1})
    observation.observe_progress({"total_pages": True, "extracted_pages": 2})
    assert (observation.total_pages, observation.extracted_pages) == (4, 2)

    observation.observe_progress({"total_pages": 3, "extracted_pages": 5})
    observation.observe_progress({"total_pages": -1, "extracted_pages": -1})
    observation.observe_progress(None)
    assert (observation.total_pages, observation.extracted_pages) == (4, 2)
