import re
from bisect import bisect_right
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NEWLINE = re.compile(r"\r\n|\r|\n")


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


class SourceIdentity(Model):
    model_config = ConfigDict(strict=True)

    document_id: str = Field(min_length=1)
    version: str | None = Field(default=None, min_length=1)
    uri: str | None = Field(default=None, min_length=1)
    name: str | None = Field(default=None, min_length=1)


class InputFile(Model):
    path: Path
    assets: dict[str, Path] = Field(default_factory=dict)
    encoding: str = "utf-8"
    identity: SourceIdentity | None = None


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


class PagePosition(Model):
    kind: Literal["page"] = "page"
    source: FileRef
    number: int = Field(ge=1)


class SlidePosition(Model):
    kind: Literal["slide"] = "slide"
    source: FileRef
    number: int = Field(ge=1)


class SheetPosition(Model):
    kind: Literal["sheet"] = "sheet"
    source: FileRef
    name: str = Field(min_length=1)


Location = Annotated[TextSpan | PdfRegion | PagePosition | SlidePosition | SheetPosition, Field(discriminator="kind")]


class PipelineError(Exception):
    def __init__(self, stage: str, code: str, retryable: bool = False):
        super().__init__(code)
        self.stage, self.code, self.retryable = stage, code, retryable


def line_ends(text: str) -> list[int]:
    return [match.end() for match in NEWLINE.finditer(text)]


def line_range(ends: list[int], start: int, end: int) -> tuple[int, int]:
    return bisect_right(ends, start) + 1, bisect_right(ends, max(start, end - 1)) + 1


def merge_line_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    result = []
    for first, last in sorted(ranges):
        if result and first <= result[-1][1] + 1:
            result[-1] = (result[-1][0], max(last, result[-1][1]))
        else:
            result.append((first, last))
    return result
