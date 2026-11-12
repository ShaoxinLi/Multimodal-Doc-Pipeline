from typing import Annotated, Literal

from pydantic import ConfigDict, Field

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


Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Positive = Annotated[int, Field(ge=1)]
Ordinal = Annotated[int, Field(ge=0)]
LineRange = tuple[Positive, Positive]


class Page(Model):
    number: Positive
    label: str | None


class Sheet(Model):
    name: str
    cells: list[str] | None


class TablePart(Model):
    table_rows: tuple[Ordinal, Ordinal]


class ObjectCitation(Model):
    kind: Literal["table", "figure", "equation", "algorithm", "code"]
    labels: list[str]
    anchors: list[str]
    body_range: tuple[Ordinal, Ordinal]
    part: TablePart | None


class ChunkCitations(Model):
    source_name: str
    source_uri: str
    pages: list[Page] | None
    slides: list[Positive] | None
    sheets: list[Sheet] | None
    source_lines: list[LineRange] | None
    objects: list[ObjectCitation]
    content_ranges: list[LineRange]
    location_status: Literal["known", "partial", "unavailable"]


class ChunkMetadata(Model):
    document_id: str
    document_version: str | None
    source_sha256: Sha256 | None
    chunk_sha256: Sha256
    source_type: str
    content_status: Literal["parsed", "partial", "unparsed"]


class ExportedChunk(Model):
    model_config = ConfigDict(strict=True)

    id: Ordinal
    body: str
    heading_path: list[str]
    embedding_input: str | None
    assets: list[str]
    citations: ChunkCitations
    metadata: ChunkMetadata
