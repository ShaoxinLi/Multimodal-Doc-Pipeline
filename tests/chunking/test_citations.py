from pathlib import Path

import pytest

from doc_pipeline import process_file
from doc_pipeline.chunking.builder import make_chunks
from doc_pipeline.chunking.citations import Citations, chunk_records
from doc_pipeline.contracts.chunk import Part, TableRows, TextRange
from doc_pipeline.contracts.config import Config
from doc_pipeline.contracts.core import FileRef, PdfRegion, Range, TextSpan
from doc_pipeline.contracts.document import (
    Block,
    Cell,
    Equation,
    Heading,
    Image,
    InlineAssetUse,
    ParsedContent,
    Table,
    Text,
)
from doc_pipeline.parsed_content.md_renderer import render_markdown
from tests.support import read_output

SOURCE = FileRef(path="/input/doc.txt", sha256="a" * 64)
KEYS = {"document_id", "document_version", "source", "source_type", "page_indices", "page_coordinate_space",
        "page_status", "slide_indices", "section_path", "block_ids", "labels", "source_locations"}


def citations(parsed_content):
    _, mappings = render_markdown(parsed_content)
    return Citations(parsed_content, mappings)


def test_omitted_heading_still_has_citation_without_duplicate_context_graph():
    parsed_content = ParsedContent(blocks=[
        Block(id="h1", payload=Heading(text="Outer" * 20, level=1)),
        Block(id="h2", payload=Heading(text="Inner", level=2)),
        Block(id="p", payload=Text(plain_text="B" * 43, markdown="B" * 43)),
    ])
    _, mappings = render_markdown(parsed_content)
    config = Config(chunking={"target_input_bytes": 64, "max_input_bytes": 100})
    [chunk] = make_chunks(parsed_content, mappings, config)
    assert chunk.heading_path == ["Outer" * 20, "Inner"]
    assert chunk.embedding_input == "Inner\n\n" + "B" * 43
    assert len(chunk.citations) == 3
    indexed = {c.block_id: c for c in chunk.citations}
    assert indexed["h1"].heading_index == 0 and indexed["h1"].input_range is None
    assert indexed["h2"].heading_index == 1 and indexed["h2"].input_range == Range(start=0, end=5)
    assert indexed["p"].body_range == Range(start=0, end=43)
    assert indexed["p"].input_range == Range(start=7, end=50)
    assert indexed["p"].locations == [] and indexed["p"].precision == "unresolved"


@pytest.mark.parametrize("start,end,first,last", [(4, 5, 2, 2), (1, 4, 1, 1), (4, 8, 2, 3)])
def test_exact_text_unicode_crlf_ranges_clip_without_search(start, end, first, last):
    value = "A😀\r\nB\r\nC"
    block = Block(id="p", payload=Text(plain_text=value, markdown=value), location_precision="exact",
                  locations=[TextSpan(source=SOURCE, codepoints=Range(start=10, end=10 + len(value)),
                                        first_line=1, last_line_inclusive=3)])
    citation = citations(ParsedContent(blocks=[block])).selection(
        Part(block_id="p", selector=TextRange(codepoints=Range(start=start, end=end))))
    [location] = citation.locations
    assert location.path == SOURCE.path
    assert location.codepoints == Range(start=10 + start, end=10 + end)
    assert (location.first_line, location.last_line) == (first, last)
    assert citation.markdown.codepoints == Range(start=start, end=end)
    assert (citation.markdown.first_line, citation.markdown.last_line) == (first, last)


def test_enclosing_multiple_source_locations_remain_honest():
    locations = [TextSpan(source=SOURCE, codepoints=Range(start=start, end=end),
                            first_line=1, last_line_inclusive=3) for start, end in [(0, 40), (80, 100)]]
    block = Block(id="p", payload=Text(plain_text="repeat repeat", markdown="**repeat** repeat"),
                  locations=locations, location_precision="enclosing")
    citation = citations(ParsedContent(blocks=[block])).selection(
        Part(block_id="p", selector=TextRange(codepoints=Range(start=2, end=13))))
    assert citation.precision == "enclosing"
    assert [loc.codepoints for loc in citation.locations] == [loc.codepoints for loc in locations]
    assert citation.markdown.precision == "enclosing"
    assert citation.markdown.codepoints == Range(start=0, end=17)


def test_table_row_citation_keeps_label_and_spanning_image():
    table = Block(id="table", payload=Table(html="", captions=["TABLE IV Results"], rows=[
        [Cell(text="Shared", rowspan=2), Cell(text="A")], [Cell(text="B")], [Cell(text="C"), Cell(text="D")]]))
    use = InlineAssetUse(owner_block_id="table", table_cell=(0, 0), original_reference="a.png",
                         resolution="archived", asset=FileRef(path="assets/a.png", sha256="c" * 64))
    builder = citations(ParsedContent(blocks=[table], inline_assets=[use]))
    selected = builder.selection(Part(block_id="table", selector=TableRows(row_range=Range(start=1, end=2))))
    assert selected.labels == ["TABLE IV"]
    assert selected.assets == ["assets/a.png"]
    assert selected.table_rows == Range(start=1, end=2)
    assert not builder.selection(Part(block_id="table", selector=TableRows(row_range=Range(start=2, end=3)))).assets


@pytest.mark.parametrize("kind", ["image", "inline", "table"])
def test_image_only_chunks_keep_assets_and_pdf_locations_without_fake_embedding_text(kind):
    asset = FileRef(path="assets/figure.png", sha256="c" * 64)
    payload = {
        "image": Image(asset=asset, resolution="archived"),
        "inline": Text(plain_text=" ", markdown="![](assets/figure.png)"),
        "table": Table(html="", rows=[[Cell(text="")]]),
    }[kind]
    block = Block(id="figure", payload=payload, location_precision="enclosing", locations=[
        PdfRegion(source=FileRef(path="provider.pdf", sha256="b" * 64), page_index=0,
                  bbox=(0, 0, 20, 20), page_size=(100, 100)),
    ])
    uses = [] if kind == "image" else [InlineAssetUse(
        owner_block_id=block.id, table_cell=(0, 0) if kind == "table" else None,
        original_reference="figure.png", resolution="archived", asset=asset)]
    parsed_content = ParsedContent(blocks=[block], inline_assets=uses)
    _, mappings = render_markdown(parsed_content)
    [chunk] = make_chunks(parsed_content, mappings, Config())
    assert not chunk.body.strip()
    assert chunk.embedding_input is None
    [citation] = chunk.citations
    assert citation.assets == [asset.path]
    assert citation.locations[0].path == "provider.pdf"


def test_native_rows_keep_uniform_nulls_identity_and_exact_citations(tmp_path):
    source = tmp_path / "note.md"
    source.write_text("# Title\n\nParagraph.\n\n<div>Uninterpreted</div>\n")
    process_file(source, tmp_path / "first")
    report, rows, _ = read_output(tmp_path / "first")
    for row in rows:
        meta = row["metadata"]
        assert set(meta) == KEYS
        assert meta["document_id"] == source.as_uri()
        assert meta["document_version"] is None
        assert meta["page_indices"] is None
        assert meta["page_coordinate_space"] is None
        assert meta["page_status"] == "nonpaged"
        assert meta["slide_indices"] is None
        assert meta["source"] == report["source"]
        assert meta["section_path"] == ["Title"]
        assert meta["block_ids"] == list(dict.fromkeys(c["block_id"] for c in row["citations"] if c["role"] == "body"))
        for ref in meta["source_locations"]:
            citation = row["citations"][ref["citation_index"]]
            location = citation["locations"][ref["location_index"]]
            assert ref["source"]["path"] == location["path"]
            assert ref["coordinate_space"] == "original"
            assert "sha256" not in location
    source.write_text("# Title\n\nChanged.\n")
    process_file(source, tmp_path / "second")
    _, changed, _ = read_output(tmp_path / "second")
    assert changed[0]["metadata"]["document_id"] == rows[0]["metadata"]["document_id"]
    assert changed[0]["metadata"]["source"]["sha256"] != rows[0]["metadata"]["source"]["sha256"]


def test_page_metadata_uses_only_original_body_evidence(tmp_path):
    source = FileRef(path=str(tmp_path / "source.pdf"), sha256="a" * 64)
    provider = FileRef(path="provider.pdf", sha256="b" * 64)

    def region(ref, page):
        return PdfRegion(source=ref, page_index=page, bbox=(10, 10, 40, 40), page_size=(100, 100))

    parsed = ParsedContent(blocks=[
        Block(id="h", payload=Heading(level=1, text="Title"), locations=[region(provider, 0)],
              location_precision="enclosing"),
        Block(id="p", payload=Text(markdown="Known.", plain_text="Known."), locations=[region(source, 2)],
              location_precision="enclosing"),
        Block(id="u", payload=Text(markdown="Unlocated.", plain_text="Unlocated.")),
        Block(id="e", payload=Equation(source=r"x=2\tag{3}"), locations=[region(source, 3)],
              location_precision="enclosing"),
    ])
    _, mappings = render_markdown(parsed)
    rows = list(chunk_records(make_chunks(parsed, mappings, Config()), source=source, source_type="pdf", parsed=parsed))
    assert rows[0]["metadata"]["page_indices"] == [2]
    assert rows[0]["metadata"]["page_status"] == "partial"
    assert rows[0]["metadata"]["page_coordinate_space"] == "original"
    assert rows[1]["metadata"]["page_indices"] == [3]
    assert rows[1]["metadata"]["labels"]["equations"] == [r"\tag{3}"]
    assert rows[1]["metadata"]["page_status"] == "known"
    for row in rows:
        assert set(row["metadata"]) == KEYS
        assert row["metadata"]["section_path"] == ["Title"]
        [heading] = [citation for citation in row["citations"] if citation["role"] == "heading"]
        assert heading["locations"] == []
        assert heading["precision"] == "unresolved"
        assert heading["reason"] == "original_source_location_unverified"
        for ref in row["metadata"]["source_locations"]:
            assert ref["source"] == source.model_dump()


def test_unresolved_rows_do_not_invent_pages_or_equation_labels(tmp_path):
    source = FileRef(path=str(tmp_path / "source.pdf"), sha256="a" * 64)
    parsed = ParsedContent(blocks=[Block(id="e", payload=Equation(source="x=2 % \\tag{fake}\n"))])
    _, mappings = render_markdown(parsed)
    [row] = chunk_records(make_chunks(parsed, mappings, Config()), source=source, source_type="pdf", parsed=parsed)
    assert row["metadata"]["page_indices"] is None
    assert row["metadata"]["page_status"] == "unavailable"
    assert row["metadata"]["source_locations"] == []
    assert row["metadata"]["labels"]["equations"] == []
    assert Path(source.path).as_uri() == row["metadata"]["document_id"]


def test_spanning_table_title_supplies_label_without_removing_its_content(tmp_path):
    source = FileRef(path=str(tmp_path / "scan.png"), sha256="a" * 64)
    parsed = ParsedContent(blocks=[Block(id="t", payload=Table(html="", rows=[
        [Cell(text="Table I. Measurements", colspan=2)], [Cell(text="A"), Cell(text="1")],
    ]))])
    _, mappings = render_markdown(parsed)
    [row] = chunk_records(make_chunks(parsed, mappings, Config()), source=source, source_type="png", parsed=parsed)
    assert row["metadata"]["labels"]["tables"] == ["Table I"]
    assert "Table I. Measurements" in row["body"]


def test_slide_scopes_are_metadata_without_inventing_pdf_pages(tmp_path):
    source = FileRef(path=str(tmp_path / "deck.pptx"), sha256="a" * 64)
    parsed = ParsedContent(blocks=[Block(id="s", semantic_scope=2, payload=Text(markdown="Slide", plain_text="Slide"))])
    _, mappings = render_markdown(parsed)
    [row] = chunk_records(make_chunks(parsed, mappings, Config()), source=source, source_type="pptx", parsed=parsed)
    assert row["metadata"]["slide_indices"] == [2]
    assert row["metadata"]["page_indices"] is None


def test_export_reindexes_original_evidence_and_preserves_chunk_content(tmp_path):
    source = FileRef(path=str(tmp_path / "source.pdf"), sha256="a" * 64)
    stale_source = FileRef(path=source.path, sha256="b" * 64)
    provider = FileRef(path="provider.pdf", sha256="c" * 64)

    def region(ref, page):
        return PdfRegion(source=ref, page_index=page, bbox=(0, 0, 10, 10), page_size=(100, 100))

    block = Block(id="p", payload=Text(markdown="Sample.", plain_text="Sample."), location_precision="enclosing",
                  locations=[
                      region(stale_source, 1), region(source, 2), region(provider, 3),
                      TextSpan(source=source, codepoints=Range(start=0, end=7), first_line=1, last_line_inclusive=1),
                  ])
    parsed = ParsedContent(blocks=[block])
    _, mappings = render_markdown(parsed)
    [chunk] = make_chunks(parsed, mappings, Config())
    original = chunk.model_dump(mode="json", exclude_none=True)
    original_blocks = parsed.model_dump()
    [row] = chunk_records([chunk], source=source, source_type="pdf", parsed=parsed)

    metadata = row.pop("metadata")
    assert set(metadata) == KEYS
    assert metadata["page_status"] == "partial"
    assert metadata["page_indices"] == [2]
    assert metadata["page_coordinate_space"] == "original"
    assert metadata["source_locations"] == [
        {"citation_index": 0, "location_index": index, "coordinate_space": "original", "source": source.model_dump()}
        for index in (0, 1)
    ]
    assert chunk.model_dump(mode="json", exclude_none=True) == original
    assert parsed.model_dump() == original_blocks
    [original_citation] = original["citations"]
    kept = [original_citation["locations"][i] for i in (1, 3)]
    assert row == {
        **original,
        "citations": [{**original_citation, "locations": kept, "precision": "unresolved",
                       "reason": "original_source_location_unverified"}],
    }


@pytest.mark.parametrize("formula, expected", [
    (r"x=1 \verb|\tag{literal}| \tag{4}", [r"\tag{4}"]),
    (r"x=1 \\\label{real}", [r"\label{real}"]),
    (r"x=1 \\% \tag{fake}", []),
    (r"x=1 \text{\tag{fake}} \label{real}", [r"\label{real}"]),
    (r"x=1 \label*{spurious} \tag{comment%}", []),
    ("x=1 % \\tag{fake}\r\\label{real}", [r"\label{real}"]),
    (r"x=1 \tag{20\%}", [r"\tag{20\%}"]),
])
def test_equation_labels_follow_latex_commands_not_literal_text(tmp_path, formula, expected):
    source = FileRef(path=str(tmp_path / "source.pdf"), sha256="a" * 64)
    parsed = ParsedContent(blocks=[Block(id="e", payload=Equation(source=formula))])
    _, mappings = render_markdown(parsed)
    [row] = chunk_records(make_chunks(parsed, mappings, Config()), source=source, source_type="pdf", parsed=parsed)
    assert row["metadata"]["labels"]["equations"] == expected
