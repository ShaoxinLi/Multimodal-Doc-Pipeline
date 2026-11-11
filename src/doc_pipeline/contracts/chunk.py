from typing import Annotated, Literal

from pydantic import Field

from doc_pipeline.contracts.core import Model, PagePosition, Range, SheetPosition, SlidePosition


class Whole(Model):
    kind: Literal["whole"] = "whole"


class TextRange(Model):
    kind: Literal["text_range"] = "text_range"
    codepoints: Range


class TableRows(Model):
    kind: Literal["table_rows"] = "table_rows"
    row_range: Range


class CodeLines(Model):
    kind: Literal["code_lines"] = "code_lines"
    line_range: Range


Selector = Annotated[Whole | TextRange | TableRows | CodeLines, Field(discriminator="kind")]


class Part(Model):
    block_id: str
    selector: Selector = Field(default_factory=Whole)


class TextLocation(Model):
    kind: Literal["native"] = "native"
    path: str
    codepoints: Range
    first_line: int
    last_line: int


class PdfLocation(Model):
    kind: Literal["pdf"] = "pdf"
    path: str
    page_index: int
    bbox: tuple[float, float, float, float]
    page_size: tuple[float, float]


class MarkdownLocation(Model):
    codepoints: Range
    first_line: int
    last_line: int
    precision: Literal["exact", "enclosing"]


class Citation(Model):
    block_id: str
    kind: str
    role: Literal["body", "context", "heading"]
    block_text_range: Range
    body_range: Range | None = None
    input_range: Range | None = None
    heading_index: int | None = None
    markdown: MarkdownLocation
    locations: list[Annotated[TextLocation | PdfLocation | PagePosition | SlidePosition | SheetPosition, Field(discriminator="kind")]]
    precision: Literal["exact", "enclosing", "unresolved"]
    reason: str | None = None
    table_rows: Range | None = None
    labels: list[str] = Field(default_factory=list)
    anchors: list[str] = Field(default_factory=list)
    assets: list[str] = Field(default_factory=list)


class Chunk(Model):
    id: int
    body: str
    heading_path: list[str]
    embedding_input: str | None
    citations: list[Citation]
