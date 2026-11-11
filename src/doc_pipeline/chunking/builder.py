import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from doc_pipeline.chunking.citations import _LABELS, Citations
from doc_pipeline.contracts.chunk import Chunk, CodeLines, Part, TableRows, TextRange
from doc_pipeline.contracts.config import Config
from doc_pipeline.contracts.core import PdfRegion, Range
from doc_pipeline.contracts.document import (
    Algorithm,
    Block,
    Code,
    Equation,
    Furniture,
    Heading,
    Image,
    MarkdownMapping,
    Opaque,
    ParsedContent,
    Table,
    Text,
)
from doc_pipeline.parsed_content.selection import (
    canonical_text,
    caption_plain_text,
    code_lines,
    join_text,
    table_grid,
)

_MATH = r"\$\$(?:\\.|[^$\\])*\$\$|\$(?:\\.|[^$\\\n])*\$|\\\((?:\\[^)]|[^\\])*\\\)|\\\[(?:\\[^\]]|[^\\])*\\\]"
_SENTENCE_MARK = re.compile(rf"(?P<math>{_MATH})|[.!?](?:\s+|$)|[。！？．][^\S\n]*|\n{{2,}}", re.DOTALL)
_INLINE_MATH = re.compile(_MATH, re.DOTALL)
_SEPARATOR = "\n\n"
_SEPARATOR_BYTES = len(_SEPARATOR)
_CLOSERS = "\"'”’)]}»」』）】"
_SENTENCE_END = ".!?。！？…"
_NUMBER = r"\s*([0-9]+(?:[.-][0-9]+)*|[IVXLCDM]+|[A-Z]\.[0-9]+)(?![0-9A-Za-z_(\[]|[.-][0-9A-Za-z])"
_COMPOSITE = re.compile(r"(?:>|```|~~~|[-+*]\s|\d{1,9}[.)]\s)")


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class _Selection:
    part: Part
    text: str

    @classmethod
    def whole(cls, block: Block, text: str) -> "_Selection":
        return cls(Part(block_id=block.id), text)

    @classmethod
    def text_range(cls, block: Block, text: str, start: int, end: int) -> "_Selection":
        return cls(Part(block_id=block.id, selector=TextRange(codepoints=Range(start=start, end=end))), text[start:end])

    @classmethod
    def rows(cls, block: Block, lines: list[str], start: int, end: int) -> "_Selection":
        return cls(Part(block_id=block.id, selector=TableRows(row_range=Range(start=start, end=end))),
                   "\n".join(lines[start:end]))

    @classmethod
    def code(cls, block: Block, lines: list[str], start: int, end: int) -> "_Selection":
        return cls(Part(block_id=block.id, selector=CodeLines(line_range=Range(start=start, end=end))),
                   "".join(lines[start:end]))


def _prose_ranges(text: str, role: str) -> Iterator[tuple[int, int]]:
    if role == "list":
        for match in re.finditer(r"[^\n]*\n|[^\n]+$", text):
            yield match.span()
        return
    start = 0
    for match in _SENTENCE_MARK.finditer(text):
        if match.lastgroup != "math":
            yield start, match.end()
            start = match.end()
    if start < len(text):
        yield start, len(text)


def _hard_ranges(text: str, start: int, end: int, budget: int) -> Iterator[tuple[int, int]]:
    spans = iter(_INLINE_MATH.finditer(text, start, end))
    span = next(spans, None)
    protected_end = start
    while start < end:
        prefix = text[start:min(end, start + budget)].encode("utf-8")
        cut = start + len(prefix[:budget].decode("utf-8", errors="ignore"))
        whitespace = list(re.finditer(r"\s+", text[start:cut]))
        word_boundary = whitespace[-1].end() + start if whitespace else start
        at_word_boundary = word_boundary >= start + max(1, (cut - start) // 2)
        if at_word_boundary and cut < end:
            cut = word_boundary
        while span is not None and span.end() <= cut:
            protected_end = span.end()
            span = next(spans, None)
        if span is not None and span.start() < cut:
            cut = span.start() if span.start() > start else span.end()
            yield start, cut
            start = cut
            continue
        yield start, cut
        if cut == end:
            break
        start = cut if at_word_boundary else max(protected_end, cut - (cut - start) // 10)


def _ending(text: str) -> str:
    return text.rstrip().rstrip(_CLOSERS).rstrip()[-1:]


def _numbers(kind: str, text: str, *, caption: bool = False) -> set[str]:
    prefix = r"^\s*" if caption else r"(?<![A-Za-z])"
    delimiter = r"(?=\s|[:：]|\.(?:\s|$)|$)" if caption else ""
    pattern = rf"{prefix}{_LABELS[kind]}{_NUMBER}{delimiter}"
    return {match[1].upper() for match in re.finditer(pattern, text, re.IGNORECASE)}


def _prose(block: Block | None) -> str | None:
    payload = block.payload if block else None
    if (not isinstance(payload, Text) or payload.role != "prose" or payload.atomic
            or payload.markdown.lstrip().startswith(">") or not payload.plain_text.strip()):
        return None
    return payload.plain_text


def _adjacent(first: Block, second: Block, *, aligned: bool = True) -> bool:
    if first.semantic_scope != second.semantic_scope:
        return False
    regions = [[item for item in block.locations if isinstance(item, PdfRegion)] for block in (first, second)]
    if not all(regions):
        return True
    if any(len(item) != 1 for item in regions):
        return False
    (a,), (b,) = regions
    overlap = min(a.bbox[2], b.bbox[2]) - max(a.bbox[0], b.bbox[0])
    return (a.page_index == b.page_index and a.bbox[1] <= b.bbox[1]
            and (not aligned or overlap >= min(a.bbox[2] - a.bbox[0], b.bbox[2] - b.bbox[0]) / 2))


def _sides(block: Block, before: Block | None, after: Block | None) -> tuple[bool, bool]:
    payload = block.payload
    left = _prose(before) if before and _adjacent(before, block) else None
    right = _prose(after) if after and _adjacent(block, after) else None
    colon = left is not None and _ending(left) in {":", "："}
    continued = right is not None and after.payload.paragraph_start is False
    match payload:
        case Equation():
            return left is not None and _ending(left) not in _SENTENCE_END, continued
        case Text(atomic=True, role="list"):
            return colon, False
        case Text(atomic=True, role="prose", markdown=markdown):
            return colon and bool(_COMPOSITE.match(markdown.lstrip())), False
        case Code() | Table() | Algorithm() | Image():
            kind = "figure" if isinstance(payload, Image) else payload.kind
            captions = list(map(caption_plain_text, payload.captions))
            if isinstance(payload, Algorithm):
                captions = [*captions, payload.text.split("\n", 1)[0]]
            own = {number for label in captions for number in _numbers(kind, label, caption=True)}
            mentioned = _numbers(kind, left or "")
            labeled = continued and bool(own & _numbers(kind, right))
            if isinstance(payload, Code):
                return colon, labeled
            return bool(own & mentioned), labeled
    return False, False


def _plan(blocks: list[Block]) -> dict[int, list[int]]:
    padded = [None, *blocks, None]
    sides = {
        index: _sides(block, padded[index], padded[index + 2])
        for index, block in enumerate(blocks)
        if not isinstance(block.payload, (Heading, Opaque, Furniture)) and canonical_text(block).strip()
    }
    groups = {}
    for index, (left, right) in sides.items():
        left = left and not sides.get(index - 2, (False, False))[1]
        right = right and not sides.get(index + 2, (False, False))[0]
        if left or right:
            groups[index - left] = [index + 1 + right]
    index = 0
    while index + 2 < len(blocks):
        if not isinstance(blocks[index].payload, Equation):
            index += 1
            continue
        end = index + 1
        while end + 1 < len(blocks):
            connector, equation = blocks[end:end + 2]
            prose = _prose(connector)
            if (prose is None or prose.strip().lower() not in {"and", "or"}
                    or not isinstance(equation.payload, Equation)
                    or not _adjacent(blocks[end - 1], connector, aligned=False)
                    or not _adjacent(connector, equation, aligned=False)
                    or not _adjacent(blocks[end - 1], equation)):
                break
            end += 2
        if end > index + 1:
            overlapping = [(start, stop) for start, stops in groups.items() for stop in stops
                           if start < end and stop > index]
            start = min([index, *(start for start, _ in overlapping)])
            stop = max([end, *(stop for _, stop in overlapping)])
            groups[start] = [stop, *groups.get(start, [])]
        index = end
    return groups


class _Chunker:
    def __init__(self, parsed_content: ParsedContent, mappings: list[MarkdownMapping], config: Config):
        self.target = config.chunking.target_input_bytes
        self.maximum = config.chunking.max_input_bytes
        self.chunks: list[Chunk] = []
        self.headings: list[tuple[int, _Selection]] = []
        self.pending: list[_Selection] = []
        self.section_start = 0
        self.citations = Citations(parsed_content, mappings)

    def _input(
        self, body: str, extra: list[_Selection], template: str = "", heading_count: int | None = None,
    ) -> tuple[str, list[_Selection]]:
        headings = [item for _, item in self.headings[:heading_count]]
        fixed = [item.text for item in extra] + ([template] if template else []) + [body]
        size = sum(_size(item.text) + _SEPARATOR_BYTES for item in headings)
        size += sum(map(_size, fixed)) + (len(fixed) - 1) * _SEPARATOR_BYTES
        first = 0
        while size > self.maximum and first < len(headings):
            size -= _size(headings[first].text) + _SEPARATOR_BYTES
            first += 1
        kept = headings[first:]
        return _SEPARATOR.join([item.text for item in kept] + fixed), kept + extra

    def _emit(self, selected: list[_Selection], *,
              role: Literal["content", "heading", "source"] = "content",
              extra: list[_Selection] | None = None, template: str = "") -> None:
        if not selected:
            return
        body = _SEPARATOR.join(item.text for item in selected)
        if role == "source":
            embedding_input, context = None, []
        else:
            heading_count = -1 if role == "heading" else None
            embedding_input, context = self._input(body, extra or [], template, heading_count)
        ordinal = len(self.chunks)
        parts = [item.part for item in selected]
        context_parts = [item.part for item in context]
        self.chunks.append(Chunk(
            id=ordinal,
            body=body,
            heading_path=[item.text for _, item in self.headings],
            embedding_input=embedding_input,
            citations=self.citations.build(parts, context_parts, [item.part for _, item in self.headings],
                                           template, embedding_input is not None),
        ))

    def _flush(self) -> None:
        self._emit(self.pending)
        self.pending.clear()

    def _heading_only(self) -> None:
        if self.headings and self.section_start == len(self.chunks):
            self._emit([self.headings[-1][1]], role="heading")

    def _pack(self, selected: _Selection) -> None:
        existing = self.pending[-1] if self.pending else None
        last = existing.part.selector if existing else None
        new = selected.part.selector
        if (existing and existing.part.block_id == selected.part.block_id
                and isinstance(last, TextRange) and isinstance(new, TextRange)
                and last.codepoints.end == new.codepoints.start):
            combined = _Selection(
                Part(block_id=existing.part.block_id,
                     selector=TextRange(codepoints=Range(start=last.codepoints.start, end=new.codepoints.end))),
                existing.text + selected.text,
            )
            proposed = [*self.pending[:-1], combined]
        else:
            proposed = [*self.pending, selected]
        text = _SEPARATOR.join(item.text for item in proposed)
        if self.pending and _size(self._input(text, [])[0]) > self.target:
            self._flush()
            self.pending.append(selected)
        else:
            self.pending = proposed

    def _prose(self, block: Block, payload: Text, text: str) -> None:
        if _size(self._input(text, [])[0]) <= self.target:
            self._pack(_Selection.whole(block, text))
            return
        heading_size = sum(_size(item.text) + _SEPARATOR_BYTES for _, item in self.headings)
        hard_budget = max(self.target - heading_size, self.target // 2)
        for start, end in _prose_ranges(text, payload.role):
            if _size(text[start:end]) <= self.maximum:
                self._pack(_Selection.text_range(block, text, start, end))
                continue
            self._flush()
            for line in re.finditer(r"[^\n]*\n|[^\n]+$", text[start:end]):
                left, right = start + line.start(), start + line.end()
                if _size(text[left:right]) <= self.maximum:
                    self._pack(_Selection.text_range(block, text, left, right))
                else:
                    self._flush()
                    for a, b in _hard_ranges(text, left, right, hard_budget):
                        self._emit([_Selection.text_range(block, text, a, b)])

    def convert_html_table(self, block: Block, table: Table, text: str) -> None:
        try:
            grid, groups = table_grid(table)
        except ValueError:
            self._emit([_Selection.whole(block, text)])
            return
        headers = set(table.verified_header_rows)
        data_groups = []
        for start, end in groups:
            count = sum(index in headers for index in range(start, end))
            if 0 < count < end - start:
                self._emit([_Selection.whole(block, text)])
                return
            if count == 0:
                data_groups.append((start, end))
        if not data_groups or not grid[0]:
            self._emit([_Selection.whole(block, text)])
            return

        rows = ["\t".join(row) for row in grid]
        extra = []
        caption = join_text(table.captions)
        footnote = join_text(table.footnotes)
        if caption:
            extra.append(_Selection.text_range(block, text, 0, len(caption)))
        extra.extend(_Selection.rows(block, rows, index, index + 1) for index in sorted(headers))
        if footnote:
            extra.append(_Selection.text_range(block, text, len(text) - len(footnote), len(text)))
        template = "" if headers else "\t".join(f"column_{index + 1}" for index in range(len(grid[0])))
        start, end = data_groups[0]
        for following_start, following_end in data_groups[1:]:
            if following_start == end:
                candidate = _Selection.rows(block, rows, start, following_end)
                if _size(self._input(candidate.text, extra, template)[0]) <= self.target:
                    end = following_end
                    continue
            self._emit([_Selection.rows(block, rows, start, end)], extra=extra, template=template)
            start, end = following_start, following_end
        self._emit([_Selection.rows(block, rows, start, end)], extra=extra, template=template)

    def _code(self, block: Block, text: str) -> None:
        lines = code_lines(block.payload.text)
        extra = []
        caption = join_text(block.payload.captions)
        footnote = join_text(block.payload.footnotes)
        if caption:
            extra.append(_Selection.text_range(block, text, 0, len(caption)))
        if footnote:
            extra.append(_Selection.text_range(block, text, len(text) - len(footnote), len(text)))
        start = 0
        for end in range(2, len(lines) + 1):
            candidate = _Selection.code(block, lines, start, end)
            if _size(self._input(candidate.text, extra)[0]) > self.target:
                self._emit([_Selection.code(block, lines, start, end - 1)], extra=extra)
                start = end - 1
        if lines:
            self._emit([_Selection.code(block, lines, start, len(lines))], extra=extra)
        else:
            self._emit([_Selection.whole(block, text)])

    def run(self, parsed_content: ParsedContent) -> list[Chunk]:
        groups = _plan(parsed_content.blocks)
        resume = 0
        scope = None
        for index, block in enumerate(parsed_content.blocks):
            if index < resume:
                continue
            if block.semantic_scope != scope:
                self._flush()
                self._heading_only()
                self.headings.clear()
                self.section_start = len(self.chunks)
                scope = block.semantic_scope
            for stop in groups.get(index, []):
                selected = [_Selection.whole(item, canonical_text(item))
                            for item in parsed_content.blocks[index:stop]]
                if _size(self._input(_SEPARATOR.join(item.text for item in selected), [])[0]) <= self.target:
                    self._flush()
                    self._emit(selected)
                    resume = stop
                    break
            if index < resume:
                continue
            payload = block.payload
            if isinstance(payload, Furniture):
                continue
            text = canonical_text(block)
            whole = _Selection.whole(block, text)
            if not text.strip():
                if isinstance(payload, Image) or self.citations.assets.get(block.id):
                    self._flush()
                    self._emit([whole], role="source")
                elif isinstance(payload, Opaque):
                    self._flush()
                continue
            if isinstance(payload, Heading):
                self._flush()
                if self.headings and self.headings[-1][0] >= payload.level:
                    self._heading_only()
                while self.headings and self.headings[-1][0] >= payload.level:
                    self.headings.pop()
                self.headings.append((payload.level, whole))
                self.section_start = len(self.chunks)
                continue
            if isinstance(payload, Text) and not payload.atomic:
                self._prose(block, payload, text)
                continue
            self._flush()
            oversize = _size(text) > self.maximum
            if isinstance(payload, Opaque):
                self._emit([whole], role="source")
            elif isinstance(payload, Table) and oversize:
                self.convert_html_table(block, payload, text)
            elif isinstance(payload, (Code, Algorithm)) and oversize:
                self._code(block, text)
            else:
                self._emit([whole])
        self._flush()
        self._heading_only()
        return self.chunks


def make_chunks(parsed_content: ParsedContent, mappings: list[MarkdownMapping], config: Config) -> list[Chunk]:
    return _Chunker(parsed_content, mappings, config).run(parsed_content)
