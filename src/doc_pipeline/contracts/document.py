from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import Field

from doc_pipeline.contracts.core import FileRef, Location, Model, Range


class Heading(Model):
    kind: Literal["heading"] = "heading"
    level: int = Field(ge=1)
    text: str
    markdown: str | None = None


class Text(Model):
    kind: Literal["text"] = "text"
    markdown: str
    plain_text: str
    role: Literal["prose", "list", "footnote"] = "prose"
    atomic: bool = False
    paragraph_start: bool | None = None


class Cell(Model):
    text: str
    rowspan: int = Field(default=1, ge=1, le=1000)
    colspan: int = Field(default=1, ge=1, le=1000)


class Caption(Model):
    plain_text: str
    markdown: str


CaptionPart = str | Caption


class Table(Model):
    kind: Literal["table"] = "table"
    html: str
    rows: list[list[Cell]]
    verified_header_rows: list[int] = Field(default_factory=list)
    captions: list[CaptionPart] = Field(default_factory=list)
    footnotes: list[CaptionPart] = Field(default_factory=list)


class Code(Model):
    kind: Literal["code"] = "code"
    text: str
    language: str | None = None
    captions: list[CaptionPart] = Field(default_factory=list)
    footnotes: list[CaptionPart] = Field(default_factory=list)


class Algorithm(Model):
    kind: Literal["algorithm"] = "algorithm"
    text: str
    markdown: str
    captions: list[CaptionPart] = Field(default_factory=list)
    footnotes: list[CaptionPart] = Field(default_factory=list)


class Equation(Model):
    kind: Literal["equation"] = "equation"
    source: str
    format: str = "latex"


AssetResolution = Literal["archived", "missing_snapshot", "external_unarchived", "blocked_reference", "unresolved"]


class Image(Model):
    kind: Literal["image"] = "image"
    asset: FileRef | None = None
    original_reference: str = ""
    alt: str = ""
    captions: list[CaptionPart] = Field(default_factory=list)
    footnotes: list[CaptionPart] = Field(default_factory=list)
    chart_text: str = ""
    resolution: AssetResolution = "unresolved"


class Opaque(Model):
    kind: Literal["opaque"] = "opaque"
    source_text: str
    syntax: str
    reason: str


class Furniture(Model):
    kind: Literal["furniture"] = "furniture"
    text: str
    role: str


Payload = Annotated[Heading | Text | Table | Code | Algorithm | Equation | Image | Opaque | Furniture,
                    Field(discriminator="kind")]


class Block(Model):
    id: str
    payload: Payload
    anchors: list[str] = Field(default_factory=list)
    semantic_scope: int | None = Field(default=None, ge=0)
    locations: list[Location] = Field(default_factory=list)
    location_precision: Literal["exact", "enclosing", "unresolved"] = "unresolved"
    location_error: str | None = None


class MarkdownMapping(Model):
    block_id: str
    markdown_range: Range
    first_line: int = Field(ge=1)
    last_line_inclusive: int = Field(ge=1)
    relation: Literal["exact", "enclosing"] = "enclosing"


class InlineAssetUse(Model):
    owner_block_id: str
    codepoints: Range | None = None
    table_cell: tuple[int, int] | None = None
    original_reference: str
    alt: str = ""
    asset: FileRef | None = None
    resolution: AssetResolution


class PdfPageMapping(Model):
    number: int = Field(ge=1)
    label: str | None
    basis: Literal["identical_bytes", "equivalent_page", "unmapped"]
    reason: str | None


DiagnosticAction = Literal[
    "retained_opaque", "unverified", "unarchived", "omitted",
    "inferred", "retried", "split", "failed", "noted",
]


class Diagnostic(Model):
    code: str
    action: DiagnosticAction
    reason: str | None = "n/a"
    retryable: bool | Literal["n/a"] | None = "n/a"
    block_id: str | None = None
    source_line: int | None = Field(default=None, ge=1)
    source_page: int | None = Field(default=None, ge=1)
    asset: str | None = None
    document_wide: bool = False


class ProviderMetadata(Model):
    backend: str | None = None
    version: str | None = None
    ocr_enabled: bool | None = None


class ParsedContent(Model):
    blocks: list[Block]
    inline_assets: list[InlineAssetUse] = Field(default_factory=list)
    parse_status: Literal["complete", "partial"] = "complete"
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    pdf_pages: list[PdfPageMapping] = Field(default_factory=list)
    provider_metadata: ProviderMetadata | None = None


@dataclass(frozen=True)
class TextAsset:
    reference: str
    alt: str
    resolution: AssetResolution
    file: FileRef | None = None
    table_cell: tuple[int, int] | None = None
    definition: tuple[int, int] | None = None
    codepoints: Range | None = None


@dataclass
class TextBlock:
    start: int
    end: int
    payload: Payload
    images: list[TextAsset] = field(default_factory=list)
    related_ranges: list[tuple[int, int]] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)
