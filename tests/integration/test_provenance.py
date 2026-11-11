import pytest
from markdown_it import MarkdownIt

from doc_pipeline import process_file
from tests.support import read_output


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_exported_markdown_citations_cover_their_blocks_across_newline_styles(tmp_path, newline):
    path = tmp_path / "input.md"
    path.write_bytes(
        newline.join(["# Heading", "", "<div>", "Hidden literal", "</div>", "", "Afterward", ""]).encode()
    )
    output = tmp_path / "out"
    process_file(path, output)
    _, chunks, _ = read_output(output)
    markdown = (output / "content.md").read_bytes().decode()
    tokens = [
        token
        for token in MarkdownIt().parse(markdown)
        if token.type in {"heading_open", "fence", "paragraph_open"}
    ]
    expected = {(token.map[0] + 1, token.map[1]) for token in tokens}
    citations = [citation["markdown"] for chunk in chunks for citation in chunk["citations"]]
    assert {(citation["first_line"], citation["last_line"]) for citation in citations} == expected
