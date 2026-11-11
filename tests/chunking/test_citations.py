import uuid

import pytest

from doc_pipeline import process_file
from doc_pipeline.chunking.builder import make_chunks
from doc_pipeline.chunking.citations import Citations, export_chunks
from doc_pipeline.contracts.chunk import Part, TableRows, TextRange
from doc_pipeline.contracts.config import Config
from doc_pipeline.contracts.core import FileRef, PdfRegion, Range, SlidePosition, SourceIdentity, TextSpan
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
METADATA = {"document_id", "document_version", "source_sha256", "chunk_sha256", "source_type", "content_status"}


def exported(parsed, source=SOURCE, source_type="pdf", chunks=None):
    if chunks is None:
        _, mappings = render_markdown(parsed)
        chunks = make_chunks(parsed, mappings, Config())
    identity = SourceIdentity(document_id="logical-doc", name="display.pdf", uri=source.path)
    return list(export_chunks(chunks, parsed, source=source, source_type=source_type, identity=identity, asset_digests={}))


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
        meta, compact = row["metadata"], row["citations"]
        assert set(meta) == METADATA
        assert meta["document_id"] == str(uuid.uuid5(uuid.NAMESPACE_URL, source.resolve().as_uri()))
        assert meta["document_version"] is None
        assert compact["pages"] is compact["slides"] is compact["sheets"] is None
        assert compact["location_status"] == "known"
        assert meta["source_sha256"] == report["source"]["sha256"]
        assert row["heading_path"] == ["Title"]
        assert compact["source_uri"] == source.resolve().as_uri()
        assert compact["source_name"] == source.name
        assert compact["source_lines"]
    assert {row["metadata"]["content_status"] for row in rows} == {"parsed", "unparsed"}
    source.write_text("# Title\n\nChanged.\n")
    process_file(source, tmp_path / "second")
    _, changed, _ = read_output(tmp_path / "second")
    assert changed[0]["metadata"]["document_id"] == rows[0]["metadata"]["document_id"]
    assert changed[0]["metadata"]["source_sha256"] != rows[0]["metadata"]["source_sha256"]


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
    prose, equation = exported(parsed, source)
    assert [page.number for page in prose.citations.pages] == [3]
    assert prose.citations.location_status == "partial"
    assert [page.number for page in equation.citations.pages] == [4]
    assert equation.citations.objects[0].labels == ["(3)"]
    assert equation.citations.location_status == "known"
    assert prose.heading_path == equation.heading_path == ["Title"]


def test_unresolved_rows_do_not_invent_pages_or_equation_labels(tmp_path):
    source = FileRef(path=str(tmp_path / "source.pdf"), sha256="a" * 64)
    parsed = ParsedContent(blocks=[Block(id="e", payload=Equation(source="x=2 % \\tag{fake}\n"))])
    [row] = exported(parsed, source)
    assert row.citations.pages is None
    assert row.citations.location_status == "unavailable"
    assert row.citations.objects[0].labels == []
    assert row.metadata.document_id == "logical-doc"


def test_spanning_table_title_supplies_label_without_removing_its_content(tmp_path):
    source = FileRef(path=str(tmp_path / "scan.png"), sha256="a" * 64)
    parsed = ParsedContent(blocks=[Block(id="t", payload=Table(html="", rows=[
        [Cell(text="Table I. Measurements", colspan=2)], [Cell(text="A"), Cell(text="1")],
    ]))])
    [row] = exported(parsed, source, source_type="png")
    assert row.citations.objects[0].labels == ["Table I"]
    assert "Table I. Measurements" in row.body


def test_slide_scopes_are_metadata_without_inventing_pdf_pages(tmp_path):
    source = FileRef(path=str(tmp_path / "deck.pptx"), sha256="a" * 64)
    text = Text(markdown="Slide", plain_text="Slide")
    parsed = ParsedContent(blocks=[Block(id="s", semantic_scope=2, payload=text)])
    [unverified] = exported(parsed, source, source_type="pptx")
    assert unverified.citations.slides is None
    proven = ParsedContent(blocks=[Block(id="s", semantic_scope=2, payload=text,
                                         locations=[SlidePosition(source=source, number=3)])])
    [row] = exported(proven, source, source_type="pptx")
    assert row.citations.slides == [3]
    assert row.citations.pages is None


def test_export_excludes_stale_and_converted_positions_and_preserves_chunk_content(tmp_path):
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
    [row] = exported(parsed, source, chunks=[chunk])
    assert row.citations.location_status == "partial"
    assert [page.number for page in row.citations.pages] == [3]
    assert row.body == chunk.body
    assert row.embedding_input == chunk.embedding_input
    assert chunk.model_dump(mode="json", exclude_none=True) == original
    assert parsed.model_dump() == original_blocks


@pytest.mark.parametrize("formula, labels, anchors", [
    (r"x=1 \verb|\tag{literal}| \tag{4}", ["(4)"], []),
    (r"x=1 \\\label{real}", [], ["real"]),
    (r"x=1 \\% \tag{fake}", [], []),
    (r"x=1 \text{\tag{fake}} \label{real}", [], ["real"]),
    (r"x=1 \label*{spurious} \tag{comment%}", [], []),
    ("x=1 % \\tag{fake}\r\\label{real}", [], ["real"]),
    (r"x=1 \tag{20\%}", [r"(20\%)"], []),
    (r"x=1 \tag*{S4}", ["S4"], []),
])
def test_equation_labels_follow_latex_commands_not_literal_text(formula, labels, anchors):
    parsed = ParsedContent(blocks=[Block(id="e", payload=Equation(source=formula))])
    [row] = exported(parsed)
    [obj] = row.citations.objects
    assert obj.labels == labels
    assert obj.anchors == anchors
