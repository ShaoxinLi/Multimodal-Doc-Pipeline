from bs4 import BeautifulSoup
from markdown_it import MarkdownIt
from PIL import Image as RasterImage

from doc_pipeline.parsed_content.md_renderer import render_markdown
from tests.parsers.text.support import parse


def test_local_assets_and_explicit_snapshots_keep_blocked_links_inert(tmp_path):
    undeclared = tmp_path / "unlisted.png"
    RasterImage.new("RGB", (3, 2), "red").save(undeclared)
    allowed = tmp_path / "alternate.png"
    RasterImage.new("RGB", (2, 3), "blue").save(allowed)
    url = "https://example.org/archive.png"
    original = f"""![missing](unlisted.png) ![blocked](../escape.png) ![absolute](/tmp/read.png)

![external](https://example.org/no.png) ![archived]({url}) ![unsafe](data:image/gif;base64,AAAA)
"""
    parsed_content = parse(tmp_path, "images.md", original, assets={url: allowed, "../escape.png": undeclared})
    assert parsed_content.parse_status == "complete"
    assert [asset.resolution for asset in parsed_content.inline_assets] == [
        "archived",
        "blocked_reference",
        "blocked_reference",
        "external_unarchived",
        "archived",
        "blocked_reference",
    ]
    assert len(list((tmp_path / "assets").iterdir())) == 2
    markdown, _ = render_markdown(parsed_content)
    page = BeautifulSoup(MarkdownIt("commonmark", {"html": True}).render(markdown), "html.parser")
    assert [image["alt"] for image in page.find_all("img")] == ["missing", "archived"]
    assert "../escape.png" in page.get_text()
    assert "https://example.org/no.png" in page.get_text()
