from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from doc_pipeline.contracts.core import FileRef
from doc_pipeline.contracts.document import (
    Algorithm,
    Block,
    Cell,
    Code,
    Equation,
    Heading,
    Image,
    InlineAssetUse,
    Opaque,
    ParsedContent,
    Table,
    Text,
)
from doc_pipeline.parsed_content.md_renderer import render_markdown


def test_renderer_records_codepoint_coordinates_without_visible_anchor_noise():
    parsed_content = ParsedContent(
        blocks=[
            Block(id="first", payload=Text(plain_text="第一🫠段", markdown="第一🫠段")),
            Block(id="formula", payload=Equation(source="x + y")),
            Block(id="code", payload=Code(text="```\n\n", language="py")),
            Block(
                id="opaque",
                payload=Opaque(source_text="<script>alert(1)</script>", syntax="html", reason="unknown"),
            ),
            Block(id="blank", payload=Text(plain_text="", markdown="")),
        ]
    )
    markdown, mappings = render_markdown(parsed_content)
    actual = [markdown[item.markdown_range.start : item.markdown_range.end] for item in mappings]
    assert actual == [
        "第一🫠段",
        "$$\nx + y\n$$",
        "````py\n```\n\n````",
        "```html\n<script>alert(1)</script>\n```",
        "",
    ]
    assert [item.relation for item in mappings] == ["exact", "enclosing", "enclosing", "enclosing", "exact"]
    assert [item.block_id for item in mappings] == [block.id for block in parsed_content.blocks]
    assert "<a id=" not in markdown
    assert not markdown.endswith("\n\n\n")


def test_inline_markdown_keeps_safe_images_and_disarms_blocked_references():
    parsed_content = ParsedContent(
        blocks=[
            Block(
                id="heading",
                payload=Heading(
                    level=2,
                    text="Architecture",
                    markdown="## [![Architecture](assets/arch.png)](details.md)",
                ),
            ),
            Block(
                id="paragraph",
                payload=Text(
                    plain_text="Example missing remote dangerous",
                    markdown=(
                        "Example `![literal](file:///secret)` ![missing](blocked.png) "
                        "![remote](https://example.test/x.png) [dangerous](javascript:alert(1))"
                    ),
                ),
            ),
            Block(
                id="raw",
                payload=Text(
                    plain_text="ignored",
                    markdown="<form>![untrusted](https://example.test/other.png)</form>",
                ),
            ),
        ],
        inline_assets=[
            InlineAssetUse(
                owner_block_id="paragraph",
                original_reference="blocked.png",
                alt="missing",
                resolution="blocked_reference",
            ),
        ],
    )
    markdown, _ = render_markdown(parsed_content)
    html = BeautifulSoup(MarkdownIt("commonmark", {"html": True}).render(markdown), "html.parser")
    assert [(tag["alt"], tag["src"]) for tag in html.find_all("img")] == [("Architecture", "assets/arch.png")]
    assert [tag["href"] for tag in html.find_all("a", href=True)] == ["details.md"]
    assert html.h2.a.img["alt"] == "Architecture"
    assert html.code.text == "![literal](file:///secret)"
    assert "blocked.png" in html.get_text()
    assert "https://example.test/x.png" in html.get_text()


def test_sanitized_table_keeps_in_cell_packaged_images_and_inert_labels():
    table = Table(
        html=(
            '<table><tr><td onclick="alert(1)" rowspan="2"><a href="details.md">'
            '<img alt="A" src="assets/a.png" onerror="alert(1)"></a></td>'
            '<td><img src=".%2e/secret" alt="Bad"><script>bad</script></td></tr>'
            "<tr><td>Next</td></tr></table>"
        ),
        rows=[[Cell(text="A", rowspan=2), Cell(text="Bad")], [Cell(text="Next")]],
    )
    parsed_content = ParsedContent(
        blocks=[
            Block(id="table", payload=table),
            Block(
                id="image",
                payload=Image(asset=FileRef(path="assets/photo.png", sha256="a" * 64), alt="Photo"),
            ),
        ]
    )
    markdown, _ = render_markdown(parsed_content)
    html = BeautifulSoup(MarkdownIt("commonmark", {"html": True}).render(markdown), "html.parser")
    assert html.table.td.a.img["src"] == "assets/a.png"
    assert html.table.td["rowspan"] == "2"
    assert html.find_all("img")[-1]["src"] == "assets/photo.png"
    assert len(html.find_all("img")) == 2
    assert not html.find("script")
    assert "onerror" not in markdown and "onclick" not in markdown
    assert "Bad" in html.table.get_text()


def test_renderer_keeps_embedded_captions_and_math_markup_once():
    table = Table(
        html='<table><caption>Inventory</caption><tr><td><span class="math inline">x^2</span></td></tr></table>',
        rows=[[Cell(text="$x^2$")]],
        captions=["Inventory"],
        footnotes=["2026"],
    )
    markdown, _ = render_markdown(ParsedContent(blocks=[Block(id="table", payload=table)]))
    assert markdown.count("Inventory") == 1
    assert '<span class="math inline">x^2</span>' in markdown
    assert markdown.rstrip().endswith("2026")


def test_algorithm_reading_view_preserves_steps_math_and_indent_without_json_or_code_fence():
    text = "Algorithm 1: Train\n\n1 Initialize $x$.\n2   Update $x$.\n    return $x$."
    block = Block(id="algorithm", payload=Algorithm(text=text, markdown=text, footnotes=["Fixed seed"]))
    markdown, mappings = render_markdown(ParsedContent(blocks=[block]))
    assert "```" not in markdown
    assert "$x$" in markdown
    assert "&#160;" in markdown
    assert "Fixed seed" in markdown
    page = BeautifulSoup(MarkdownIt("commonmark", {"html": True}).render(markdown), "html.parser")
    assert len(page.find_all("br")) == 2
    assert "2 \u00a0\u00a0Update" in page.get_text()
    assert "Algorithm 1" in page.get_text()
    assert mappings[0].relation == "enclosing"


def test_safe_inline_superscripts_do_not_turn_whole_paragraphs_into_html():
    block = Block(
        id="p", payload=Text(plain_text="H2O and x2", markdown="H<sub>2</sub>O and $x$<sup>2</sup>")
    )
    markdown, _ = render_markdown(ParsedContent(blocks=[block]))
    assert "<div>" not in markdown
    assert markdown.startswith("H<sub>2</sub>O and $x$<sup>2</sup>")


def test_simple_table_requires_real_header_and_preserves_literal_cells():
    table = Table(
        html="",
        rows=[[Cell(text="Name"), Cell(text="Value")], [Cell(text="A|B"), Cell(text="<unsafe>")]],
        verified_header_rows=[0],
    )
    markdown, _ = render_markdown(ParsedContent(blocks=[Block(id="t", payload=table)]))
    assert markdown.startswith("| Name | Value |\n| --- | --- |")
    page = BeautifulSoup(MarkdownIt("commonmark").enable("table").render(markdown), "html.parser")
    assert [cell.text for cell in page.find_all("td")] == ["A|B", "<unsafe>"]
    headerless, _ = render_markdown(
        ParsedContent(blocks=[Block(id="t", payload=table.model_copy(update={"verified_header_rows": []}))])
    )
    assert "<table>" in headerless
    assert "| --- |" not in headerless
