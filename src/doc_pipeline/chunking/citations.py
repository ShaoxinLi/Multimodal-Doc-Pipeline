import re
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path

from doc_pipeline.contracts.chunk import (
    Chunk,
    ChunkMetadata,
    Citation,
    MarkdownLocation,
    ObjectLabels,
    Part,
    PdfLocation,
    SourceLocationRef,
    TableRows,
    TextLocation,
    Whole,
)
from doc_pipeline.contracts.core import FileRef, Range, TextSpan
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


def _equation_labels(source: str) -> list[str]:
    labels = []
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
                    return labels
                close = source.find(source[end], end + 1)
                if close < 0 or re.search(r"[\r\n]", source[end:close]):
                    return labels
                index = close + 1
                continue
            if command[1] == "lstinline":
                return labels
            if not depth and (command[1] == "tag" or command[1] == "label" and not command[2]):
                argument = re.match(r"\s*\{(?:\\[^\r\n]|[^{}%\\\r\n])+\}", source[end:])
                if argument is not None:
                    end += argument.end()
                    label = source[index:end]
                    if label not in labels:
                        labels.append(label)
            index = end
            continue
        depth += 1 if char == "{" else -1 if char == "}" and depth else 0
        index += 1
    return labels


def _labels(block: Block) -> list[str]:
    payload = block.payload
    if isinstance(payload, Equation):
        if payload.format != "latex":
            return []
        return _equation_labels(payload.source)
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

    def _assets(self, block: Block, selected: Range, whole: bool) -> list[str]:
        payload = block.payload
        paths = [payload.asset.path] if isinstance(payload, Image) and payload.asset else []
        for use in self.assets[block.id]:
            if not use.asset:
                continue
            if use.table_cell is not None and isinstance(payload, Table) and not whole:
                row, column = use.table_cell
                origins = table_cell_origins(payload)
                cell = payload.rows[row][origins[row].index(column)]
                rows = selector_range(block, TableRows(row_range=Range(start=row, end=row + cell.rowspan)))
                if max(selected.start, rows.start) >= min(selected.end, rows.end):
                    continue
            if use.asset.path not in paths:
                paths.append(use.asset.path)
        return paths

    def selection(self, part: Part, *, role="body", body_range=None, input_range=None,
                  heading_index=None) -> Citation:
        block = self.blocks[part.block_id]
        selected = selector_range(block, part.selector)
        mapping = self.mappings[part.block_id]
        if block.location_precision == "exact" or mapping.relation == "exact":
            line_ends = [match.end() for match in re.finditer(r"\r\n|\r|\n", canonical_text(block))]
            first_offset = bisect_right(line_ends, selected.start)
            last_offset = bisect_right(line_ends, max(selected.start, selected.end - 1))
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
            else:
                locations.append(PdfLocation(path=location.source.path, page_index=location.page_index,
                                             bbox=location.bbox, page_size=location.page_size))
        rendered = mapping.markdown_range
        first, last = mapping.first_line, mapping.last_line_inclusive
        precision = "enclosing"
        if mapping.relation == "exact":
            rendered = Range(start=rendered.start + selected.start, end=rendered.start + selected.end)
            first += first_offset
            last = mapping.first_line + last_offset
            precision = "exact"
        return Citation(
            block_id=block.id, kind=block.payload.kind, role=role, block_text_range=selected,
            body_range=body_range, input_range=input_range, heading_index=heading_index,
            markdown=MarkdownLocation(codepoints=rendered, first_line=first, last_line=last, precision=precision),
            locations=locations, precision=block.location_precision, reason=block.location_error,
            table_rows=part.selector.row_range if isinstance(part.selector, TableRows) else None,
            assets=self._assets(block, selected, isinstance(part.selector, Whole)), labels=_labels(block),
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


_LABEL_FIELDS = {"table": "tables", "algorithm": "algorithms", "equation": "equations",
                 "image": "figures", "code": "code"}


def chunk_records(chunks: Iterable[Chunk], *, source: FileRef, source_type: str,
                  parsed: ParsedContent) -> Iterator[dict]:
    blocks = {block.id: block for block in parsed.blocks}
    document_id = Path(source.path).as_uri()
    for chunk in chunks:
        citations = []
        body = []
        locations = []
        pages: set[int] = set()
        body_locations_filtered = False
        labels = {field: [] for field in _LABEL_FIELDS.values()}
        for citation_index, citation in enumerate(chunk.citations):
            block = blocks[citation.block_id]
            original_locations = [
                location for location, evidence in zip(citation.locations, block.locations, strict=True)
                if evidence.source == source
            ]
            if len(original_locations) != len(citation.locations):
                body_locations_filtered = body_locations_filtered or citation.role == "body"
                citation = citation.model_copy(update={
                    "locations": original_locations,
                    "precision": "unresolved",
                    "reason": "original_source_location_unverified",
                })
            citations.append(citation)
            locations.extend(
                SourceLocationRef(citation_index=citation_index, location_index=location_index,
                                  coordinate_space="original", source=source)
                for location_index in range(len(citation.locations))
            )
            if citation.role != "body":
                continue
            body.append(citation)
            pages.update(location.page_index for location in citation.locations if isinstance(location, PdfLocation))
            if citation.kind in _LABEL_FIELDS:
                target = labels[_LABEL_FIELDS[citation.kind]]
                target.extend(label for label in citation.labels if label not in target)
        covered = sum(any(isinstance(location, PdfLocation) for location in citation.locations) for citation in body)
        scopes = {blocks[citation.block_id].semantic_scope for citation in body}
        if pages:
            page_status = "known" if covered == len(body) and not body_locations_filtered else "partial"
        elif body and not body_locations_filtered and all(
            citation.locations and all(isinstance(location, TextLocation) for location in citation.locations)
            for citation in body
        ):
            page_status = "nonpaged"
        else:
            page_status = "unavailable"
        metadata = ChunkMetadata(
            document_id=document_id, source=source, source_type=source_type,
            page_indices=sorted(pages) if pages else None,
            page_coordinate_space="original" if pages else None,
            page_status=page_status, section_path=chunk.heading_path,
            slide_indices=sorted(scopes) if source_type in {"ppt", "pptx"} and scopes and None not in scopes else None,
            block_ids=list(dict.fromkeys(citation.block_id for citation in body)),
            labels=ObjectLabels(**labels), source_locations=locations,
        )
        exported = chunk.model_copy(update={"citations": citations})
        yield {**exported.model_dump(mode="json", exclude_none=True), "embedding_input": chunk.embedding_input,
               "metadata": metadata.model_dump(mode="json")}
