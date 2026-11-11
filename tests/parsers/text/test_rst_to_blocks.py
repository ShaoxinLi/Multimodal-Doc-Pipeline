

from doc_pipeline.contracts.document import Code, Furniture, Heading, Opaque
from doc_pipeline.parsed_content.selection import canonical_text
from tests.parsers.text.support import assert_provenance, parse


def test_rst_known_syntax_and_source_only_fallback(tmp_path):
    secret = tmp_path / "secret.rst"
    secret.write_text("PRIVATE MUST NOT BE READ")
    original = f""":Title: Runbook
:Author: Engineering

Heading
=======

.. note:: Retain this note.

- Outer item.

  3. Nested enumerated content.

- Final item.

::

    .. include:: code-example.rst

A body note [1]_.

.. [1] Footnote body.

.. _example: https://example.org

.. include:: {secret}

.. csv-table::
   :file: {secret}

.. role:: custom

Paragraph with an :custom:`unknown role`.

A known final paragraph.
"""
    parsed_content = parse(tmp_path, "guide.rst", original)
    assert parsed_content.parse_status == "partial"
    assert isinstance(parsed_content.blocks[1].payload, Heading)
    opaque = [block.payload.source_text for block in parsed_content.blocks if isinstance(block.payload, Opaque)]
    assert any(f".. include:: {secret}" in value for value in opaque)
    assert any(".. csv-table::" in value and ":file:" in value for value in opaque)
    assert any(":custom:`unknown role`" in value for value in opaque)
    semantic = "\n".join(
        canonical_text(block) for block in parsed_content.blocks if not isinstance(block.payload, Opaque)
    )
    for value in [
        "Retain this note.",
        "Nested enumerated content.",
        "Footnote body.",
        "A known final paragraph.",
    ]:
        assert value in semantic
    assert "PRIVATE MUST NOT BE READ" not in semantic
    assert any(
        isinstance(block.payload, Code) and ".. include:: code-example.rst" in block.payload.text
        for block in parsed_content.blocks
    )
    assert any(
        isinstance(block.payload, Furniture) and ".. _example:" in block.payload.text
        for block in parsed_content.blocks
    )
    assert_provenance(parsed_content, original)


def test_rst_escapes_and_tab_literal_blocks(tmp_path):
    original = (
        "Plain o\\ n\\ e, non-\\ ``NULL``, 2\\ :sup:`54`, and \\*not emphasis\\*.\n"
        "\n"
        "Table::\n"
        "\n"
        " TIME\tEVENT\t \n"
        " 0\t\tstart\n"
        "\n"
        "After.\n"
    )
    parsed_content = parse(tmp_path, "escape.rst", original)
    assert parsed_content.parse_status == "complete"
    first = parsed_content.blocks[0].payload
    assert first.plain_text == "Plain one, non-NULL, 254, and *not emphasis*."
    assert "\0" not in first.markdown
    assert first.markdown.startswith("Plain one, non-` NULL `, 254, and \\*not emphasis\\*.")
    [code] = [block for block in parsed_content.blocks if isinstance(block.payload, Code)]
    assert code.payload.text == "TIME   EVENT\n0" + " " * 14 + "start"
    span = code.locations[0].codepoints
    assert original[span.start : span.end] == " TIME\tEVENT\t \n 0\t\tstart\n"
    assert_provenance(parsed_content, original)
