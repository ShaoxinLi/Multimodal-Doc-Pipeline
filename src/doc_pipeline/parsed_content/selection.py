import re
from collections.abc import Iterable

from bs4 import BeautifulSoup

from doc_pipeline.contracts.chunk import CodeLines, Selector, TableRows, TextRange, Whole
from doc_pipeline.contracts.core import Range
from doc_pipeline.contracts.document import (
    Algorithm,
    Block,
    Caption,
    CaptionPart,
    Code,
    Equation,
    Furniture,
    Heading,
    Image,
    Opaque,
    Table,
    Text,
)

_MAX_GRID_CELLS = 1_000_000


TABLE_TAG = re.compile(r"<table(?=[\s/>])", re.IGNORECASE)


def caption_plain_text(value: CaptionPart) -> str:
    return value.plain_text if isinstance(value, Caption) else value


def join_text(items: Iterable[CaptionPart], separator: str = "\n") -> str:
    return separator.join(text for text in map(caption_plain_text, items) if text)


def _escape_cell(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


def _table_layout(table: Table) -> tuple[list[list[int]], int, list[tuple[int, int]]]:
    if len(TABLE_TAG.findall(table.html)) > 1 and len(BeautifulSoup(table.html, "html.parser").find_all("table")) > 1:
        raise ValueError("nested or multiple tables have no shared logical grid")
    rows = table.rows
    if any(header < 0 or header >= len(rows) for header in table.verified_header_rows):
        raise ValueError("verified header row is outside the table")
    occupied: list[set[int]] = [set() for _ in rows]
    row_ends = list(range(1, len(rows) + 1))
    origins: list[list[int]] = []
    width = 0
    for row_index, row in enumerate(rows):
        columns_in_row: list[int] = []
        column = 0
        for cell in row:
            while column in occupied[row_index]:
                column += 1
            next_column = column + cell.colspan
            end = row_index + cell.rowspan
            if end > len(rows):
                raise ValueError("rowspan extends beyond the table")
            if len(rows) * next_column > _MAX_GRID_CELLS:
                raise ValueError("logical table grid is too large")
            columns = set(range(column, next_column))
            for covered in range(row_index, end):
                if not occupied[covered].isdisjoint(columns):
                    raise ValueError("table spans overlap")
                occupied[covered].update(columns)
            row_ends[row_index] = max(row_ends[row_index], end)
            columns_in_row.append(column)
            column = next_column
        width = max(width, column)
        origins.append(columns_in_row)
    groups = []
    start = 0
    while start < len(rows):
        end = row_ends[start]
        index = start + 1
        while index < end:
            end = max(end, row_ends[index])
            index += 1
        groups.append((start, end))
        start = end
    return origins, width, groups


def table_grid(table: Table) -> tuple[list[list[str]], list[tuple[int, int]]]:
    origins, width, groups = _table_layout(table)
    grid = [[""] * width for _ in table.rows]
    for row, cells, columns in zip(grid, table.rows, origins, strict=True):
        for cell, column in zip(cells, columns, strict=True):
            row[column] = _escape_cell(cell.text)
    return grid, groups


def table_cell_origins(table: Table) -> list[list[int]]:
    origins, _, _ = _table_layout(table)
    return origins


def _table_body(table: Table) -> str:
    try:
        grid, _ = table_grid(table)
    except ValueError:
        return "\n".join("\t".join(_escape_cell(cell.text) for cell in row) for row in table.rows)
    return "\n".join("\t".join(row) for row in grid)


def canonical_text(block: Block) -> str:
    payload = block.payload
    match payload:
        case Text(plain_text=text) | Heading(text=text) | Furniture(text=text):
            return text
        case Code() | Algorithm():
            return join_text([join_text(payload.captions), payload.text, join_text(payload.footnotes)], "\n\n")
        case Equation(source=source) | Opaque(source_text=source):
            return source
        case Image():
            return join_text([*payload.captions, payload.alt, payload.chart_text, *payload.footnotes])
        case Table():
            return join_text([join_text(payload.captions), _table_body(payload), join_text(payload.footnotes)], "\n\n")


def code_lines(text: str) -> list[str]:
    lines = text.split("\n")
    return [line + "\n" for line in lines[:-1]] + (lines[-1:] if lines[-1] else [])


def select_text(block: Block, selector: Selector) -> str:
    match selector:
        case TextRange(codepoints=span):
            text = canonical_text(block)
            if span.end <= len(text):
                return text[span.start:span.end]
        case TableRows(row_range=span) if isinstance(block.payload, Table):
            grid, _ = table_grid(block.payload)
            if span.end <= len(grid):
                return "\n".join("\t".join(row) for row in grid[span.start:span.end])
        case CodeLines(line_range=span) if isinstance(block.payload, (Code, Algorithm)):
            lines = code_lines(block.payload.text)
            if span.end <= len(lines):
                return "".join(lines[span.start:span.end])
        case Whole():
            return canonical_text(block)
    raise ValueError(f"{selector.kind} selector outside block {block.id}")


def selector_range(block: Block, selector: Selector) -> Range:
    selected = select_text(block, selector)
    payload = block.payload
    if isinstance(selector, TextRange):
        return selector.codepoints
    if isinstance(selector, Whole):
        return Range(start=0, end=len(selected))
    caption = join_text(payload.captions)
    start = len(caption) + 2 if caption else 0
    if isinstance(selector, CodeLines):
        start += sum(map(len, code_lines(payload.text)[:selector.line_range.start]))
    else:
        grid, _ = table_grid(payload)
        start += sum(len("\t".join(row)) + 1 for row in grid[:selector.row_range.start])
    return Range(start=start, end=start + len(selected))
