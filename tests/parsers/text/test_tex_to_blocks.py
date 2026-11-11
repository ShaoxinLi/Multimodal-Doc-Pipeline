
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from doc_pipeline.contracts.document import Code, Equation, Heading, Opaque, Text
from doc_pipeline.parsed_content.md_renderer import render_markdown
from tests.parsers.text.support import assert_provenance, parse


def test_tex_known_syntax_and_local_fallback(tmp_path):
    original = r"""\title{Operations}
\author{Engineering}
\begin{document}
\maketitle

Before \begin{em}styled \mbox{word}\end{em} after \LaTeX.

\begin{quotation}
First quoted paragraph.

Second quoted paragraph.
\end{quotation}

\begin{verse}
First line\\
Second line

New stanza
\end{verse}

\begin{enumerate}
\item First item
  \begin{itemize}
  \item Nested item
  \end{itemize}
  Continuation of first item.
\item Second item.
\end{enumerate}

\begin{em}
\[ x^2 + y^2 \]
\end{em}

\input{secret}

An \unknown{uninterpreted argument} paragraph.

A known final paragraph.
\end{document}
"""
    parsed_content = parse(tmp_path, "guide.latex", original)
    assert parsed_content.parse_status == "partial"
    heading = next(block for block in parsed_content.blocks if isinstance(block.payload, Heading))
    assert heading.payload.text == "Operations"
    assert heading.location_precision == "enclosing"
    assert [original[span.codepoints.start : span.codepoints.end] for span in heading.locations] == [
        r"\maketitle",
        r"\title{Operations}",
    ]
    text = [block.payload for block in parsed_content.blocks if isinstance(block.payload, Text)]
    assert any(payload.plain_text == "Before styled word after LaTeX." for payload in text)
    assert any(
        "First quoted paragraph.\n\nSecond quoted paragraph." in payload.plain_text
        and "\n>\n" in payload.markdown
        for payload in text
    )
    assert any(
        "First line\\\n> Second line" in payload.markdown and "\n>\n> New stanza" in payload.markdown
        for payload in text
    )
    assert any(payload.role == "list" and "Nested item" in payload.plain_text for payload in text)
    assert any(
        payload.role == "list" and "Continuation of first item." in payload.plain_text for payload in text
    )
    markdown, _ = render_markdown(parsed_content)
    page = BeautifulSoup(MarkdownIt("commonmark", {"html": True}).render(markdown), "html.parser")
    assert len(page.ol.find_all("li", recursive=False)) == 2
    first = page.ol.li
    assert first.ul.li.get_text().strip() == "Nested item"
    assert any(payload.role == "list" and payload.atomic for payload in text)
    assert "Continuation of first item." in first.get_text()
    assert "Continuation of first item." not in first.ul.get_text()
    assert any(
        isinstance(block.payload, Equation) and block.payload.source == "x^2 + y^2"
        for block in parsed_content.blocks
    )
    opaque = [block.payload.source_text for block in parsed_content.blocks if isinstance(block.payload, Opaque)]
    assert len(opaque) == 2
    assert "\\input{secret}" in opaque[0]
    assert r"\unknown{uninterpreted argument}" in opaque[1]
    assert any(payload.plain_text == "A known final paragraph." for payload in text)
    assert_provenance(parsed_content, original)


def test_tex_listings_are_literal(tmp_path):
    original = r"""\documentclass{article}
\begin{document}
\begin{abstract}
Abstract first paragraph.

Abstract second paragraph.
\end{abstract}

% \begin{lstlisting}
Use \lstinline{a{b}c}, \lstinline|\begin{x}{|, and \lstinline[language=C]!y % z! here.

\begin{lstlisting}[language=Python, caption={a, b}]
  \begin {test}{name}{label}
% not a comment
\end{document}
\end{lstlisting}

After the listing.
\end{document}
"""
    parsed_content = parse(tmp_path, "listing.tex", original)
    assert parsed_content.parse_status == "complete"
    [code] = [block for block in parsed_content.blocks if isinstance(block.payload, Code)]
    assert code.payload.text == "  \\begin {test}{name}{label}\n% not a comment\n\\end{document}"
    assert code.payload.language == "Python"
    span = code.locations[0].codepoints
    assert original[span.start : span.end].startswith(r"\begin{lstlisting}[language=Python")
    assert original[span.start : span.end].endswith(r"\end{lstlisting}")
    text = [block.payload for block in parsed_content.blocks if isinstance(block.payload, Text)]
    assert [payload.plain_text for payload in text] == [
        "Abstract first paragraph.",
        "Abstract second paragraph.",
        r"Use a{b}c, \begin{x}{, and y % z here.",
        "After the listing.",
    ]
    markdown, _ = render_markdown(parsed_content)
    page = BeautifulSoup(MarkdownIt("commonmark", {"html": True}).render(markdown), "html.parser")
    assert page.find_all("p")[2].get_text() == r"Use a{b}c, \begin{x}{, and y % z here."
    assert page.code.get_text() == code.payload.text + "\n"
    assert_provenance(parsed_content, original)


def test_tex_malformed_listing_keeps_whole_document_opaque(tmp_path):
    original = "\\begin{document}\nBroken \\lstinline{open\nnext line}.\n\\end{document}\n"
    parsed_content = parse(tmp_path, "broken.tex", original)
    [block] = parsed_content.blocks
    assert isinstance(block.payload, Opaque)
    assert any("entire source retained" in item for item in parsed_content.diagnostics)


def test_tex_malformed_suffix_after_document_is_opaque(tmp_path):
    body = "\\begin{document}\n\\begin{lstlisting}\n\\end{document}\\end\n\\end{lstlisting}\nKept text.\n\\end{document}"
    original = body + "\\end\n\nIgnored tail.\n"
    parsed_content = parse(tmp_path, "tail.tex", original)
    assert parsed_content.parse_status == "partial"
    assert any(isinstance(b.payload, Text) and b.payload.plain_text == "Kept text." for b in parsed_content.blocks)
    [code] = [block.payload for block in parsed_content.blocks if isinstance(block.payload, Code)]
    assert code.text == "\\end{document}\\end"
    [tail] = [block for block in parsed_content.blocks if isinstance(block.payload, Opaque)]
    assert tail.payload.source_text == "\\end\n\nIgnored tail.\n"
    assert tail.locations[0].codepoints.start == len(body)
    assert any("trailing source retained" in item for item in parsed_content.diagnostics)
    assert_provenance(parsed_content, original)

    unterminated = parse(tmp_path, "unterminated.tex", "\\begin{document}\nText \\end\n")
    assert [type(block.payload) for block in unterminated.blocks] == [Opaque]
