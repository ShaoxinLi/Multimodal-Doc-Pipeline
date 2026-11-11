import hashlib
import json
from typing import Annotated, Literal

from pydantic import ConfigDict, Field

from doc_pipeline.contracts.chunk import Citation
from doc_pipeline.contracts.config import ChunkingConfig
from doc_pipeline.contracts.core import FileRef, Model, SourceIdentity
from doc_pipeline.contracts.document import Block, InlineAssetUse, MarkdownMapping, PdfPageMapping

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
    source_sha256: Sha256
    chunk_sha256: Sha256
    source_type: str
    content_status: Literal["parsed", "partial", "unparsed"]


class ChunkPayload(Model):
    model_config = ConfigDict(strict=True)

    id: Ordinal
    body: str
    heading_path: list[str]
    embedding_input: str | None
    assets: list[str]
    citations: ChunkCitations
    metadata: ChunkMetadata


class ExportedChunk(ChunkPayload):
    artifact_id: Sha256


class BlockEvidence(Model):
    block: Block
    canonical_text: str
    mapping: MarkdownMapping
    resource_uses: list[InlineAssetUse]


class ChunkEvidence(Model):
    id: Ordinal
    selections: list[Citation]


class Resource(Model):
    file: FileRef
    media_type: str


class ManifestIdentity(Model):
    chunk_hash_recipe: Literal["sha256-canonical-json"] = "sha256-canonical-json"
    producer: str
    source: FileRef
    source_identity: SourceIdentity
    document_id_origin: Literal["supplied", "path"]
    source_type: str
    input_encoding: str
    dependencies: dict[str, FileRef]
    parse_options: dict[str, str | bool]
    native_profile: dict[str, str]
    chunking: ChunkingConfig
    parse_status: Literal["complete", "partial"]
    pdf_pages: list[PdfPageMapping]
    diagnostics: list[str]
    resources: dict[str, Resource]
    native_members: dict[str, str]
    files: dict[str, Sha256]
    chunk_count: Ordinal
    chunk_rows_sha256: Sha256


class Manifest(ManifestIdentity):
    artifact_id: Sha256


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def jsonl(rows) -> bytes:
    return b"".join(canonical(row) + b"\n" for row in rows)


def chunk_digest(body: str, heading_path: list[str], embedding_input: str | None, content_assets: list[str]) -> str:
    return digest(canonical({
        "body": body,
        "heading_path": heading_path,
        "embedding_input": embedding_input,
        "content_assets": content_assets,
    }))
