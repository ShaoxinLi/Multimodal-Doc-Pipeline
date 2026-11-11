from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Range(Model):
    start: int = Field(ge=0)
    end: int = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("range end precedes start")
        return self


class InputFile(Model):
    path: Path
    assets: dict[str, Path] = Field(default_factory=dict)
    encoding: str = "utf-8"


class FileRef(Model):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class TextSpan(Model):
    kind: Literal["native"] = "native"
    source: FileRef
    codepoints: Range
    first_line: int = Field(ge=1)
    last_line_inclusive: int = Field(ge=1)


class PdfRegion(Model):
    kind: Literal["pdf_region"] = "pdf_region"
    source: FileRef
    page_index: int = Field(ge=0)
    bbox: tuple[float, float, float, float]
    page_size: tuple[float, float]

    @model_validator(mode="after")
    def bounded(self):
        width, height = self.page_size
        if width <= 0 or height <= 0:
            raise ValueError("page dimensions must be positive")
        x1, y1, x2, y2 = self.bbox
        if not (0 <= x1 <= x2 <= width and 0 <= y1 <= y2 <= height):
            raise ValueError("page region outside coordinate space")
        return self


Location = Annotated[TextSpan | PdfRegion, Field(discriminator="kind")]


class PipelineError(Exception):
    def __init__(self, stage: str, code: str, retryable: bool = False):
        super().__init__(code)
        self.stage, self.code, self.retryable = stage, code, retryable
