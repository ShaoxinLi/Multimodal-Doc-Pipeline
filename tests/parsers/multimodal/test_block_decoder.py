import json

import pytest

from doc_pipeline.parsed_content.md_renderer import render_markdown
from doc_pipeline.parsed_content.selection import canonical_text
from tests.parsers.multimodal.support import (
    _content_archive,
    _decode,
    _node,
    _png,
    _sha,
    convert_inline_spans,
)


def test_office_style_arrays_preserve_emphasis_without_changing_search_text(tmp_path):
    archive = _content_archive(tmp_path / "office.zip", [[_node(
        "title", level=2,
        title_content=[{"type": "text", "content": "Overview", "style": ["bold", "italic"]}],
    )]])
    parsed = _decode(archive, tmp_path / "bundle")
    markdown, _ = render_markdown(parsed)
    assert "<em><strong>Overview</strong></em>" in markdown
    assert canonical_text(parsed.blocks[0]) == "Overview"


def test_office_numbered_list_prefix_does_not_duplicate_indentation(tmp_path):
    archive = _content_archive(tmp_path / "office.zip", [[_node(
        "list", attribute="ordered", list_items=[
            {"ilevel": 0, "prefix": "1.", "item_content": [convert_inline_spans("Parent")]},
            {"ilevel": 1, "prefix": "    1.", "item_content": [convert_inline_spans("Child")]},
        ],
    )]])
    parsed = _decode(archive, tmp_path / "bundle")
    markdown, _ = render_markdown(parsed)
    assert markdown.strip() == "1. Parent\n    1. Child"
    assert canonical_text(parsed.blocks[0]) == "1. Parent\n    1. Child"


def test_formula_spans_keep_word_boundaries_and_a_single_readable_caption(tmp_path):
    spans = [convert_inline_spans("Figure 3. Action of "), convert_inline_spans("Q^2", "equation_inline"),
             convert_inline_spans("on "), convert_inline_spans(r"\mathbb{P}^2", "equation_inline")]
    archive = _content_archive(tmp_path / "caption.zip", [[
        _node("image", image_caption=spans),
        _node("paragraph", paragraph_content=[convert_inline_spans("x", "equation_inline"),
                                              convert_inline_spans("y", "equation_inline"),
                                              convert_inline_spans(", respectively.")]),
    ]])
    parsed = _decode(archive, tmp_path / "bundle")
    markdown, _ = render_markdown(parsed)
    assert canonical_text(parsed.blocks[0]) == r"Figure 3. Action of $Q^2$ on $\mathbb{P}^2$"
    assert r"$Q^2$ on $\mathbb{P}^2$" in markdown
    assert canonical_text(parsed.blocks[1]) == "$x$ $y$, respectively."


def test_separates_caption_titles_and_panel_labels_in_flat_span_arrays(tmp_path):
    archive = _content_archive(tmp_path / "caption.zip", [[
        _node("image", image_caption=[
            convert_inline_spans("Figure 1: Input and output for the inference method."),
            convert_inline_spans("Ratio Data for Group of 12 Unclustered Cutaneous Melanomas"),
        ]),
        _node("chart", chart_caption=[
            convert_inline_spans("Figure 2: States on the x-axis; SSD probability on the y-axis"),
            convert_inline_spans("(a) Mean"),
        ]),
        _node("chart", chart_caption=[
            convert_inline_spans("(d) "),
            convert_inline_spans(r"\Delta f^C", "equation_inline"),
            convert_inline_spans("Figure 2: Demo of separability detection"),
        ]),
    ]])
    parsed = _decode(archive, tmp_path / "bundle")
    markdown, _ = render_markdown(parsed)
    assert [canonical_text(block) for block in parsed.blocks] == [
        "Figure 1: Input and output for the inference method.\nRatio Data for Group of 12 Unclustered Cutaneous Melanomas",
        "Figure 2: States on the x-axis; SSD probability on the y-axis\n(a) Mean",
        "(d) $\\Delta f^C$\nFigure 2: Demo of separability detection",
    ]
    assert "(d) $\\Delta f^C$\n\nFigure 2: Demo of separability detection" in markdown


def test_literal_caption_dollars_preserve_currency_and_markdown_punctuation(tmp_path):
    link = "$[read me](https://example.com)$"
    prices = "$5 to $10"
    archive = _content_archive(tmp_path / "caption.zip", [[
        _node("image", image_caption=link),
        _node("image", image_caption=[convert_inline_spans(prices)]),
        _node("image", image_caption=[
            convert_inline_spans(f"{link} or "),
            convert_inline_spans("x^2", "equation_inline"),
        ]),
    ]])
    parsed = _decode(archive, tmp_path / "bundle")
    markdown, _ = render_markdown(parsed)
    assert [canonical_text(block) for block in parsed.blocks] == [link, prices, f"{link} or $x^2$"]
    assert r"\$\[read me\](https://example\.com)\$" in markdown
    assert r"\$5 to \$10" in markdown
    assert r"\$\[read me\](https://example.com)\$ or $x^2$" in markdown


def test_preserves_unknown_block_and_empty_paragraph_without_locations(tmp_path):
    node = _node("future_block", values=[42, "retain this"])
    archive = _content_archive(tmp_path / "result.zip", [[node, _node("paragraph", paragraph_content=[])]])
    parsed_content = _decode(archive, tmp_path / "bundle")
    unknown, empty = parsed_content.blocks

    assert parsed_content.parse_status == "partial"
    assert unknown.payload.kind == "opaque"
    assert json.loads(unknown.payload.source_text) == node
    assert unknown.locations == []
    assert unknown.location_precision == "unresolved"
    assert empty.payload.kind == "text"
    assert canonical_text(empty) == ""
    assert empty.locations == []
    assert empty.location_precision == "unresolved"
    assert f"mineru_empty_block {empty.id}" in parsed_content.diagnostics


def test_decodes_merged_tables_chart_code_formula_and_referenced_images(tmp_path):
    png = _png()
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><rect width="2" height="2"/></svg>'
    archive = _content_archive(
        tmp_path / "result.zip",
        [
            [
                _node(
                    "paragraph",
                    paragraph_content=[
                        convert_inline_spans("Before "),
                        convert_inline_spans("x^2", "equation_inline"),
                        convert_inline_spans(" after."),
                    ],
                ),
                _node("code", code_content=[convert_inline_spans("def f():\r\n    return 2\r\n")], code_language="python"),
                _node(
                    "table",
                    html=(
                        '<table onclick="run()"><caption>Embedded caption</caption><thead><tr><th colspan="2">Name</th>'
                        '</tr></thead><tbody><tr><td rowspan="2">Alpha</td><td>One</td></tr>'
                        '<tr><td>Two<img src="images/crop.png" alt="Plot" onerror="run()">'
                        "<script>run()</script></td></tr></tbody></table>"
                    ),
                    table_caption=[convert_inline_spans("Table caption")],
                    table_footnote=[convert_inline_spans("A table footnote")],
                    image_source={"path": "images/crop.png"},
                ),
                _node(
                    "chart",
                    content="<table><tr><td>Year</td><td>Sales</td></tr><tr><td>2026</td><td>4</td></tr></table>",
                    chart_caption="Chart table",
                ),
                _node(
                    "chart",
                    chart_caption="Chart image",
                    chart_footnote="Chart footnote",
                    content="Chart legend",
                    image_source={"path": "images/crop.png"},
                ),
                _node("image", image_caption="Standalone image", image_source={"path": "images/vector.svg"}),
                _node(
                    "equation_interline",
                    math_content=r"\sqrt{x}",
                    math_type="latex",
                    image_source={"path": "images/crop.png"},
                ),
            ]
        ],
        [("provider/images/crop.png", png), ("provider/images/vector.svg", svg)],
    )
    bundle = tmp_path / "bundle"
    parsed_content = _decode(archive, bundle)
    paragraph, code, table, chart_table, chart_image, image, equation = parsed_content.blocks

    assert parsed_content.parse_status == "complete"
    assert paragraph.payload.markdown == "Before $x^2$ after."
    assert canonical_text(paragraph) == "Before $x^2$ after."
    assert code.payload.text == "def f():\n    return 2\n"
    assert code.payload.language == "python"
    assert table.payload.verified_header_rows == [0]
    assert table.payload.rows[0][0].colspan == 2
    assert table.payload.rows[1][0].rowspan == 2
    assert canonical_text(table) == (
        "Table caption\nEmbedded caption\n\nName\t\nAlpha\tOne\n\tTwoPlot\n\nA table footnote"
    )
    png_name = f"assets/{_sha(png)}.png"
    assert "onclick" not in table.payload.html
    assert "onerror" not in table.payload.html
    assert "<script" not in table.payload.html
    assert f'<img alt="Plot" src="{png_name}"/>' in table.payload.html
    assert chart_table.payload.kind == "table"
    assert canonical_text(chart_table) == "Chart table\n\nYear\tSales\n2026\t4"
    assert canonical_text(chart_image) == "Chart image\nChart legend\nChart footnote"
    assert chart_image.payload.asset.path == png_name
    assert (bundle / chart_image.payload.asset.path).read_bytes() == png
    assert (bundle / image.payload.asset.path).read_bytes() == svg
    assert canonical_text(equation) == r"\sqrt{x}"
    assert [(use.owner_block_id, use.table_cell) for use in parsed_content.inline_assets] == [
        (table.id, (2, 1)),
        (table.id, None),
        (equation.id, None),
    ]
    assert {use.asset.path for use in parsed_content.inline_assets} == {png_name}
    assert len(list((bundle / "assets").iterdir())) == 2


def test_missing_and_blocked_assets_are_diagnosed_without_failing_text_parse(tmp_path):
    archive = _content_archive(
        tmp_path / "result.zip",
        [
            [
                _node("image", image_source={"path": "../bad.png"}, image_caption="Retain the caption"),
                _node(
                    "table",
                    html='<table><tr><td><img src="images/missing.png" alt="Diagram"></td></tr></table>',
                ),
                _node("image", image_source={"path": "images/active.svg"}),
            ]
        ],
        [
            (
                "provider/images/active.svg",
                '<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><script>run()</script></svg>',
            )
        ],
    )
    bundle = tmp_path / "bundle"
    parsed_content = _decode(archive, bundle)
    image, table, active = parsed_content.blocks

    assert parsed_content.parse_status == "complete"
    assert image.payload.asset is None
    assert image.payload.captions == ["Retain the caption"]
    assert f"mineru_asset_blocked_reference {image.id}" in parsed_content.diagnostics
    assert f"mineru_asset_blocked_reference {active.id}" in parsed_content.diagnostics
    assert f"mineru_asset_missing_snapshot {table.id}" in parsed_content.diagnostics
    assert parsed_content.inline_assets[0].resolution == "missing_snapshot"
    assert parsed_content.inline_assets[0].table_cell == (0, 0)
    assert table.payload.html == "<table><tr><td>Diagram</td></tr></table>"
    assert not (bundle / "assets").exists()


@pytest.mark.parametrize(
    "node,payload_kind,expected",
    [
        (_node("title", title_content=[convert_inline_spans("Section")], level=2), "heading", "Section"),
        (_node("paragraph", paragraph_content=[convert_inline_spans("Prose")]), "text", "Prose"),
        *[
            (_node(kind, **{f"{kind}_content": [convert_inline_spans("Page text")]}), "furniture", "Page text")
            for kind in ("page_header", "page_footer", "page_number", "page_aside_text")
        ],
        (_node("page_footnote", page_footnote_content=[convert_inline_spans("Note")]), "text", "Note"),
        *[
            (_node(kind, list_type="text", list_items=[{"item_content": [convert_inline_spans("Item")]}]), "text", "Item")
            for kind in ("list", "index")
        ],
        (
            _node("code", code_content=[convert_inline_spans("print(1)")], code_caption="Code 1", code_footnote="Note"),
            "code",
            "Code 1\n\nprint(1)\n\nNote",
        ),
        (
            _node(
                "algorithm",
                algorithm_content=[convert_inline_spans("1 Start\n  2 Use "), convert_inline_spans("x", "equation_inline")],
                algorithm_caption="Algorithm 1",
                algorithm_footnote="Note",
            ),
            "algorithm",
            "Algorithm 1\n\n1 Start\n  2 Use $x$\n\nNote",
        ),
        (_node("equation_interline", math_content="x", math_type="latex"), "equation", "x"),
        (_node("image", image_caption="Figure 1", content="Description"), "image", "Figure 1\nDescription"),
        (
            _node("chart", chart_caption="Chart 1", content="| A | B |\n| 1 | 2 |"),
            "image",
            "Chart 1\n| A | B |\n| 1 | 2 |",
        ),
        (_node("table", html="<table><tr><td>Cell</td></tr></table>"), "table", "Cell"),
    ],
)
def test_supported_block_types(tmp_path, node, payload_kind, expected):
    parsed_content = _decode(_content_archive(tmp_path / "result.zip", [[node]]), tmp_path / "bundle")
    block = parsed_content.blocks[0]
    assert parsed_content.parse_status == "complete"
    assert block.payload.kind == payload_kind
    assert canonical_text(block) == expected
    if payload_kind == "heading":
        assert block.payload.markdown == "## Section"
        assert block.payload.level == 2
    if payload_kind == "algorithm":
        assert block.payload.markdown == "1 Start\n  2 Use $x$"


def test_inline_styles_links_phonetic_markdown_and_code_are_preserved(tmp_path):
    spans = [
        {"type": "text", "content": "Styled", "bold": True, "italic": True},
        convert_inline_spans("H<sub>2</sub>O"),
        {"type": "hyperlink", "url": "https://example.com/a", "children": [convert_inline_spans("Link")]},
        {"type": "hyperlink", "url": "javascript:run()", "content": "Blocked"},
        convert_inline_spans("phonetic", "phonetic"),
        convert_inline_spans("**Markdown**", "md"),
        convert_inline_spans("x`y", "code_inline"),
    ]
    parsed_content = _decode(
        _content_archive(tmp_path / "result.zip", [[_node("paragraph", paragraph_content=spans)]]), tmp_path / "bundle"
    )
    payload = parsed_content.blocks[0].payload
    assert payload.kind == "text"
    assert "<em><strong>Styled</strong></em>" in payload.markdown
    assert "H<sub>2</sub>O" in payload.markdown
    assert "[Link](<https://example.com/a>)" in payload.markdown
    assert "javascript:" not in payload.markdown
    assert "phonetic**Markdown**``x`y``" in payload.markdown
    assert "H2O" in payload.plain_text


def test_office_list_prefix_and_indentation_and_formula_image(tmp_path):
    nodes = [
        _node(
            "index",
            list_type="text",
            list_items=[
                {"item_content": [convert_inline_spans("First")], "prefix": "1.", "ilevel": 0},
                {"item_content": [convert_inline_spans("Nested")], "prefix": "a)", "ilevel": 1},
            ],
        ),
        _node(
            "equation_interline",
            math_content="",
            math_type="latex",
            image_source={"path": "images/formula.png"},
        ),
    ]
    parsed_content = _decode(
        _content_archive(tmp_path / "result.zip", [nodes], [("provider/images/formula.png", _png())]), tmp_path / "bundle"
    )
    assert parsed_content.blocks[0].payload.markdown == "1. First\n    - a) Nested"
    assert canonical_text(parsed_content.blocks[0]) == "1. First\n    a) Nested"
    assert parsed_content.blocks[1].payload.kind == "image"
    assert parsed_content.blocks[1].payload.asset is not None
    assert canonical_text(parsed_content.blocks[1]) == ""


@pytest.mark.parametrize(
    "node",
    [
        {"type": "paragraph", "content": None},
        {"type": "paragraph", "content": []},
        {"type": "paragraph"},
        _node("algorithm", algorithm_content=[{"type": "future", "content": "Keep"}]),
        _node("code", code_content=[convert_inline_spans("x")], code_caption={"invalid": True}),
        _node("index", list_items=[{"item_content": [convert_inline_spans("x")], "ilevel": "1"}]),
        _node("paragraph", paragraph_content=[{"type": "text", "content": {"invalid": True}}]),
        _node("title", title_content=[convert_inline_spans("Bad")], level=True),
    ],
)
def test_malformed_known_shapes_remain_opaque(tmp_path, node):
    parsed_content = _decode(_content_archive(tmp_path / "result.zip", [[node]]), tmp_path / "bundle")
    assert parsed_content.parse_status == "partial"
    assert json.loads(parsed_content.blocks[0].payload.source_text) == node


def test_code_preserves_literal_tags_and_multiline_heading_stays_one_heading(tmp_path):
    value = 'print("<b>hello</b>")\n# <sup>not markup</sup>'
    nodes = [
        _node("code", code_content=[convert_inline_spans(value)]),
        _node("title", title_content=[convert_inline_spans("Long\nheading")], level=2),
    ]
    parsed_content = _decode(_content_archive(tmp_path / "result.zip", [nodes]), tmp_path / "bundle")
    assert parsed_content.blocks[0].payload.text == value
    markdown, _ = render_markdown(parsed_content)
    assert value in markdown
    assert "## Long heading" in markdown


def test_chart_markdown_becomes_table_and_unsafe_seal_path_is_blocked(tmp_path):
    chart = _node("chart", content="| A | B |\n| --- | --- |\n| 1 | 2 |", chart_caption="Chart 2")
    seal = {
        **_node("image", image_caption="Seal", image_source={"path": "../blocked.png"}),
        "sub_type": "seal",
    }
    parsed_content = _decode(_content_archive(tmp_path / "result.zip", [[chart, seal]]), tmp_path / "bundle")
    assert parsed_content.blocks[0].payload.kind == "table"
    assert canonical_text(parsed_content.blocks[0]) == "Chart 2\n\nA\tB\n1\t2"
    assert parsed_content.blocks[1].payload.resolution == "blocked_reference"
