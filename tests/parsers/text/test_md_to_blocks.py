from hashlib import sha256

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from PIL import Image as RasterImage

from doc_pipeline.contracts.document import Furniture, Opaque, Table, Text
from doc_pipeline.parsed_content.md_renderer import render_markdown
from doc_pipeline.parsed_content.selection import canonical_text
from tests.parsers.text.support import assert_provenance, parse


def test_markdown_gfm_rewrites_only_image_tokens(tmp_path):
    raster = tmp_path / "frozen-image"
    RasterImage.new("RGB", (3, 2), "red").save(raster, format="PNG")
    digest = sha256(raster.read_bytes()).hexdigest()
    expected_path = f"assets/{digest}.png"
    original = """---
title: 文件标题
---

# Heading ![header](pic.png)

Before [![linked][p]](https://example.org/page) middle ![second](pic.png) after.

| Item | Figure |
| ---- | ------ |
| row  | ![cell](pic.png) |

![standalone](pic.png)

```md
![example](unlisted.png)
```

[p]: pic.png
"""
    parsed_content = parse(tmp_path, "images.markdown", original, assets={"pic.png": raster})
    assert parsed_content.parse_status == "complete"
    assert (tmp_path / expected_path).read_bytes() == raster.read_bytes()
    assert [asset.alt for asset in parsed_content.inline_assets] == ["header", "linked", "second", "cell"]
    assert all(asset.asset.path == expected_path for asset in parsed_content.inline_assets)
    linked_asset = parsed_content.inline_assets[1]
    owner = next(block for block in parsed_content.blocks if block.id == linked_asset.owner_block_id)
    [_, definition_span] = owner.locations
    definition = definition_span.codepoints
    assert original[definition.start : definition.end] == "[p]: pic.png\n"
    assert parsed_content.inline_assets[-1].table_cell == (1, 1)
    table = next(block.payload for block in parsed_content.blocks if isinstance(block.payload, Table))
    assert table.verified_header_rows == [0]
    assert [[cell.text for cell in row] for row in table.rows] == [["Item", "Figure"], ["row", "cell"]]
    assert_provenance(parsed_content, original)
    markdown, _ = render_markdown(parsed_content)
    page = BeautifulSoup(MarkdownIt("commonmark", {"html": True}).render(markdown), "html.parser")
    assert page.h1.img["src"] == expected_path
    linked = page.find("a", href="https://example.org/page")
    assert linked.img["alt"] == "linked"
    assert linked.parent.get_text() == "Before  middle  after."
    assert page.table.find("img")["alt"] == "cell"
    assert page.code.get_text() == "![example](unlisted.png)\n"


def test_markdown_html_table_and_uninterpreted_source(tmp_path):
    original = """<table><thead><tr><th>A</th></tr></thead><tbody><tr><td>Cell</td></tr></tbody></table>

<b>unsupported HTML</b>

| A | B |
| - | - |
| x | y | surplus |

After the opaque regions.
"""
    parsed_content = parse(tmp_path, "table.md", original)
    assert parsed_content.parse_status == "partial"
    assert isinstance(parsed_content.blocks[0].payload, Table)
    assert parsed_content.blocks[0].payload.verified_header_rows == [0]
    assert [block.payload.source_text for block in parsed_content.blocks if isinstance(block.payload, Opaque)] == [
        "<b>unsupported HTML</b>\n",
        "| A | B |\n| - | - |\n| x | y | surplus |\n",
    ]
    assert canonical_text(parsed_content.blocks[-1]) == "After the opaque regions."
    assert_provenance(parsed_content, original)


def test_markdown_comments_and_empty_anchors_are_inert(tmp_path):
    original = """<!-- overview -->

Intro paragraph.

<!--
hidden translation
-->

1. First step.

   <a id="beyond-grace" />

1. Second step with an <a name='inline'></a>anchor and a [link](#beyond-grace).

<a id="top"></a>

<!-- note --> visible trailing text

<!-->shown<!-- -->

<!-- closed --!> shown too -->

<a id="x" href="https://example.org/"></a>

<a id="y">label</a>

<a id="z"></a>
Visible paragraph after an anchor.

<a id="w" />
Following text in the same HTML block.

- Item <span id="s"></span>
"""
    parsed_content = parse(tmp_path, "inert.md", original)
    blocks = parsed_content.blocks
    furniture = [block for block in blocks if isinstance(block.payload, Furniture)]
    assert [block.payload.text for block in furniture] == [
        "<!-- overview -->\n",
        "<!--\nhidden translation\n-->\n",
        '<a id="top"></a>\n',
    ]
    assert all(block.payload.role == "syntax" for block in furniture)
    [steps] = [block for block in blocks if isinstance(block.payload, Text) and block.payload.role == "list"]
    assert steps.payload.plain_text == (
        "First step.\nSecond step with an anchor and a link."
    )
    span = steps.locations[0].codepoints
    assert original[span.start : span.end].startswith("1. First step.")
    assert original[span.start : span.end].rstrip().endswith("[link](#beyond-grace).")
    opaque = [block.payload.source_text for block in blocks if isinstance(block.payload, Opaque)]
    assert opaque == [
        "<!-- note --> visible trailing text\n",
        "<!-->shown<!-- -->\n",
        "<!-- closed --!> shown too -->\n",
        '<a id="x" href="https://example.org/"></a>\n',
        '<a id="y">label</a>\n',
        '<a id="w" />\nFollowing text in the same HTML block.\n',
        '- Item <span id="s"></span>\n',
    ]
    assert any(
        isinstance(block.payload, Text) and block.payload.plain_text.strip() == "Visible paragraph after an anchor."
        for block in blocks
    )
    assert_provenance(parsed_content, original)
    markdown, _ = render_markdown(parsed_content)
    assert "overview" not in markdown and "hidden translation" not in markdown
    page = BeautifulSoup(MarkdownIt("commonmark", {"html": True}).render(markdown), "html.parser")
    items = page.ol.find_all("li", recursive=False)
    assert [item.get_text(" ", strip=True) for item in items] == [
        "First step.",
        "Second step with an anchor and a link .",
    ]
    assert page.ol.find("a", href="#beyond-grace").get_text() == "link"
