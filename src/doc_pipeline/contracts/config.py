import os
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator

from doc_pipeline.contracts.core import Model


def service_url(value: str) -> str:
    parsed = urlsplit(value)
    parsed.port
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("api_base_url must be a plain HTTP(S) service URL")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("non-local endpoints require HTTPS")
    return value.rstrip("/")


class EmbeddingConfig(Model):
    model_config = ConfigDict(hide_input_in_errors=True)

    model: str = Field(min_length=1)
    dimensions: int | None = Field(default=None, gt=0)
    api_base_url: str
    api_key: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)

    _api_base_url = field_validator("api_base_url")(service_url)


class ChunkingConfig(Model):
    model_config = ConfigDict(hide_input_in_errors=True)

    target_input_bytes: int = Field(default=2400, ge=64)
    max_input_bytes: int = Field(default=6000, ge=64)

    @model_validator(mode="after")
    def budgets(self):
        if self.target_input_bytes > self.max_input_bytes:
            raise ValueError("target_input_bytes exceeds max_input_bytes")
        return self


class MinerUConfig(Model):
    model_config = ConfigDict(hide_input_in_errors=True)

    api_base_url: str = "https://mineru.net"
    api_key: SecretStr = Field(default=SecretStr(""), exclude=True, repr=False)
    timeout_seconds: float = Field(default=1800, gt=0)
    batch_size: int = Field(default=50, ge=1, le=50, strict=True)
    model_version: Literal["vlm", "pipeline"] = "vlm"
    enable_formula: bool = Field(default=True, strict=True)
    enable_table: bool = Field(default=True, strict=True)
    language: str = Field(default="ch", pattern=r"^[A-Za-z0-9_-]+$", max_length=32)
    is_ocr: bool = Field(default=False, strict=True)

    def parse_options(self) -> dict:
        return self.model_dump(include={"model_version", "enable_formula", "enable_table", "language", "is_ocr"})

    _api_base_url = field_validator("api_base_url")(service_url)


class Config(Model):
    model_config = ConfigDict(hide_input_in_errors=True)

    request_timeout_seconds: float = Field(default=60, gt=0)
    local_workers: int = Field(default=4, ge=1, le=32, strict=True)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    mineru: MinerUConfig = Field(default_factory=MinerUConfig)
    embedding: EmbeddingConfig | None = None

    @classmethod
    def from_toml(cls, path: Path) -> "Config":
        with path.open("rb") as stream:
            return cls.model_validate(tomllib.load(stream))


def api_key(environment: str, configured: SecretStr) -> str:
    return os.getenv(environment) or configured.get_secret_value()


class Limits(Model):
    max_source_bytes: int = Field(default=200_000_000, gt=0)
    max_pdf_pages: int = Field(default=200, gt=0)
    max_zip_bytes: int = Field(default=500_000_000, gt=0)
    max_zip_members: int = Field(default=10_000, gt=0)
    max_expanded_bytes: int = Field(default=1_000_000_000, gt=0)
    max_member_bytes: int = Field(default=200_000_000, gt=0)
