import hashlib
import json
import re
from collections import defaultdict

from doc_pipeline.contracts.chunk import (
    Chunk,
    ChunkCitations,
    ChunkMetadata,
    Citation,
    ExportedChunk,
    MarkdownLocation,
    ObjectCitation,
    Page,
    Part,
    PdfLocation,
    Selector,
    Sheet,
    TablePart,
    TableRows,
    TextLocation,
    Whole,
)
from doc_pipeline.contracts.core import (
    FileRef,
    PagePosition,
    PdfRegion,
    Range,
    SheetPosition,
    SlidePosition,
    SourceIdentity,
    TextSpan,
    line_ends,
    line_range,
    merge_line_ranges,
)
from doc_pipeline.contracts.document import (
    Algorithm,
    Block,
    Code,
    Equation,
    Image,
    MarkdownMapping,
    ParsedContent,
    Table,
)
from doc_pipeline.parsed_content.selection import (
    canonical_text,
    caption_plain_text,
    selector_range,
    table_cell_origins,
)

_LABELS = {"table": r"(?:Table|表)", "figure": r"(?:Figure|Fig\.?|图)",
           "algorithm": r"(?:Algorithm|算法)", "code": r"(?:Listing|Code|代码)"}

_OBJECTS = {"table": "table", "image": "figure", "equation": "equation", "algorithm": "algorithm", "code": "code"}


def _equation_marks(source: str) -> tuple[list[str], list[str]]:
    labels, anchors = [], []
    index = depth = 0
    while index < len(source):
        char = source[index]
        if char == "%":
            newline = re.search(r"[\r\n]", source[index:])
            index = index + newline.end() if newline else len(source)
            continue
        if char == "\\":
            command = re.match(r"\\([A-Za-z]+)(\*?)", source[index:])
            if command is None:
                index += 2
                continue
            end = index + command.end()
            if command[1] == "verb":
                if end >= len(source) or source[end].isspace():
                    return labels, anchors
                close = source.find(source[end], end + 1)
                if close < 0 or re.search(r"[\r\n]", source[end:close]):
                    return labels, anchors
                index = close + 1
                continue
            if command[1] == "lstinline":
                return labels, anchors
            if not depth and (command[1] == "tag" or command[1] == "label" and not command[2]):
                argument = re.match(r"\s*\{(?:\\[^\r\n]|[^{}%\\\r\n])+\}", source[end:])
                if argument is not None:
                    end += argument.end()
                    value = argument.group().strip()[1:-1]
                    if command[1] == "label":
                        if value not in anchors:
                            anchors.append(value)
                    else:
                        label = value if command[2] else f"({value})"
                        if label not in labels:
                            labels.append(label)
            index = end
            continue
        depth += 1 if char == "{" else -1 if char == "}" and depth else 0
        index += 1
    return labels, anchors


def _labels(block: Block) -> list[str]:
    payload = block.payload
    if not isinstance(payload, (Table, Image, Algorithm, Code)):
        return []
    kind = "figure" if isinstance(payload, Image) else payload.kind
    candidates = list(map(caption_plain_text, payload.captions))
    if isinstance(payload, Algorithm):
        candidates.append(payload.text.split("\n", 1)[0])
    if isinstance(payload, Table) and payload.rows and len(payload.rows[0]) == 1 and payload.rows[0][0].colspan > 1:
        candidates = [*candidates, payload.rows[0][0].text]
    labels = []
    for text in candidates:
        match = re.match(
            rf"\s*({_LABELS[kind]}\s*(?:[0-9]+(?:[.-][0-9]+)*|[IVXLCDM]+|[A-Z]\.[0-9]+))(?=\s|[:.：]|$)",
            text, re.IGNORECASE,
        )
        if match and match[1] not in labels:
            labels.append(match[1])
    return labels


class Citations:
    def __init__(self, parsed_content: ParsedContent, mappings: list[MarkdownMapping]):
        self.blocks = {block.id: block for block in parsed_content.blocks}
        self.mappings = {item.block_id: item for item in mappings}
        self.assets = defaultdict(list)
        for use in parsed_content.inline_assets:
            self.assets[use.owner_block_id].append(use)

    def _assets(self, block: Block, selected: Range, selector: Selector) -> list[str]:
        payload = block.payload
        primary = payload.asset if isinstance(payload, Image) else None
        paths = [primary.path] if primary else []
        for use in self.assets[block.id]:
            if not use.asset:
                continue
            if primary and use.asset == primary and use.original_reference == payload.original_reference:
                primary = None
                continue
            if use.table_cell is not None and isinstance(payload, Table) and not isinstance(selector, Whole):
                row, column = use.table_cell
                origins = table_cell_origins(payload)
                cell = payload.rows[row][origins[row].index(column)]
                if isinstance(selector, TableRows):
                    rows = selector.row_range
                    if row >= rows.end or row + cell.rowspan <= rows.start:
                        continue
                else:
                    continue
            if use.codepoints is not None and not isinstance(selector, Whole):
                span = use.codepoints
                if span.start == span.end:
                    eof = len(canonical_text(block))
                    if not (selected.start <= span.start < selected.end or span.start == selected.end == eof):
                        continue
                elif max(selected.start, span.start) >= min(selected.end, span.end):
                    continue
            paths.append(use.asset.path)
        return paths

    def selection(self, part: Part, *, role="body", body_range=None, input_range=None,
                  heading_index=None) -> Citation:
        block = self.blocks[part.block_id]
        selected = selector_range(block, part.selector)
        mapping = self.mappings[part.block_id]
        if block.location_precision == "exact" or mapping.relation == "exact":
            first_line, last_line = line_range(line_ends(canonical_text(block)), selected.start, selected.end)
            first_offset, last_offset = first_line - 1, last_line - 1
        locations = []
        for location in block.locations:
            if isinstance(location, TextSpan):
                span = location.codepoints
                first, last = location.first_line, location.last_line_inclusive
                if block.location_precision == "exact":
                    span = Range(start=span.start + selected.start, end=span.start + selected.end)
                    first += first_offset
                    last = location.first_line + last_offset
                locations.append(TextLocation(path=location.source.path, codepoints=span,
                                                first_line=first, last_line=last))
            elif isinstance(location, PdfRegion):
                locations.append(PdfLocation(path=location.source.path, page_index=location.page_index,
                                             bbox=location.bbox, page_size=location.page_size))
            else:
                locations.append(location)
        rendered = mapping.markdown_range
        first, last = mapping.first_line, mapping.last_line_inclusive
        precision = "enclosing"
        if mapping.relation == "exact":
            rendered = Range(start=rendered.start + selected.start, end=rendered.start + selected.end)
            first += first_offset
            last = mapping.first_line + last_offset
            precision = "exact"
        if isinstance(block.payload, Equation) and block.payload.format == "latex":
            labels, anchors = _equation_marks(block.payload.source)
        else:
            labels, anchors = _labels(block), []
        return Citation(
            block_id=block.id, kind=block.payload.kind, role=role, block_text_range=selected,
            body_range=body_range, input_range=input_range, heading_index=heading_index,
            markdown=MarkdownLocation(codepoints=rendered, first_line=first, last_line=last, precision=precision),
            locations=locations, precision=block.location_precision, reason=block.location_error,
            table_rows=part.selector.row_range if isinstance(part.selector, TableRows) else None,
            assets=self._assets(block, selected, part.selector), labels=labels,
            anchors=list(dict.fromkeys([*block.anchors, *anchors])),
        )

    def build(self, parts: list[Part], context: list[Part], headings: list[Part],
              template: str, embedded: bool) -> list[Citation]:
        result = []
        input_offset = 0
        for part in context:
            span = selector_range(self.blocks[part.block_id], part.selector)
            end = input_offset + span.end - span.start
            heading_index = headings.index(part) if part in headings else None
            result.append(self.selection(part, role="heading" if heading_index is not None else "context",
                                         input_range=Range(start=input_offset, end=end), heading_index=heading_index))
            input_offset = end + 2
        if template:
            input_offset += len(template) + 2
        body_offset = 0
        for part in parts:
            span = selector_range(self.blocks[part.block_id], part.selector)
            end = body_offset + span.end - span.start
            result.append(self.selection(part, body_range=Range(start=body_offset, end=end),
                                         input_range=Range(start=input_offset + body_offset, end=input_offset + end)
                                         if embedded else None,
                                         heading_index=headings.index(part) if part in headings else None))
            body_offset = end + 2
        for index, part in enumerate(headings):
            if part not in context and part not in parts:
                result.append(self.selection(part, role="heading", heading_index=index))
        return result


def export_chunks(chunks: list[Chunk], parsed: ParsedContent, *, source: FileRef | None, identity: SourceIdentity,
                  source_type: str, asset_digests: dict[str, str]):
    blocks = {block.id: block for block in parsed.blocks}
    page_labels = {page.number: page.label for page in parsed.pdf_pages if page.basis != "unmapped"}
    for chunk in chunks:
        pages, slides, sheets, lines, ranges, objects, uses = set(), set(), [], [], [], [], []
        coverage = []
        opaque = 0
        for citation in chunk.citations:
            if citation.role != "body":
                continue
            block = blocks[citation.block_id]
            opaque += citation.kind == "opaque"
            ranges.append((citation.markdown.first_line, citation.markdown.last_line))
            uses.extend(citation.assets)
            originals = {loc.number for loc in block.locations if isinstance(loc, PagePosition)
                         and loc.source == source}
            office_location = False
            located = []
            for index, (location, raw) in enumerate(zip(citation.locations, block.locations, strict=True)):
                original = raw.source == source
                if isinstance(location, PdfLocation):
                    if original:
                        pages.add(location.page_index + 1)
                    located.append(original or location.page_index + 1 in originals)
                elif isinstance(location, PagePosition) and original:
                    pages.add(location.number)
                elif isinstance(location, TextLocation) and original and index == 0:
                    lines.append((location.first_line, location.last_line))
                    located.append(True)
                elif isinstance(location, SlidePosition) and original:
                    slides.add(location.number)
                    office_location = True
                elif isinstance(location, SheetPosition) and original:
                    sheets.append(location.name)
                    office_location = True
                elif not isinstance(location, TextLocation) or index == 0:
                    located.append(False)
            coverage.extend([True] if office_location else located or [bool(originals)])
            if citation.kind in _OBJECTS:
                body = citation.body_range
                rows = citation.table_rows
                objects.append(ObjectCitation(
                    kind=_OBJECTS[citation.kind], labels=citation.labels, anchors=citation.anchors,
                    body_range=(body.start, body.end),
                    part=TablePart(table_rows=(rows.start, rows.end)) if rows else None,
                ))

        yield ExportedChunk(
            id=chunk.id, body=chunk.body, heading_path=chunk.heading_path,
            embedding_input=chunk.embedding_input, assets=list(dict.fromkeys(uses)),
            citations=ChunkCitations(
                source_name=identity.name, source_uri=identity.uri,
                pages=[Page(number=number, label=page_labels.get(number)) for number in sorted(pages)] or None,
                slides=sorted(slides) or None,
                sheets=[Sheet(name=name, cells=None) for name in dict.fromkeys(sheets)] or None,
                source_lines=merge_line_ranges(lines) or None, objects=objects, content_ranges=merge_line_ranges(ranges),
                location_status=("known" if all(coverage) else "partial") if any(coverage) else "unavailable",
            ),
            metadata=ChunkMetadata(
                document_id=identity.document_id, document_version=identity.version,
                source_sha256=source.sha256 if source else None,
                chunk_sha256=chunk_digest(chunk.body, chunk.heading_path, chunk.embedding_input,
                                          [asset_digests[key] for key in uses]),
                source_type=source_type,
                content_status=("unparsed" if opaque == len(ranges) else "partial") if opaque else "parsed",
            ),
        )


def canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def chunk_digest(body: str, heading_path: list[str], embedding_input: str | None, content_assets: list[str]) -> str:
    return digest(canonical({
        "body": body,
        "heading_path": heading_path,
        "embedding_input": embedding_input,
        "content_assets": content_assets,
    }))
