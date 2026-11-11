import json
import logging
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal

from doc_pipeline.contracts.core import PipelineError


@dataclass
class MinerUObservation:
    data_id: str
    batch_id: str | None = None
    total_pages: int | None = None
    extracted_pages: int | None = None

    def observe_progress(self, progress) -> None:
        if not isinstance(progress, dict):
            return
        total, extracted = progress.get("total_pages"), progress.get("extracted_pages")
        total = total if type(total) is int and total >= 0 else self.total_pages
        extracted = extracted if type(extracted) is int and extracted >= 0 else self.extracted_pages
        if total is None or extracted is None or extracted <= total:
            self.total_pages, self.extracted_pages = total, extracted


@dataclass
class _TokenCount:
    observed_tokens: int = 0
    responses_with_usage: int = 0

    def observe(self, count) -> None:
        if type(count) is int and count >= 0:
            self.observed_tokens += count
            self.responses_with_usage += 1

    def add(self, other: "_TokenCount") -> None:
        self.observed_tokens += other.observed_tokens
        self.responses_with_usage += other.responses_with_usage


@dataclass
class EmbeddingUsage:
    responses: int = 0
    prompt_tokens: _TokenCount = field(default_factory=_TokenCount)
    total_tokens: _TokenCount = field(default_factory=_TokenCount)

    def observe(self, usage) -> None:
        self.responses += 1
        if isinstance(usage, dict):
            self.prompt_tokens.observe(usage.get("prompt_tokens"))
            self.total_tokens.observe(usage.get("total_tokens"))

    def add(self, other: "EmbeddingUsage") -> None:
        self.responses += other.responses
        self.prompt_tokens.add(other.prompt_tokens)
        self.total_tokens.add(other.total_tokens)

    def snapshot(self) -> dict:
        usage = {"responses": self.responses}
        for name in ("prompt_tokens", "total_tokens"):
            count = getattr(self, name)
            usage[name] = {
                "observed_tokens": count.observed_tokens if count.responses_with_usage else None,
                "responses_with_usage": count.responses_with_usage,
            }
        return usage


@dataclass(frozen=True)
class RunError:
    stage: str
    code: str
    retryable: bool = False


@dataclass(frozen=True)
class RunInstant:
    timestamp: str
    monotonic: float

    @classmethod
    def now(cls) -> "RunInstant":
        return cls(datetime.now(UTC).isoformat(), time.monotonic())


@dataclass
class RunClock:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    started: float = field(default_factory=lambda: time.monotonic())

    def snapshot(self, finished: RunInstant | None = None) -> dict:
        if finished is None:
            finished = RunInstant.now()
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": finished.timestamp,
            "elapsed_seconds": finished.monotonic - self.started,
        }


@dataclass
class ExecutionRecord:
    clock: RunClock
    index: int
    stage_seconds: dict[str, float] = field(default_factory=dict)
    parse_status: Literal["complete", "partial"] | None = None
    chunk_count: int = 0
    embedding_count: int = 0
    embedding_error_count: int = 0
    mineru: MinerUObservation | None = None
    embedding_usage: EmbeddingUsage | None = None
    error: RunError | None = None
    failed_stage: str | None = None
    _timing: dict = field(init=False, repr=False, compare=False)

    @property
    def status(self) -> Literal["complete", "partial", "failed"]:
        if self.error:
            return "failed"
        if self.parse_status == "partial" or self.embedding_error_count:
            return "partial"
        return "complete"

    @contextmanager
    def measure(self, stage: str):
        started = time.monotonic()
        try:
            yield
        except Exception:
            self.failed_stage = stage
            raise
        finally:
            self.stage_seconds[stage] = self.stage_seconds.get(stage, 0) + time.monotonic() - started

    def finish(self, at: RunInstant | None = None) -> None:
        self._timing = {**self.clock.snapshot(at), "stage_seconds": self.stage_seconds.copy()}

    def fail(self, error: PipelineError | OSError | ValueError, *, at: RunInstant | None = None) -> None:
        if self.error is not None:
            return
        if isinstance(error, PipelineError):
            self.error = RunError(self.failed_stage or error.stage, error.code, error.retryable)
        else:
            code = "io_error" if isinstance(error, OSError) else "invalid_input"
            self.error = RunError(self.failed_stage or "intake", code)
        self.finish(at)

    def snapshot(self, mineru_options: dict) -> dict:
        record = {
            **self._timing,
            "status": self.status,
            "parse_status": self.parse_status,
            "chunk_count": self.chunk_count,
            "embedding_count": self.embedding_count,
            "embedding_error_count": self.embedding_error_count,
        }
        if self.mineru:
            record["mineru"] = {"options": mineru_options, **asdict(self.mineru)}
        if self.embedding_usage:
            record["embedding_usage"] = self.embedding_usage.snapshot()
        if self.error:
            record["error"] = asdict(self.error)
        return record

    def log(self) -> None:
        event = {
            **self._timing,
            "index": self.index,
            "status": self.status,
            "chunk_count": self.chunk_count,
            "embedding_count": self.embedding_count,
            "embedding_error_count": self.embedding_error_count,
        }
        if self.error:
            event["error"] = asdict(self.error)
        level = logging.WARNING if self.error else logging.INFO
        logging.log(level, "document_finished %s", json.dumps(event, separators=(",", ":")))
