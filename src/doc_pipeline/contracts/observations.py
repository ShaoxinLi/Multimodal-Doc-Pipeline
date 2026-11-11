from collections.abc import Callable
from dataclasses import dataclass, field

ServiceIssueCallback = Callable[[str, str | None, str, bool], None]
EmbeddingIssueCallback = Callable[[str, str | None, str, bool, list[int]], None]
MinerUIssueCallback = Callable[[int, str, str | None, str, bool], None]


@dataclass
class RequestCounts:
    requests: int = 0
    responses: int = 0

    def snapshot(self) -> dict:
        return {"requests": self.requests, "responses": self.responses}


@dataclass
class MinerURequests:
    submit: RequestCounts = field(default_factory=RequestCounts)
    poll: RequestCounts = field(default_factory=RequestCounts)
    upload: RequestCounts = field(default_factory=RequestCounts)
    download: RequestCounts = field(default_factory=RequestCounts)

    def snapshot(self) -> dict:
        return {name: getattr(self, name).snapshot() for name in ("submit", "poll", "upload", "download")}


@dataclass
class MinerUObservation:
    data_id: str
    batch_id: str | None = None
    total_pages: int | None = None
    extracted_pages: int | None = None
    backend: str | None = None
    version: str | None = None
    ocr_enabled: bool | None = None

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
    observed: int = 0
    reported_responses: int = 0

    def observe(self, count) -> None:
        if type(count) is int and count >= 0:
            self.observed += count
            self.reported_responses += 1

    def add(self, other: "_TokenCount") -> None:
        self.observed += other.observed
        self.reported_responses += other.reported_responses

    def snapshot(self, requests: int) -> dict:
        return {
            "observed": self.observed if self.reported_responses or not requests else None,
            "reported_responses": self.reported_responses,
        }


@dataclass
class EmbeddingUsage(RequestCounts):
    prompt_tokens: _TokenCount = field(default_factory=_TokenCount)
    total_tokens: _TokenCount = field(default_factory=_TokenCount)

    def observe(self, response: dict) -> None:
        usage = response.get("usage")
        if isinstance(usage, dict):
            self.prompt_tokens.observe(usage.get("prompt_tokens"))
            self.total_tokens.observe(usage.get("total_tokens"))

    def add(self, other: "EmbeddingUsage") -> None:
        self.requests += other.requests
        self.responses += other.responses
        self.prompt_tokens.add(other.prompt_tokens)
        self.total_tokens.add(other.total_tokens)

    def snapshot(self) -> dict:
        return {
            **super().snapshot(),
            "prompt_tokens": self.prompt_tokens.snapshot(self.requests),
            "total_tokens": self.total_tokens.snapshot(self.requests),
        }
