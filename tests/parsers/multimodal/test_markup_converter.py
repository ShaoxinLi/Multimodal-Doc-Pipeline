from doc_pipeline.parsed_content.md_renderer import render_markdown
from doc_pipeline.parsed_content.selection import canonical_text
from tests.parsers.multimodal.support import _content_archive, _decode, _node, _png


def test_nested_table_keeps_readable_html_and_searchable_text_once(tmp_path):
    html = (
        "<table><caption>Table 2 Outer</caption><tr><td>Outer A</td><td>"
        "<table><caption>Inner caption</caption><tr><td>Inner X</td><td>Inner Y</td></tr>"
        '<tr><td colspan="2"><img src="images/crop.png" alt="Inner plot"></td></tr></table>'
        "</td></tr><tr><td>Outer B</td><td>Outer C</td></tr></table>"
    )
    archive = _content_archive(
        tmp_path / "nested.zip", [[_node("table", html=html)]], [("provider/images/crop.png", _png())]
    )
    parsed_content = _decode(archive, tmp_path / "bundle")
    assert parsed_content.parse_status == "complete"
    [block] = parsed_content.blocks
    assert block.payload.kind == "table"
    assert [len(row) for row in block.payload.rows] == [2, 2]
    assert block.payload.captions == ["Table 2 Outer"]
    markdown, _ = render_markdown(parsed_content)
    assert markdown.count("<table>") == 2
    assert markdown.count("Inner caption") == 1
    body = canonical_text(block)
    for text in ("Outer A", "Inner X", "Inner Y", "Inner plot", "Outer B", "Outer C"):
        assert body.count(text) == 1
    assert "Inner XInner Y" not in body
    [asset] = parsed_content.inline_assets
    assert asset.owner_block_id == block.id
    assert asset.table_cell is None
    assert asset.resolution == "archived"


def test_office_table_preserves_safe_links_and_omits_layout_whitespace(tmp_path):
    html = (
        "<table>\n <tr>\n <th><p>Name</p></th>\n </tr>\n <tr>\n <td>\n<p>"
        '<a href="https://tika.apache.org/" onclick="bad()">Tika</a> '
        '<a href="javascript:bad()">unsafe label</a></p>\n</td>\n </tr>\n</table>'
    )
    parsed_content = _decode(_content_archive(tmp_path / "links.zip", [[_node("table", html=html)]]), tmp_path / "bundle")
    assert "javascript:" not in parsed_content.blocks[0].payload.html
    assert "onclick" not in parsed_content.blocks[0].payload.html
    markdown, _ = render_markdown(parsed_content)
    assert 'href="https://tika.apache.org/"' in markdown
    assert "unsafe label" in markdown
    assert "javascript:" not in markdown
    assert "onclick" not in markdown
    assert "&#10;" not in markdown


def test_simple_office_paragraph_cells_render_as_markdown_table(tmp_path):
    html = "<table>\n<tr><th><p>Amount</p></th></tr>\n<tr><td><p>37.50</p></td></tr>\n</table>"
    parsed_content = _decode(_content_archive(tmp_path / "simple.zip", [[_node("table", html=html)]]), tmp_path / "bundle")
    markdown, _ = render_markdown(parsed_content)
    assert "| Amount |" in markdown
    assert "| --- |" in markdown
    assert "<table" not in markdown


def test_bookmark_only_anchors_do_not_force_plain_tables_to_html(tmp_path):
    html = '<table><tr><th><a name="bookmark"></a>Amount</th></tr><tr><td>1</td></tr></table>'
    parsed_content = _decode(_content_archive(tmp_path / "bookmarks.zip", [[_node("table", html=html)]]), tmp_path / "bundle")
    markdown, _ = render_markdown(parsed_content)
    assert "| Amount |" in markdown
    assert "<a" not in parsed_content.blocks[0].payload.html


def test_multiple_top_level_tables_remain_opaque(tmp_path):
    html = "<table><tr><td>A</td></tr></table><table><tr><td>B</td></tr></table>"
    parsed_content = _decode(_content_archive(tmp_path / "siblings.zip", [[_node("table", html=html)]]), tmp_path / "bundle")
    assert parsed_content.blocks[0].payload.kind == "opaque"
