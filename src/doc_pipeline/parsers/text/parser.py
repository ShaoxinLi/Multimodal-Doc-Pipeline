from __future__ import annotations

import re
from pathlib import Path

from doc_pipeline.contracts.config import Limits
from doc_pipeline.contracts.core import FileRef, InputFile, PipelineError, TextSpan
from doc_pipeline.contracts.document import (
    Block,
    Code,
    Furniture,
    InlineAssetUse,
    Opaque,
    ParsedContent,
    Text,
    TextBlock,
)
from doc_pipeline.parsed_content.selection import canonical_text
from doc_pipeline.parsers.text.assets import AssetResolver
from doc_pipeline.parsers.text.md_to_blocks import parse_markdown
from doc_pipeline.parsers.text.rst_to_blocks import parse_rst
from doc_pipeline.parsers.text.source import TextSource, markdown_escape
from doc_pipeline.parsers.text.tex_to_blocks import parse_tex

_SUFFIXES = {"markdown": "md", "latex": "tex"}


_PARSERS = {
    "md": parse_markdown,
    "txt": None,
    "rst": parse_rst,
    "tex": parse_tex,
}


_DRAWING = re.compile(r"[-+=|:*~_/\\ \u2500-\u257f]+")


def _is_spatial_layout(plain: str) -> bool:
    lines = [line for line in plain.split("\n") if line.strip()]
    if any(_DRAWING.fullmatch(line.strip()) and len(re.findall(r"[+|\u2500-\u257f]", line)) >= 2 for line in lines):
        return True
    columns = sum(bool(re.search(r"\S(?: {3,}|\t)\S", line)) for line in lines)
    return columns >= 2 and 2 * columns >= len(lines) and all(line.expandtabs(4)[:4] == "    " for line in lines)


def _parse_text(source: TextSource) -> list[TextBlock]:
    records = []
    start = end = 0
    for match in re.finditer(r"[^\n]*(?:\n|$)", source.text):
        if match.group().strip():
            if end == 0:
                start = match.start()
            end = match.end()
        elif end:
            end -= int(source.text[end - 1] == "\n")
            plain = source.text[start:end]
            if _is_spatial_layout(plain):
                payload = Code(text=plain)
            else:
                # Leading indentation would turn escaped prose into a Markdown code block.
                markdown = markdown_escape("\n".join(line.lstrip(" \t") for line in plain.split("\n")))
                payload = Text(markdown=markdown, plain_text=plain)
            records.append(TextBlock(start, end, payload))
            end = 0
    return records


def parse_text_document(source: InputFile, source_ref: FileRef, output_dir: Path, *,
                        max_asset_bytes: int = Limits().max_source_bytes,
                        output_root: Path | None = None) -> ParsedContent:

    suffix = source.path.suffix.lower().removeprefix(".")
    format_ = _SUFFIXES.get(suffix, suffix)
    if format_ not in _PARSERS:
        raise PipelineError("parse", "unsupported_source_format")
    parser = _PARSERS[format_]
    raw = source.path.read_bytes()
    try:
        parsed_source = TextSource(raw.decode(source.encoding), source_ref)
    except (LookupError, UnicodeDecodeError) as exc:
        raise PipelineError("parse", "native_decode_error") from exc
    resolve = AssetResolver(source.assets, output_dir, source_dir=source.path.resolve().parent,
                            max_asset_bytes=max_asset_bytes, output_root=output_root)
    if parser is None:
        records, diagnostics = _parse_text(parsed_source), []
    else:
        records, diagnostics = parser(parsed_source, resolve)

    blocks: list[Block] = []
    uses: list[InlineAssetUse] = []

    def add(record: TextBlock, span: TextSpan) -> None:
        block_id = f"b{len(blocks):04d}"
        original = parsed_source.original[span.codepoints.start : span.codepoints.end]
        payload = record.payload
        if isinstance(payload, Furniture):
            payload = payload.model_copy(update={"text": original})
        elif isinstance(payload, Opaque):
            payload = payload.model_copy(update={"source_text": original})
            diagnostics.append(f"opaque_source: line {span.first_line}: {payload.reason}")
        related = dict.fromkeys([
            *record.related_ranges,
            *(asset.definition for asset in record.images if asset.definition is not None),
        ])
        block = Block(id=block_id, payload=payload)
        locations = [span, *(parsed_source.span(*bounds) for bounds in related)]
        precision = "exact" if not related and canonical_text(block) == original else "enclosing"
        blocks.append(block.model_copy(update={"locations": locations, "location_precision": precision}))
        uses.extend(
            InlineAssetUse(
                owner_block_id=block_id,
                table_cell=asset.table_cell,
                original_reference=asset.reference,
                alt=asset.alt,
                asset=asset.file,
                resolution=asset.resolution,
            )
            for asset in record.images
        )

    def opaque_gap(start: int, end: int) -> None:
        if parsed_source.text[start:end].strip():
            payload = Opaque(source_text="", syntax=format_, reason="source absent from interpreted syntax")
            add(TextBlock(start, end, payload), parsed_source.span(start, end))

    if parsed_source.bom:
        add(TextBlock(0, 0, Furniture(text="", role="syntax")), parsed_source.original_span(0, 1, 1, 1))
    cursor = 0
    for record in sorted(records, key=lambda entry: (entry.start, entry.end)):
        if not cursor <= record.start < record.end <= len(parsed_source.text):
            diagnostics.append(f"invalid_parser_span: {record.start}:{record.end}")
            continue
        opaque_gap(cursor, record.start)
        add(record, parsed_source.span(record.start, record.end))
        cursor = record.end
    opaque_gap(cursor, len(parsed_source.text))
    return ParsedContent(
        blocks=blocks,
        inline_assets=uses,
        parse_status="partial" if any(isinstance(b.payload, Opaque) for b in blocks) else "complete",
        diagnostics=[*diagnostics, *resolve.diagnostics],
    )
