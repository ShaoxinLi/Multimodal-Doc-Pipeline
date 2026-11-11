from pytest import raises

from doc_pipeline.contracts.chunk import CodeLines, TableRows, TextRange, Whole
from doc_pipeline.contracts.core import Range
from doc_pipeline.contracts.document import Block, Cell, Code, Table, Text
from doc_pipeline.parsed_content.selection import (
    canonical_text,
    select_text,
    table_cell_origins,
    table_grid,
)


def test_grid_reserves_logical_slots_and_joins_transitive_rowspans():
    table = Table(
        html="",
        captions=["Stock", "", "January"],
        footnotes=["Source"],
        rows=[
            [Cell(text="a\\\t\r\nb", rowspan=2), Cell(text="Header", colspan=2)],
            [Cell(text="B", rowspan=2), Cell(text="C")],
            [Cell(text="D"), Cell(text="E")],
            [Cell(text="F")],
        ],
    )
    block = Block(id="table", payload=table)
    assert table_grid(table) == (
        [[r"a\\\t\r\nb", "Header", ""], ["", "B", "C"], ["D", "", "E"], ["F", "", ""]],
        [(0, 3), (3, 4)],
    )
    assert table_cell_origins(table) == [[0, 1], [1, 2], [0, 2], [0]]
    rows = "a\\\\\\t\\r\\nb\tHeader\t\n\tB\tC\nD\t\tE\nF\t\t"
    assert canonical_text(block) == f"Stock\nJanuary\n\n{rows}\n\nSource"
    assert select_text(block, TableRows(row_range=Range(start=1, end=3))) == "\tB\tC\nD\t\tE"


def test_invalid_spans_still_have_whole_lexical_text():
    table = Table(
        html="",
        rows=[[Cell(text="First"), Cell(text="Stays", rowspan=2)], [Cell(text="Overlaps", colspan=2)]],
    )
    block = Block(id="invalid", payload=table)
    with raises(ValueError):
        table_grid(table)
    assert select_text(block, Whole()) == "First\tStays\nOverlaps"
    with raises(ValueError):
        select_text(block, TableRows(row_range=Range(start=0, end=1)))


def test_nested_tables_cannot_provide_a_logical_grid():
    table = Table(html="<table><tr><td><table></table></td></tr></table>", rows=[[Cell(text="inner")]])
    with raises(ValueError):
        table_cell_origins(table)


def test_selector_ranges_use_codepoints_and_lf_code_lines():
    text = Block(id="body", payload=Text(plain_text="中🫠文", markdown="**中🫠文**"))
    code = Block(id="code", payload=Code(text="  x\n\u2028\n"))
    assert select_text(text, TextRange(codepoints=Range(start=1, end=2))) == "🫠"
    assert select_text(code, CodeLines(line_range=Range(start=0, end=2))) == "  x\n\u2028\n"
    with raises(ValueError):
        select_text(code, CodeLines(line_range=Range(start=2, end=3)))
    with raises(ValueError):
        select_text(text, TableRows(row_range=Range(start=0, end=1)))
    with raises(ValueError):
        select_text(text, TextRange(codepoints=Range(start=1, end=4)))
