
import pytest
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from doc_pipeline.contracts.core import PipelineError
from doc_pipeline.contracts.document import Furniture, Heading, Text
from doc_pipeline.parsed_content.md_renderer import render_markdown
from doc_pipeline.parsed_content.selection import canonical_text
from tests.parsers.text.support import assert_provenance, parse


@pytest.mark.parametrize("filename", ["note.txt", "note.md", "note.rst", "note.tex"])
def test_repeated_unicode_crlf_ranges(tmp_path, filename):
    original = "\ufeff同一段 😀\r\n\r\n同一段 😀\r\n"
    parsed_content = parse(tmp_path, filename, original)
    assert parsed_content.parse_status == "complete"
    assert isinstance(parsed_content.blocks[0].payload, Furniture)
    paragraphs = [block for block in parsed_content.blocks if isinstance(block.payload, Text)]
    assert [canonical_text(block) for block in paragraphs] == ["同一段 😀", "同一段 😀"]
    assert [block.locations[0].codepoints.start for block in paragraphs] == [1, original.rindex("同一段")]
    assert [block.locations[0].first_line for block in paragraphs] == [1, 3]
    assert_provenance(parsed_content, original)


def test_txt_encoding_and_literal_markdown(tmp_path):
    original = "# 不是标题\r\n$$ x $$\r\n![图](x.png)"
    parsed_content = parse(tmp_path, "plain.txt", original, encoding="gb18030")
    [block] = parsed_content.blocks
    assert isinstance(block.payload, Text)
    assert not parsed_content.inline_assets
    assert block.payload.plain_text == original.replace("\r\n", "\n")
    assert block.location_precision == "enclosing"
    assert_provenance(parsed_content, original)
    with pytest.raises(PipelineError, match="native_decode_error"):
        parse(tmp_path, "plain.txt", b"\xff")


@pytest.mark.parametrize("filename", ["list.md", "list.rst"])
def test_nested_list_preserves_unsplittable_structure(tmp_path, filename):
    original = "- First item.\n\n  A continued paragraph.\n\n  - Nested item.\n\n- Second item.\n"
    parsed_content = parse(tmp_path, filename, original)
    [block] = parsed_content.blocks
    assert isinstance(block.payload, Text)
    assert block.payload.role == "list" and block.payload.atomic
    assert_provenance(parsed_content, original)


def test_txt_layout_blocks_are_literal_code(tmp_path):
    diagram = (
        "       0                   1\n"
        "      +-+-+-+-+-+-+-+-+-+-+-+-+\n"
        "      |  Source \\ Port  | *x* |\n"
        "      +-+-+-+-+-+-+-+-+-+-+-+-+"
    )
    columns = "      NS      A.ISI.EDU.\n      MX      10      VENERA"
    prose = "   Header Format\n\n     The source port number.  It is\n     16 bits."
    original = f"{prose}\n\n{diagram}\n\n{columns}\n\n     Sentence one.   Sentence two.\n     More text.\n"
    parsed_content = parse(tmp_path, "rfc.txt", original)
    payloads = [block.payload for block in parsed_content.blocks]
    assert [payload.kind for payload in payloads] == ["text", "text", "code", "code", "text"]
    assert [payloads[2].text, payloads[3].text] == [diagram, columns]
    assert payloads[1].plain_text == "     The source port number.  It is\n     16 bits."
    assert payloads[1].markdown == "The source port number\\.  It is\n16 bits\\."
    assert not any(isinstance(payload, Heading) for payload in payloads)
    markdown, _ = render_markdown(parsed_content)
    page = BeautifulSoup(MarkdownIt("commonmark").render(markdown), "html.parser")
    assert [node.get_text() for node in page.find_all("code")] == [diagram + "\n", columns + "\n"]
    assert [node.get_text() for node in page.find_all("p")] == [
        "Header Format",
        "The source port number.  It is\n16 bits.",
        "Sentence one.   Sentence two.\nMore text.",
    ]
    assert all(block.location_precision == "exact" for block in parsed_content.blocks)
    assert_provenance(parsed_content, original)


def test_txt_unicode_box_drawing_preserves_spacing(tmp_path):
    original = "    ┌───┐\n    │ x │\n    └───┘"
    parsed = parse(tmp_path, "diagram.txt", original)
    assert parsed.blocks[0].payload.kind == "code"
    markdown, _ = render_markdown(parsed)
    page = BeautifulSoup(MarkdownIt("commonmark").render(markdown), "html.parser")
    assert page.code.get_text() == original + "\n"
    assert_provenance(parsed, original)
