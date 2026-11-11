import json
import logging
import os
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Literal

from doc_pipeline.chunking.citations import digest
from doc_pipeline.contracts.config import Config
from doc_pipeline.contracts.core import InputFile, PipelineError, merge_line_ranges
from doc_pipeline.contracts.observations import EmbeddingUsage, MinerUObservation

NA = "n/a"
STAGES = ("intake", "acquire", "parse", "render", "chunk", "embed", "write")
NATIVE_FORMATS = {"md", "markdown", "txt", "rst", "tex", "latex"}
StageName = Literal["intake", "acquire", "parse", "render", "chunk", "embed", "write"]
StageState = Literal[
    "not_started", "running", "succeeded", "failed", "skipped", "not_applicable", "not_reached", "interrupted",
]
RunState = Literal["running", "completed", "failed", "interrupted"]
ProgressStage = Literal[
    "intake", "upload", "remote_parse", "download", "parse", "embedding", "complete", "partial", "failed",
]


@dataclass(frozen=True)
class ProgressEvent:
    index: int
    path: Path
    stage: ProgressStage
    completed: int | None = None
    total: int | None = None
    failed: int = 0


ProgressCallback = Callable[[ProgressEvent], None]


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

    def snapshot(self, state: RunState, finished: RunInstant | None = None) -> dict:
        return {
            "id": self.run_id, "started_at": self.started_at,
            "elapsed_seconds": (finished.monotonic if finished else time.monotonic()) - self.started,
            "state": state,
        }


@dataclass
class Stage:
    state: StageState = "not_started"
    seconds: float | Literal["n/a"] | None = NA
    reason: str = NA
    started: float | None = None

    def start(self) -> None:
        if self.started is not None:
            raise RuntimeError("stage already running")
        self.state, self.reason, self.started = "running", NA, time.monotonic()
        if not isinstance(self.seconds, (int, float)):
            self.seconds = 0.0

    def end(self, state: StageState, elapsed: float | None = None) -> None:
        if self.started is not None:
            self.seconds += time.monotonic() - self.started if elapsed is None else elapsed
            self.started = None
        self.state = state

    def snapshot(self) -> dict:
        seconds = self.seconds
        if self.started is not None:
            seconds += time.monotonic() - self.started
        return {"state": self.state, "seconds": seconds, "reason": self.reason}


TargetUnit = Literal["chunk", "content_line", "source_line", "source_page", "asset", "document", "block"]


@dataclass(frozen=True)
class IssueTarget:
    unit: TargetUnit
    values: tuple[int, ...] | tuple[str, ...] = ()


DOCUMENT = IssueTarget("document")


@dataclass
class IssueGroup:
    stage: StageName
    code: str
    reason: str | None
    action: str
    retryable: bool | Literal["n/a"] | None
    targets: Counter = field(default_factory=Counter)

    def snapshot(self, block_targets: dict[str, IssueTarget], published: bool) -> dict:
        resolved = {}
        unlocated = 0
        for target, count in self.targets.items():
            if target is not None and target.unit == "block":
                target = block_targets.get(target.values[0])
            if target is not None and not published and target.unit in {"chunk", "content_line"}:
                target = None
            if target is None:
                unlocated += count
            else:
                resolved.setdefault(target.unit, set()).update(target.values)
        targets = []
        for unit, values in resolved.items():
            if unit == "document":
                values = NA
            elif unit == "asset":
                values = sorted(values)
            else:
                values = [list(pair) for pair in merge_line_ranges([(value, value) for value in values])]
            targets.append({"unit": unit, "values": values})
        return {
            "stage": self.stage, "code": self.code, "reason": self.reason,
            "action": self.action, "retryable": self.retryable, "occurrences": sum(self.targets.values()),
            "targets": targets, "unlocated": unlocated,
        }


@dataclass
class Issues:
    groups: dict[tuple, IssueGroup] = field(default_factory=dict)
    block_targets: dict[str, IssueTarget] = field(default_factory=dict)
    source_targets: dict[str, IssueTarget] = field(default_factory=dict)

    def add(self, stage: StageName, code: str, reason: str | None = NA, action: str = "noted",
            retryable: bool | Literal["n/a"] | None = NA, target: IssueTarget | None = DOCUMENT) -> IssueGroup:
        key = stage, code, reason, action, retryable
        group = self.groups.setdefault(key, IssueGroup(*key))
        group.targets[target] += 1
        return group

    def snapshot(self, *, published: bool = True) -> list[dict]:
        targets = self.block_targets if published else self.source_targets
        return [group.snapshot(targets, published) for group in self.groups.values()]


@dataclass(frozen=True)
class Failure:
    error: BaseException
    issue: IssueGroup


@dataclass
class ExecutionRecord:
    clock: RunClock
    index: int
    source: InputFile
    config: Config
    on_progress: ProgressCallback | None = field(default=None, repr=False)
    on_checkpoint: Callable[[dict], None] | None = field(default=None, repr=False)
    stages: dict[str, Stage] = field(default_factory=lambda: {name: Stage() for name in STAGES})
    state: RunState = "running"
    issues: Issues = field(default_factory=Issues)
    mineru: MinerUObservation | None = None
    embedding_usage: EmbeddingUsage | None = None
    result: dict = field(default_factory=lambda: dict.fromkeys(("chunks", "locations", "embedding")))
    sha256: str | None = None
    source_bytes: int | None = None
    finished: RunInstant | None = None
    failed_stage: StageName | None = None
    failure_at: RunInstant | None = None
    failure: Failure | None = None

    def __post_init__(self) -> None:
        self.kind = self.source.path.suffix.lower().lstrip(".")
        if self.kind in NATIVE_FORMATS:
            self.stages["acquire"].state = "not_applicable"
        if self.config.embedding is None:
            self.stages["embed"].state = "skipped"
            self.stages["embed"].reason = "disabled"
            self.result["embedding"] = NA

    @property
    def path(self) -> Path:
        return self.source.path

    @property
    def status(self) -> Literal["complete", "partial", "failed"]:
        if self.state in {"failed", "interrupted"}:
            return "failed"
        chunks, embedding = self.result["chunks"], self.result["embedding"]
        if (chunks and (chunks["partial"] or chunks["unparsed"])) or (
            isinstance(embedding, dict) and embedding["failed"]
        ):
            return "partial"
        return "complete"

    def progress(self, stage: ProgressStage, completed: int | None = None,
                 total: int | None = None, failed: int = 0) -> None:
        if self.on_progress is not None:
            self.on_progress(ProgressEvent(self.index, self.path, stage, completed, total, failed))

    def checkpoint(self) -> dict:
        report = self.snapshot()
        if self.on_checkpoint is not None:
            try:
                self.on_checkpoint(report)
            except (OSError, PipelineError):
                self.failed_stage = "write"
                if self.stages["write"].seconds == NA:
                    self.stages["write"].seconds = None
                raise
        return report

    @contextmanager
    def measure(self, stage: StageName) -> Iterator[None]:
        self.stages[stage].start()
        try:
            self.checkpoint()
            yield
        except BaseException as error:
            self.failed_stage = self.failed_stage or stage
            interrupted = isinstance(error, (KeyboardInterrupt, SystemExit)) or self.failed_stage != stage
            self.stages[stage].end("interrupted" if interrupted else "failed")
            raise
        else:
            self.stages[stage].end("succeeded")
            self.checkpoint()

    def fail(self, error: BaseException) -> None:
        if self.state != "running":
            return
        interrupted = isinstance(error, (KeyboardInterrupt, SystemExit))
        self.state = "interrupted" if interrupted else "failed"
        active = next((name for name, stage in self.stages.items() if stage.state == "running"), None)
        pending = next((name for name, stage in self.stages.items() if stage.state == "not_started"), "intake")
        stage = self.failed_stage or active or (error.stage if isinstance(error, PipelineError) else pending)
        stage = {"embedding": "embed", "remote_acquisition": "acquire"}.get(stage, stage)
        if stage not in self.stages:
            stage = "intake"
        self.stages[stage].end("interrupted" if interrupted else "failed")
        for other in self.stages.values():
            if other.state == "running":
                other.end("interrupted")
            if other.state == "not_started":
                other.state, other.reason = "not_reached", "upstream_interrupted" if interrupted else "upstream_failed"
        if isinstance(error, PipelineError):
            code, reason, retryable = error.code, error.reason, error.retryable
        else:
            code = "interrupted" if interrupted else "io_error" if isinstance(error, OSError) else "internal_error"
            reason, retryable = NA, None
        self.failure = Failure(error, self.issues.add(stage, code, reason, "failed", retryable))
        self.result = {"chunks": None, "locations": None, "embedding": NA if self.config.embedding is None else None}
        self.finished = self.failure_at or RunInstant.now()
        self.checkpoint()

    def complete(self, result: dict) -> dict:
        self.state, self.result, self.finished = "completed", result, RunInstant.now()
        try:
            return self.checkpoint()
        except BaseException:
            self.state, self.finished = "running", None
            self.result = dict.fromkeys(("chunks", "locations", "embedding"))
            self.failed_stage = "write"
            raise

    def source_snapshot(self) -> dict:
        path = self.source.path.resolve()
        identity = self.source.identity
        uri = identity.uri if identity and identity.uri else path.as_uri()
        return {
            "name": identity.name if identity and identity.name else path.name,
            "uri": uri,
            "document_id": identity.document_id if identity else str(uuid.uuid5(uuid.NAMESPACE_URL, path.as_uri())),
            "document_version": identity.version if identity and identity.version is not None else NA,
            "format": self.kind, "sha256": self.sha256, "bytes": self.source_bytes,
        }

    def snapshot(self) -> dict:
        stages = {name: stage.snapshot() for name, stage in self.stages.items()}
        stages["intake"]["encoding"] = self.source.encoding if self.kind in NATIVE_FORMATS else NA
        stages["chunk"]["settings"] = self.config.chunking.model_dump()
        profile = self.config.embedding
        stages["embed"]["profile"] = {
            "model": profile.model, "dimensions": profile.dimensions if profile.dimensions is not None else NA,
            "endpoint_sha256": digest(profile.api_base_url.encode()),
        } if profile else NA
        stages["embed"]["usage"] = self.embedding_usage.snapshot() if self.embedding_usage else None if profile else NA
        observed = self.mineru
        stages["acquire"]["service"] = NA if self.kind in NATIVE_FORMATS else {
            "endpoint_sha256": digest(self.config.mineru.api_base_url.encode()),
            "requested": self.config.mineru.parse_options(),
            "observed": {name: getattr(observed, name, None) for name in ("backend", "version", "ocr_enabled")},
            "batch_id": observed.batch_id if observed else None,
            "data_id": observed.data_id if observed else None,
            "progress": {name: getattr(observed, name, None) for name in ("total_pages", "extracted_pages")},
        }
        return {
            "schema_version": 2,
            "pipeline": {"version": version("multimodal-doc-pipeline"),
                         "revision": os.environ.get("DOC_PIPELINE_REVISION") or None},
            "run": {**self.clock.snapshot(self.state, self.finished), "index": self.index},
            "source": self.source_snapshot(), "stages": stages,
            "result": self.result, "issues": self.issues.snapshot(published=self.state == "completed"),
        }

    def log(self) -> None:
        event = {**self.clock.snapshot(self.state, self.finished), "index": self.index,
                 "stages": {name: stage.snapshot() for name, stage in self.stages.items()}}
        logging.log(logging.WARNING if self.state == "failed" else logging.INFO,
                    "document_finished %s", json.dumps(event, separators=(",", ":")))
