import pytest

from doc_pipeline.chunking.builder import make_chunks
from doc_pipeline.contracts.config import Config
from doc_pipeline.contracts.core import FileRef, PdfRegion
from doc_pipeline.contracts.document import (
    Algorithm,
    Block,
    Cell,
    Code,
    Equation,
    Furniture,
    Heading,
    Image,
    Opaque,
    ParsedContent,
    Table,
    Text,
)
from doc_pipeline.parsed_content.md_renderer import render_markdown
from doc_pipeline.parsed_content.selection import canonical_text


def body_citations(chunk):
    return [citation for citation in chunk.citations if citation.role == "body"]


def text(block_id, value, **kwargs):
    return Block(id=block_id, payload=Text(plain_text=value, markdown=value, **kwargs))


def heading(block_id, level, value):
    return Block(id=block_id, payload=Heading(level=level, text=value))


def chunks_for(*blocks, target=64, maximum=100):
    parsed_content = ParsedContent(blocks=list(blocks))
    _, mappings = render_markdown(parsed_content)
    config = Config(chunking={"target_input_bytes": target, "max_input_bytes": maximum})
    return make_chunks(parsed_content, mappings, config)


def assert_citations(blocks, chunks):
    block_index = {block.id: block for block in blocks}
    assert [chunk.id for chunk in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        for citation in chunk.citations:
            source = canonical_text(block_index[citation.block_id])
            selected = source[citation.block_text_range.start:citation.block_text_range.end]
            if citation.body_range:
                assert chunk.body[citation.body_range.start:citation.body_range.end] == selected
            if citation.input_range:
                assert chunk.embedding_input[citation.input_range.start:citation.input_range.end] == selected


def test_hard_wrapped_sentence_stays_together_when_it_fits_the_hard_budget():
    sentence = "A sentence that wraps across a line\ncontinues with its remaining words."
    blocks = [text("p", sentence)]
    chunks = chunks_for(*blocks)
    assert [chunk.body for chunk in chunks] == [sentence]
    assert_citations(blocks, chunks)


def test_fullwidth_period_ends_chinese_sentences_before_hard_wrapped_lines():
    sentence = "这是一句话换行后\n仍然属于同一句话．"
    chunks = chunks_for(text("p", sentence * 3), target=64, maximum=100)
    assert [chunk.body for chunk in chunks] == [sentence] * 3


def test_oversized_wrapped_prose_uses_line_boundaries_without_duplicate_text():
    value = "\n".join(f"wrapped prose line {index:02}" for index in range(1, 7))
    blocks = [text("p", value)]
    chunks = chunks_for(*blocks, target=64, maximum=100)
    assert "".join(chunk.body for chunk in chunks) == value
    assert all(chunk.body.endswith("\n") for chunk in chunks[:-1])
    assert_citations(blocks, chunks)


def test_packs_text_with_heading_and_stops_at_opaque_and_section_boundaries():
    blocks = [
        heading("root", 1, "Guide"),
        heading("usage", 2, "Usage"),
        text("p1", "first paragraph"),
        Block(id="page", payload=Furniture(text="Page 1", role="page_number")),
        text("p2", "- item one\n- item two", role="list"),
        Block(id="opaque", payload=Opaque(source_text="\\include{other}", syntax="tex", reason="include")),
        text("p3", "last paragraph"),
        heading("empty", 2, "Empty"),
    ]
    chunks = chunks_for(*blocks)
    assert [chunk.body for chunk in chunks] == [
        "first paragraph\n\n- item one\n- item two",
        "\\include{other}",
        "last paragraph",
        "Empty",
    ]
    assert chunks[1].embedding_input is None
    assert [chunk.heading_path for chunk in chunks] == [
        ["Guide", "Usage"],
        ["Guide", "Usage"],
        ["Guide", "Usage"],
        ["Guide", "Empty"],
    ]
    assert chunks[0].embedding_input == "Guide\n\nUsage\n\n" + chunks[0].body
    assert chunks[-1].embedding_input == "Guide\n\nEmpty"
    assert_citations(blocks, chunks)


def test_sentence_slices_preserve_original_contiguous_text_when_packed():
    value = "中" * 9 + "。  " + "🫠" * 7 + "！ " + "last " * 9 + "?"
    block = text("long", value)
    chunks = chunks_for(block, target=72)
    assert len(chunks) == 2
    assert chunks[0].body == "中" * 9 + "。  " + "🫠" * 7 + "！ "
    assert "".join(chunk.body for chunk in chunks) == value
    assert len(body_citations(chunks[0])) == 1
    assert_citations([block], chunks)


def test_long_list_splits_on_items_before_sentence_boundaries():
    items = ["- " + "first. " * 7 + "item\n", "- " + "second. " * 6 + "item\n", "- final item"]
    block = text("list", "".join(items), role="list")
    chunks = chunks_for(block)
    assert [chunk.body for chunk in chunks] == items
    assert_citations([block], chunks)


def test_hard_unicode_slices_overlap_only_within_the_oversize_sentence():
    value = "🫠" * 60
    blocks = [text("before", "Before."), text("long", value), text("after", "After.")]
    chunks = chunks_for(*blocks, maximum=64)
    assert chunks[0].body == "Before." and chunks[-1].body == "After."
    assert [body_citations(chunk)[0].block_id for chunk in chunks[1:-1]] == ["long"] * 4
    spans = [body_citations(chunk)[0].block_text_range for chunk in chunks[1:-1]]
    assert [(span.start, span.end) for span in spans] == [(0, 16), (15, 31), (30, 46), (45, 60)]
    assert all(len(chunk.embedding_input.encode("utf-8")) <= 64 for chunk in chunks)
    assert_citations(blocks, chunks)


def test_hard_slices_preserve_inline_math_even_when_math_alone_exceeds_maximum():
    math = "$" + "x" * 130 + "$"
    value = "A" * 40 + math + "z" * 85
    block = text("math", value)
    chunks = chunks_for(block)
    assert chunks[0].body == "A" * 40
    assert chunks[1].body == chunks[1].embedding_input == math
    assert body_citations(chunks[2])[0].block_text_range.start == 40 + len(math)
    assert_citations([block], chunks)


def test_atomic_composite_and_equation_remain_complete_when_over_budget():
    blocks = [
        text("composite", "nested-code\n" + "x" * 110, atomic=True),
        Block(id="equation", payload=Equation(source="x^2 + " * 20)),
        Block(id="textless", payload=Image(original_reference="figure.png")),
        Block(id="image", payload=Image(alt="diagram", captions=["Figure 1"], chart_text="A to B")),
    ]
    chunks = chunks_for(*blocks)
    assert [body_citations(chunk)[0].block_id for chunk in chunks] == ["composite", "equation", "textless", "image"]
    assert chunks[0].embedding_input == chunks[0].body
    assert chunks[1].embedding_input == chunks[1].body
    assert chunks[2].body == "" and chunks[2].embedding_input is None
    assert chunks[3].body == "Figure 1\ndiagram\nA to B"
    assert_citations(blocks, chunks)


def test_short_table_and_code_stay_whole_even_above_soft_target():
    blocks = [
        Block(id="table", payload=Table(html="", rows=[[Cell(text="T" * 80)]])),
        Block(id="code", payload=Code(text="P" * 70 + "\nC")),
    ]
    chunks = chunks_for(*blocks)
    assert [chunk.body for chunk in chunks] == ["T" * 80, "P" * 70 + "\nC"]
    assert_citations(blocks, chunks)


def test_long_table_uses_complete_row_groups_and_repeats_real_table_context():
    table = Block(
        id="stock",
        payload=Table(
            html="",
            rows=[
                [Cell(text="SKU"), Cell(text="Bin")],
                [Cell(text="A" * 18, rowspan=2), Cell(text="North" * 3)],
                [Cell(text="South" * 3)],
                [Cell(text="B" * 18), Cell(text="West" * 4)],
                [Cell(text="C" * 18), Cell(text="East" * 4)],
            ],
            verified_header_rows=[0],
            captions=["Stock"],
            footnotes=["All active"],
        ),
    )
    chunks = chunks_for(table, target=80)
    assert [
        (part.table_rows.start, part.table_rows.end)
        for chunk in chunks
        for part in body_citations(chunk)
    ] == [(1, 3), (3, 4), (4, 5)]
    assert chunks[0].body == "A" * 18 + "\t" + "North" * 3 + "\n\t" + "South" * 3
    assert all(chunk.embedding_input.startswith("Stock\n\nSKU\tBin\n\nAll active\n\n") for chunk in chunks)
    assert_citations([table], chunks)


def test_long_table_without_header_labels_all_rows_with_template_columns():
    table = Block(
        id="table",
        payload=Table(html="", rows=[[Cell(text=label * 30), Cell(text="z" * 15)] for label in "ABC"]),
    )
    chunks = chunks_for(table)
    assert [body_citations(chunk)[0].table_rows.start for chunk in chunks] == [0, 1, 2]
    assert all(chunk.embedding_input.startswith("column_1\tcolumn_2\n\n") for chunk in chunks)
    assert_citations([table], chunks)


def test_header_crossing_data_rowspan_keeps_oversize_table_whole():
    table = Block(
        id="table",
        payload=Table(
            html="",
            rows=[[Cell(text="Header", rowspan=2), Cell(text="Bin")], [Cell(text="B" * 120)]],
            verified_header_rows=[0],
        ),
    )
    [chunk] = chunks_for(table)
    assert chunk.embedding_input == chunk.body == "Header\tBin\n\t" + "B" * 120
    assert_citations([table], [chunk])


def test_long_code_preserves_lines_and_does_not_truncate_an_oversize_line():
    value = "x" * 140 + "\n" + "  line two\n\n" + "  line four\n"
    code = Block(id="code", payload=Code(text=value, language="py"))
    chunks = chunks_for(code)
    assert [chunk.body for chunk in chunks] == ["x" * 140 + "\n", "  line two\n\n  line four\n"]
    assert "".join(chunk.body for chunk in chunks) == value
    assert_citations([code], chunks)



def body_ids(chunks):
    return [[citation.block_id for citation in body_citations(chunk)] for chunk in chunks]


def equation(block_id="eq", source="E = mc^2"):
    return Block(id=block_id, payload=Equation(source=source))


def grouped(*blocks, target=400):
    chunks = chunks_for(*blocks, target=target, maximum=800)
    assert_citations(list(blocks), chunks)
    return body_ids(chunks)


def test_equation_takes_its_lead_in_and_verified_continuation_but_not_further_paragraphs():
    blocks = [
        text("before", "where y is the label."),
        text("lead", "Next, we formulate the problem:", paragraph_start=True),
        equation(),
        text("where", "where E is energy.", paragraph_start=False),
        text("next", "In another setting, the model differs.", paragraph_start=True),
    ]
    chunks = chunks_for(*blocks, target=400, maximum=800)
    assert body_ids(chunks) == [["before"], ["lead", "eq", "where"], ["next"]]
    assert chunks[1].body == "Next, we formulate the problem:\n\nE = mc^2\n\nwhere E is energy."
    assert_citations(blocks, chunks)


@pytest.mark.parametrize(
    ("lead", "joined"),
    [
        ("The effectiveness is evaluate by", True),
        ("as defined in (see [3])", True),
        ("which is called “the bound”", True),
        ("由下式给出：", True),
        ("This sentence is complete.", False),
        ("It ends inside a quote.”", False),
        ("Does it hold (really)?", False),
        ("该式成立。", False),
    ],
)
def test_equation_left_side_requires_colon_or_unfinished_sentence(lead, joined):
    ids = grouped(text("lead", lead), equation(), text("after", "After."))
    assert ids == ([["lead", "eq"], ["after"]] if joined else [["lead"], ["eq"], ["after"]])


@pytest.mark.parametrize(("hint", "joined"), [(False, True), (True, False), (None, False)])
def test_equation_right_side_requires_verified_continuation(hint, joined):
    ids = grouped(text("lead", "Done."), equation(), text("where", "where x is small.", paragraph_start=hint))
    assert ids == ([["lead"], ["eq", "where"]] if joined else [["lead"], ["eq"], ["where"]])


@pytest.mark.parametrize(
    "neighbor",
    [
        text("n", "- item one:", role="list"),
        text("n", "A footnote:", role="footnote"),
        text("n", "nested:\n    code", atomic=True),
        text("n", "> quoted:"),
    ],
)
def test_equation_never_takes_list_footnote_atomic_or_quoted_neighbors(neighbor):
    after = Block(id="m", payload=neighbor.payload.model_copy(update={"paragraph_start": False}))
    assert grouped(neighbor, equation(), after) == [["n"], ["eq"], ["m"]]


@pytest.mark.parametrize(
    "between",
    [
        heading("h", 2, "Next"),
        Block(id="f", payload=Furniture(text="5", role="page_number")),
        text("f", " "),
        Block(id="f", payload=Opaque(source_text="\\input{x}", syntax="tex", reason="include")),
        Block(id="f", payload=Image(original_reference="figure.png")),
    ],
)
def test_groups_never_skip_intervening_or_source_only_blocks(between):
    second = between.model_copy(update={"id": "g"})
    blocks = [text("lead", "It follows that:"), between, equation(), second, text("where", "where x.", paragraph_start=False)]
    ids = grouped(*blocks)
    assert ["eq"] in ids and ["where"] in ids
    assert not any("lead" in item and len(item) > 1 for item in ids)


def _region(page, bbox):
    return PdfRegion(source=FileRef(path="doc.pdf", sha256="0" * 64), page_index=page, bbox=bbox, page_size=(600, 800))


@pytest.mark.parametrize(
    ("region", "joined"),
    [
        (_region(0, (55, 120, 290, 160)), True),
        (_region(1, (55, 20, 290, 60)), False),
        (_region(0, (320, 120, 560, 160)), False),
        (_region(0, (55, 40, 290, 60)), False),
    ],
)
def test_verified_locations_reject_cross_page_and_cross_column_adjacency(region, joined):
    lead = text("lead", "It follows that:").model_copy(update={"locations": [_region(0, (45, 80, 300, 110))]})
    eq = equation().model_copy(update={"locations": [region]})
    assert grouped(lead, eq) == ([["lead", "eq"]] if joined else [["lead"], ["eq"]])


def test_whole_candidate_must_fit_target_with_headings_and_is_never_trimmed():
    lead = text("lead", "生成器的训练可写成如下优化问题：")
    where = text("where", "其中 x 为扰动变量。", paragraph_start=False)
    blocks = [heading("h", 1, "公式"), lead, equation(source="x" * 10), where]
    size = len("公式\n\n生成器的训练可写成如下优化问题：\n\nxxxxxxxxxx\n\n其中 x 为扰动变量。".encode())
    exact = chunks_for(*blocks, target=size, maximum=2 * size)
    assert body_ids(exact) == [["lead", "eq", "where"]]
    assert len(exact[0].embedding_input.encode()) == size
    assert_citations(blocks, exact)
    assert body_ids(chunks_for(*blocks, target=size - 1, maximum=2 * size)) == [["lead"], ["eq"], ["where"]]
    blocks[2] = equation(source="x" * 100)
    anchor = len("公式\n\n".encode()) + 100
    assert body_ids(chunks_for(*blocks, target=anchor, maximum=4 * size)) == [["lead"], ["eq"], ["where"]]


def test_shared_neighbor_claims_are_dropped_without_chaining():
    blocks = [
        text("a", "First we have:"),
        equation("e1"),
        text("mid", "where x is given by:", paragraph_start=False),
        equation("e2"),
        text("z", "where y is small.", paragraph_start=False),
    ]
    assert grouped(*blocks) == [["a", "e1"], ["mid"], ["e2", "z"]]


@pytest.mark.parametrize(
    ("lead", "after", "expected"),
    [
        ("Example:", None, [["lead", "code"], ["after"]]),
        ("Run it.", None, [["lead"], ["code"], ["after"]]),
        ("Done.", False, [["lead"], ["code", "after"]]),
        ("Done.", True, [["lead"], ["code"], ["after"]]),
    ],
)
def test_code_takes_a_colon_introduction_and_only_a_labeled_continuation(lead, after, expected):
    code = Block(id="code", payload=Code(text="pip install x\n", captions=["Listing 2: Setup"]))
    follow = text("after", "Listing 2 installs the package.", paragraph_start=after)
    assert grouped(text("lead", lead), code, follow) == expected
    unlabeled = text("after", "The command installs the package.", paragraph_start=False)
    assert grouped(text("lead", "Done."), code, unlabeled) == [["lead"], ["code"], ["after"]]


_TABLE = Table(html="", rows=[[Cell(text="A"), Cell(text="1")]], captions=["Table 1: Results"])


@pytest.mark.parametrize(
    ("payload", "lead", "joined"),
    [
        (_TABLE, "Table 1 reports accuracy.", True),
        (_TABLE, "Table 2 reports accuracy.", False),
        (_TABLE, "Table 1.2a reports accuracy.", False),
        (_TABLE, "Table 1-2a reports accuracy.", False),
        (_TABLE, "Table 1-A reports accuracy.", False),
        (_TABLE, "Table 1(a) reports accuracy.", False),
        (_TABLE, "The following table lists accuracy:", False),
        (_TABLE, "The following table (Table 2) lists accuracy:", False),
        (_TABLE, "The following table (Table 2a) lists accuracy:", False),
        (_TABLE, "The following table (Table 1.2a) lists accuracy:", False),
        (_TABLE, "The following table (Table IVa) lists accuracy:", False),
        (_TABLE, "The results are:", False),
        (_TABLE.model_copy(update={"captions": []}), "下表列出结果：", False),
        (Algorithm(text="step", markdown="step", captions=["Algorithm 1 Training"]),
         "以下算法给出训练流程：", False),
        (Algorithm(text="Algorithm 1 Training\nstep", markdown="step"), "Algorithm 1 trains the model.", True),
        (Algorithm(text="step", markdown="step", captions=["Algorithm 1 Training"]),
         "The following figure shows it:", False),
        (Image(captions=["Fig. 2: Architecture"]), "Figure 2 shows the architecture.", True),
        (Image(captions=["Fig. 2: Architecture"]), "Figure 3 shows the architecture.", False),
    ],
)
def test_captioned_objects_need_matching_label_and_never_rely_on_intro_keywords(payload, lead, joined):
    ids = grouped(text("lead", lead), Block(id="obj", payload=payload), text("after", "After."))
    assert ids == ([["lead", "obj"], ["after"]] if joined else [["lead"], ["obj"], ["after"]])


@pytest.mark.parametrize("caption", ["Table 1.2a: Results", "Table 1-2a: Results", "Table 1-A: Results", "Table 1(a): Results"])
def test_caption_identifier_is_not_truncated_for_either_neighbor(caption):
    obj = Block(id="obj", payload=_TABLE.model_copy(update={"captions": [caption]}))
    before = text("before", "Table 1 reports accuracy.")
    after = text("after", "Table 1 also reports variance.", paragraph_start=False)
    assert grouped(before, obj, after) == [["before"], ["obj"], ["after"]]


@pytest.mark.parametrize(
    ("after", "hint", "joined"),
    [
        ("Table 1 also shows variance.", False, True),
        ("Table 1 also shows variance.", None, False),
        ("Table 3 also shows variance.", False, False),
        ("It also shows variance.", False, False),
    ],
)
def test_captioned_object_right_side_needs_matching_label_and_continuation(after, hint, joined):
    ids = grouped(text("lead", "Done."), Block(id="obj", payload=_TABLE), text("after", after, paragraph_start=hint))
    assert ids == ([["lead"], ["obj", "after"]] if joined else [["lead"], ["obj"], ["after"]])


@pytest.mark.parametrize(
    ("composite", "lead", "joined"),
    [
        (text("c", "- a\n    - b", role="list", atomic=True), "Steps:", True),
        (text("c", "> quoted\n> ```\n> x\n> ```", atomic=True), "He wrote:", True),
        (text("c", "- a\n    - b", role="list", atomic=True), "Steps.", False),
        (text("c", "[^1]: note\n    more", role="footnote", atomic=True), "Notes:", False),
        (text("c", "plain\n    composite", atomic=True), "Details:", False),
    ],
)
def test_atomic_composites_take_only_an_explicit_colon_introduction(composite, lead, joined):
    after = text("after", "where it continues.", paragraph_start=False)
    ids = grouped(text("lead", lead), composite, after)
    assert ids == ([["lead", "c"], ["after"]] if joined else [["lead"], ["c"], ["after"]])


def scoped(block, scope):
    return block.model_copy(update={"semantic_scope": scope})


def test_slide_scope_separates_prose_and_heading_context():
    blocks = [
        scoped(heading("h0", 1, "First slide"), 0),
        scoped(text("p0", "First paragraph."), 0),
        scoped(text("p1", "Another paragraph."), 0),
        scoped(heading("h1", 2, "Second slide"), 1),
        scoped(text("p2", "Second paragraph."), 1),
        scoped(text("p3", "Third slide without title."), 2),
    ]
    chunks = chunks_for(*blocks, target=500, maximum=1000)
    assert body_ids(chunks) == [["p0", "p1"], ["p2"], ["p3"]]
    assert [chunk.heading_path for chunk in chunks] == [["First slide"], ["Second slide"], []]
    assert_citations(blocks, chunks)


def test_slide_scope_preserves_title_only_slide_and_blocks_object_attachment():
    blocks = [
        scoped(heading("h0", 1, "Title only"), 0),
        scoped(text("intro", "It follows that:"), 1),
        scoped(equation(), 2),
        scoped(text("after", "where x is small.", paragraph_start=False), 3),
    ]
    chunks = chunks_for(*blocks, target=500, maximum=1000)
    assert body_ids(chunks) == [["h0"], ["intro"], ["eq"], ["after"]]
    assert [chunk.heading_path for chunk in chunks] == [["Title only"], [], [], []]
    assert_citations(blocks, chunks)


def test_semantic_objects_still_attach_within_one_slide():
    blocks = [scoped(text("lead", "It follows that:"), 1), scoped(equation(), 1)]
    assert body_ids(chunks_for(*blocks, target=500, maximum=1000)) == [["lead", "eq"]]


def test_pure_equation_connectors_stay_with_the_equation_chain():
    blocks = [
        text("lead", "The equations are:"),
        equation("e1"),
        text("and", "and", paragraph_start=False),
        equation("e2"),
        text("or", "or"),
        equation("e3"),
        text("after", "where x is small.", paragraph_start=False),
    ]
    chunks = chunks_for(*blocks, target=500, maximum=1000)
    assert body_ids(chunks) == [[block.id for block in blocks]]
    assert_citations(blocks, chunks)


def test_equation_chain_is_only_emitted_whole_when_it_fits():
    blocks = [equation("e1", "x" * 40), text("and", "and"), equation("e2", "y" * 40)]
    size = len("\n\n".join(canonical_text(block) for block in blocks).encode())
    assert body_ids(chunks_for(*blocks, target=size, maximum=1000)) == [["e1", "and", "e2"]]
    assert body_ids(chunks_for(*blocks, target=size - 1, maximum=1000)) == [["e1"], ["and", "e2"]]


def test_oversized_equation_bridge_preserves_smaller_valid_semantic_groups():
    blocks = [text("lead", "The equations are:"), equation("e1", "x" * 35), text("and", "and"),
              equation("e2", "y" * 35)]
    chunks = chunks_for(*blocks, target=64, maximum=1000)
    assert body_ids(chunks) == [["lead", "e1"], ["and", "e2"]]
    assert_citations(blocks, chunks)


@pytest.mark.parametrize("boundary", ["scope", "page", "heading", "list", "sentence"])
def test_equation_connector_does_not_cross_semantic_boundaries(boundary):
    blocks = [equation("e1"), text("and", "and"), equation("e2")]
    if boundary == "scope":
        blocks = [scoped(block, index) for index, block in enumerate(blocks)]
    elif boundary == "page":
        blocks = [block.model_copy(update={"locations": [_region(index, (0, 0, 100, 20))]})
                  for index, block in enumerate(blocks)]
    elif boundary == "heading":
        blocks[1] = heading("and", 1, "and")
    elif boundary == "list":
        blocks[1] = text("and", "and", role="list")
    else:
        blocks[1] = text("and", "and this follows.")
    chunks = chunks_for(*blocks, target=500, maximum=1000)
    assert not any({"e1", "e2"} <= {c.block_id for c in body_citations(chunk)} for chunk in chunks)


def test_margin_connector_joins_aligned_equations_on_one_page():
    blocks = [equation("e1"), text("and", "and"), equation("e2")]
    boxes = [(230, 440, 360, 458), (68, 463, 93, 478), (184, 476, 408, 493)]
    blocks = [block.model_copy(update={"locations": [_region(0, box)]})
              for block, box in zip(blocks, boxes)]
    assert body_ids(chunks_for(*blocks, target=500, maximum=1000)) == [["e1", "and", "e2"]]
    blocks[-1] = blocks[-1].model_copy(update={"locations": [_region(0, (450, 476, 550, 493))]})
    assert body_ids(chunks_for(*blocks, target=500, maximum=1000)) == [["e1"], ["and"], ["e2"]]
