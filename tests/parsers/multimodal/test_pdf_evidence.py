import copy
import hashlib
import json
from io import BytesIO
from zipfile import ZipFile

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.constants import PageLabelStyle
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from doc_pipeline.chunking.builder import make_chunks
from doc_pipeline.chunking.citations import export_chunks
from doc_pipeline.contracts.config import Config, Limits
from doc_pipeline.contracts.core import FileRef, SourceIdentity
from doc_pipeline.parsed_content.md_renderer import render_markdown
from doc_pipeline.parsed_content.selection import canonical_text
from doc_pipeline.parsers.multimodal.parser import decode_archive
from doc_pipeline.parsers.multimodal.pdf_evidence import page_correspondence
from tests.parsers.multimodal.support import exported_rows
from tests.support import document_archive
from tests.support import pdf_bytes as _pdf


def _block(spans):
    return {"type": "text", "bbox": [0, 0, 50, 10], "lines": [{"spans": [s]} for s in spans]}


def _fixture(cross=False):
    first = {"type": "text", "content": "First", "bbox": [0, 0, 50, 10]}
    second = {"type": "text", "content": "Second", "bbox": [0, 20, 40, 30]}
    body = _block([first, dict(second, cross_page=True)] if cross else [first])
    pages = [
        {
            "page_idx": 0,
            "page_size": [100, 100],
            "para_blocks": [body],
            "preproc_blocks": [_block([first])],
            "discarded_blocks": [],
        },
        {
            "page_idx": 1,
            "page_size": [100, 100],
            "para_blocks": [],
            "preproc_blocks": [_block([second])],
            "discarded_blocks": [],
        },
    ]
    node = {
        "type": "paragraph",
        "bbox": [0, 0, 500, 100],
        "content": {"paragraph_content": [{"type": "text", "content": "First Second" if cross else "First"}]},
    }
    return node, {"pdf_info": pages}


def _decode(tmp_path, node, layout, *, same_source=False, provider=None, extra_origins=(), groups=None):
    provider = _pdf() if provider is None else provider
    source = tmp_path / "input.pdf"
    source.write_bytes(provider if same_source else _pdf(1))
    source_ref = FileRef(path="source/input.pdf", sha256=hashlib.sha256(source.read_bytes()).hexdigest())
    archive = tmp_path / "result.zip"
    with ZipFile(archive, "w") as z:
        z.writestr("content_list_v2.json", json.dumps(groups or [[node]]))
        z.writestr("layout.json", json.dumps(layout))
        z.writestr("result_origin.pdf", provider)
        for name in extra_origins:
            z.writestr(name, provider)
    return decode_archive(
        source_ref,
        archive,
        tmp_path / "bundle",
        Limits(),
    )


def test_verified_converted_geometry_stays_in_memory(tmp_path):
    node, layout = _fixture()
    provider = _pdf()
    block = _decode(tmp_path, node, layout, provider=provider).blocks[0]
    region = block.locations[0]
    assert region.source.path == "native/provider.pdf"
    assert region.source.sha256 == hashlib.sha256(provider).hexdigest()
    assert region.page_index == 0
    assert region.bbox == (0, 0, 50, 10)
    assert block.location_precision == "enclosing"
    assert block.location_error == "original_pdf_page_unverified"
    assert not (tmp_path / "bundle/provider.pdf").exists()


def test_matching_pdf_bytes_reuse_original_source_location(tmp_path):
    node, layout = _fixture()
    block = _decode(tmp_path, node, layout, same_source=True).blocks[0]
    assert block.location_precision == "enclosing"
    assert block.locations[0].source.path == "source/input.pdf"
    assert not (tmp_path / "bundle/provider.pdf").exists()


def test_floored_layout_dimensions_keep_layout_normalization_and_actual_pdf_frame(tmp_path):
    provider = _pdf(2, width=100.5, height=100.9)
    node, layout = _fixture(cross=True)
    block = _decode(tmp_path, node, layout, provider=provider).blocks[0]
    assert [(r.page_index, r.bbox, r.page_size) for r in block.locations] == [
        (0, (0, 0, 50, 10), (100.5, 100.9)),
        (1, (0, 20, 40, 30), (100.5, 100.9)),
    ]
    assert block.location_precision == "enclosing"


@pytest.mark.parametrize("size", [[99, 100], [101, 101], [100.4, 100.8], [True, 100]])
def test_page_size_tolerance_does_not_accept_arbitrary_nearby_dimensions(tmp_path, size):
    provider = _pdf(1, width=100.5, height=100.9)
    node, layout = _fixture()
    layout["pdf_info"] = layout["pdf_info"][:1]
    layout["pdf_info"][0]["page_size"] = size
    block = _decode(tmp_path, node, layout, provider=provider).blocks[0]
    assert block.locations == []


def test_fractional_page_edge_is_valid_in_the_actual_pdf_frame(tmp_path):
    provider = _pdf(1, width=100.5, height=100.9)
    node, layout = _fixture()
    layout["pdf_info"] = layout["pdf_info"][:1]
    layout["pdf_info"][0]["para_blocks"][0]["lines"][0]["spans"][0]["bbox"][2] = 100.3
    block = _decode(tmp_path, node, layout, provider=provider).blocks[0]
    assert block.locations[0].bbox == (0, 0, 100.3, 10)
    assert block.locations[0].page_size == (100.5, 100.9)


def test_cross_page_regions_require_every_span_to_match_adjacent_preprocessed_page(tmp_path):
    node, layout = _fixture(cross=True)
    block = _decode(tmp_path, node, layout).blocks[0]
    assert [(r.page_index, r.bbox) for r in block.locations] == [
        (0, (0, 0, 50, 10)),
        (1, (0, 20, 40, 30)),
    ]


@pytest.mark.parametrize("mutation", ["missing", "ambiguous", "wrong_content", "wrong_page"])
def test_cross_page_failure_returns_no_partial_regions(tmp_path, mutation):
    node, layout = _fixture(cross=True)
    following = layout["pdf_info"][1]
    if mutation == "missing":
        following["preproc_blocks"] = []
    elif mutation == "ambiguous":
        following["preproc_blocks"] *= 2
    elif mutation == "wrong_content":
        following["preproc_blocks"][0]["lines"][0]["spans"][0]["content"] = "Different"
    else:
        following["page_idx"] = 0
    block = _decode(tmp_path, node, layout).blocks[0]
    assert block.locations == []
    assert block.location_precision == "unresolved"
    assert block.location_error in {
        "provider_cross_page_spans_unverified",
        "provider_layout_unverified",
    }


@pytest.mark.parametrize(
    "mutation", ["content", "geometry", "dimensions", "duplicate", "invalid_pdf", "multiple_pdfs"]
)
def test_unverified_or_ambiguous_evidence_never_becomes_a_page_claim(tmp_path, mutation):
    node, layout = _fixture()
    kwargs = {}
    if mutation == "content":
        node["content"]["paragraph_content"][0]["content"] = "Different"
    elif mutation == "geometry":
        node["bbox"] = [1, 0, 500, 100]
    elif mutation == "dimensions":
        layout["pdf_info"][0]["page_size"] = [99, 100]
    elif mutation == "duplicate":
        layout["pdf_info"][1]["para_blocks"] = copy.deepcopy(layout["pdf_info"][0]["para_blocks"])
    elif mutation == "invalid_pdf":
        kwargs["provider"] = b"not a pdf"
    else:
        kwargs["extra_origins"] = ["other_origin.pdf"]
    block = _decode(tmp_path, node, layout, same_source=True, **kwargs).blocks[0]
    assert block.locations == []
    assert block.location_precision == "unresolved"


def test_layout_page_index_is_used_without_assuming_content_group_position(tmp_path):
    node, layout = _fixture()
    layout["pdf_info"][0]["page_idx"] = 1
    layout["pdf_info"][1]["page_idx"] = 0
    block = _decode(tmp_path, node, layout).blocks[0]
    assert block.locations[0].page_index == 1


def test_rotated_provider_pdf_with_unresolved_coordinates_is_omitted(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100).rotate(90)
    stream = BytesIO()
    writer.write(stream)
    node, layout = _fixture()
    block = _decode(tmp_path, node, layout, provider=stream.getvalue()).blocks[0]
    assert not (tmp_path / "bundle/provider.pdf").exists()
    assert block.locations == []
    assert block.location_precision == "unresolved"
    assert block.location_error == "provider_layout_unverified"


@pytest.mark.parametrize("invalid", [None, [1], {"lines": [None], "bbox": [0, 0, 50, 10]}])
def test_malformed_optional_layout_keeps_decoded_content(tmp_path, invalid):
    node, layout = _fixture()
    layout["pdf_info"][0]["para_blocks"] = [invalid]
    parsed_content = _decode(tmp_path, node, layout)
    assert parsed_content.parse_status == "complete"
    assert parsed_content.blocks[0].payload.plain_text == "First"
    assert parsed_content.blocks[0].locations == []


def test_unsupported_payload_cannot_promote_raw_json_to_source_text_region(tmp_path):
    node, layout = _fixture()
    node["content"]["paragraph_content"][0]["type"] = "future_inline"
    block = _decode(tmp_path, node, layout, same_source=True).blocks[0]
    assert block.payload.kind == "opaque"
    assert block.locations == []
    assert block.location_error == "provider_block_payload_unsupported"


def _paragraph(name, top, first=58, *, margin=48, lines=3, right=300):
    boxes = [[first if i == 0 else margin, top + 12 * i, right - 120 * (i == lines - 1), top + 12 * i + 10]
             for i in range(lines)]
    words = [f"{name}{i}" for i in range(lines)]
    layout = {
        "type": "text",
        "bbox": [min(first, margin), top, right, boxes[-1][3]],
        "lines": [{"bbox": box, "spans": [{"type": "text", "content": w, "bbox": box}]} for box, w in zip(boxes, words)],
    }
    node = {"type": "paragraph", "bbox": layout["bbox"],
            "content": {"paragraph_content": [{"type": "text", "content": " ".join(words)}]}}
    return node, layout


def _page_blocks(tmp_path, paragraphs, preproc=(), pages=()):
    first = {"page_idx": 0, "page_size": [1000, 1000], "para_blocks": [layout for _, layout in paragraphs],
             "preproc_blocks": list(preproc), "discarded_blocks": []}
    provider = _pdf(1 + len(pages), width=1000, height=1000)
    groups = [[node for node, _ in paragraphs]]
    return _decode(tmp_path, None, {"pdf_info": [first, *pages]}, provider=provider, groups=groups).blocks


def test_paragraph_start_follows_the_local_indentation_convention_of_each_column(tmp_path):
    blocks = _page_blocks(tmp_path, [
        _paragraph("a", 100),
        _paragraph("b", 150),
        _paragraph("c", 200, 47),
        _paragraph("d", 250, 48, lines=1),
        _paragraph("e", 270, lines=2),
        _paragraph("f", 100, 520, margin=510, right=760),
        _paragraph("g", 150, 521, margin=510, right=760),
        _paragraph("h", 200, 511, margin=510, right=760),
    ])
    assert [block.payload.paragraph_start for block in blocks] == [True, True, False, False, True, None, None, False]
    assert all(block.location_precision == "enclosing" for block in blocks)
    assert not (tmp_path / "bundle/provider.pdf").exists()


@pytest.mark.parametrize(
    "paragraphs",
    [
        [_paragraph("a", 100, 48), _paragraph("b", 150, 48), _paragraph("c", 200, 48)],
        [_paragraph("a", 100), _paragraph("b", 150, 48)],
        [_paragraph("a", 100), _paragraph("b", 150), _paragraph("c", 200, lines=1)],
        [_paragraph("a", 100), _paragraph("b", 150), _paragraph("c", 200, 52)],
    ],
)
def test_paragraph_start_is_unknown_without_an_established_unambiguous_convention(tmp_path, paragraphs):
    assert _page_blocks(tmp_path, paragraphs)[-1].payload.paragraph_start is None


@pytest.mark.parametrize("mutation", [None, "missing_line_bbox", "line_order", "mixed_column", "cross_page"])
def test_unverifiable_line_geometry_leaves_hint_unknown_and_keeps_citation_geometry(tmp_path, mutation):
    paragraphs = [_paragraph("a", 100), _paragraph("b", 150), _paragraph("c", 200, 48, lines=4)]
    lines = paragraphs[-1][1]["lines"]
    preproc, pages = [], []
    if mutation == "missing_line_bbox":
        del lines[1]["bbox"]
    elif mutation == "line_order":
        lines[1]["bbox"], lines[2]["bbox"] = lines[2]["bbox"], lines[1]["bbox"]
    elif mutation == "mixed_column":
        for index, line in enumerate(lines[2:]):
            line["bbox"] = line["spans"][0]["bbox"] = [510, 60 + 12 * index, 760, 70 + 12 * index]
    elif mutation == "cross_page":
        span = lines[-1]["spans"][0]
        preproc = [_block([line["spans"][0] for line in lines[:-1]])]
        pages = [{"page_idx": 1, "page_size": [1000, 1000], "para_blocks": [], "discarded_blocks": [],
                  "preproc_blocks": [_block([dict(span)])]}]
        span["cross_page"] = True
    block = _page_blocks(tmp_path, paragraphs, preproc, pages)[-1]
    assert block.payload.paragraph_start is (False if mutation is None else None)
    assert len(block.locations) == (2 if mutation == "cross_page" else 1)


def test_verified_indentation_groups_equation_with_its_lead_in_and_continuation(tmp_path):
    lead, lead_layout = _paragraph("lead", 150)
    lead["content"]["paragraph_content"] = [
        {"type": "text", "content": "lead0 "},
        {"type": "equation_inline", "content": "x"},
        {"type": "text", "content": " lead1 lead2:"},
    ]
    lead_layout["lines"][0]["spans"] = [
        {"type": "text", "content": "lead0 ", "bbox": [58, 150, 100, 160]},
        {"type": "inline_equation", "content": "x", "bbox": [105, 150, 115, 160]},
    ]
    lead_layout["lines"][-1]["spans"][0]["content"] += ":"
    equation_bbox = [60, 190, 200, 200]
    equation = {
        "type": "equation_interline", "bbox": equation_bbox,
        "content": {"math_content": "x=2", "math_type": "latex"},
    }
    equation_layout = {
        "type": "interline_equation", "bbox": equation_bbox,
        "lines": [{"spans": [{"type": "interline_equation", "content": "x=2", "bbox": equation_bbox}]}],
    }
    pairs = [
        _paragraph("before", 100),
        (lead, lead_layout),
        (equation, equation_layout),
        _paragraph("where", 210, 48),
        _paragraph("next", 260),
    ]
    layout = {"pdf_info": [{
        "page_idx": 0, "page_size": [1000, 1000], "para_blocks": [block for _, block in pairs],
    }]}
    parsed = _decode(tmp_path, None, layout, provider=_pdf(1, width=1000, height=1000),
                     groups=[[node for node, _ in pairs]])
    before, lead_block, equation_block, continuation, following = parsed.blocks
    assert [block.payload.paragraph_start for block in (before, lead_block, continuation, following)] == [
        True, True, False, True,
    ]

    _, mappings = render_markdown(parsed)
    chunks = make_chunks(parsed, mappings, Config())
    assert [[c.block_id for c in chunk.citations if c.role == "body"] for chunk in chunks] == [
        [before.id], [lead_block.id, equation_block.id, continuation.id], [following.id],
    ]
    chunk = chunks[1]
    assert chunk.body == "lead0 $x$ lead1 lead2:\n\nx=2\n\nwhere0 where1 where2"
    assert len(chunk.body.encode()) <= Config().chunking.target_input_bytes
    assert chunk.embedding_input == chunk.body
    for citation, block in zip(chunk.citations, (lead_block, equation_block, continuation), strict=True):
        source = canonical_text(block)
        selected = source[citation.block_text_range.start:citation.block_text_range.end]
        assert selected == source
        assert chunk.body[citation.body_range.start:citation.body_range.end] == selected
        assert chunk.embedding_input[citation.input_range.start:citation.input_range.end] == selected
        assert citation.precision == "enclosing" and citation.locations[0].page_index == 0


def test_converted_geometry_preserves_chunk_boundaries_across_pages(tmp_path):
    lead, layout = _fixture()
    lead_text = "It follows that:"
    lead["content"]["paragraph_content"][0]["content"] = lead_text
    layout["pdf_info"][0]["para_blocks"][0]["lines"][0]["spans"][0]["content"] = lead_text
    formula = "x=2"
    equation_bbox = [0, 20, 40, 30]
    equation = {
        "type": "equation_interline", "bbox": [0, 200, 400, 300],
        "content": {"math_content": formula, "math_type": "latex"},
    }
    layout["pdf_info"][1]["para_blocks"] = [{
        "type": "interline_equation", "bbox": equation_bbox,
        "lines": [{"spans": [{"type": "interline_equation", "content": formula, "bbox": equation_bbox}]}],
    }]
    groups = [[lead], [equation]]
    original = _decode(tmp_path, None, layout, groups=groups, same_source=True)
    converted = _decode(tmp_path, None, layout, groups=groups)
    _, mappings = render_markdown(converted)
    original_chunks = make_chunks(original, mappings, Config())
    chunks = make_chunks(converted, mappings, Config())
    source_path = tmp_path / "input.pdf"
    source = FileRef(path=str(source_path), sha256=hashlib.sha256(source_path.read_bytes()).hexdigest())
    identity = SourceIdentity(document_id="test", name=source_path.name, uri=source.path)
    rows = list(export_chunks(chunks, converted, source=source, source_type="pdf", identity=identity, resources={}))

    assert [row.body for row in rows] == [lead_text, formula]
    assert [(row.id, row.body, row.embedding_input) for row in rows] == [
        (chunk.id, chunk.body, chunk.embedding_input) for chunk in original_chunks
    ]
    assert all(row.citations.pages is None for row in rows)
    assert all(row.citations.location_status == "unavailable" for row in rows)


def _cropped_pdf(*, crop=(10, 20, 110, 120), media=(0, 0, 140, 160), rotation=0, unit=1):
    from pypdf.generic import FloatObject, NameObject, RectangleObject

    writer = PdfWriter()
    for _ in range(2):
        page = writer.add_blank_page(width=140, height=160)
        page.mediabox = RectangleObject(media)
        page.cropbox = RectangleObject(crop)
        page.rotate(rotation)
        page[NameObject("/UserUnit")] = FloatObject(unit)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_crop_relative_geometry_translates_to_media_top_left_coordinates(tmp_path):
    node, layout = _fixture(cross=True)
    block = _decode(tmp_path, node, layout, provider=_cropped_pdf()).blocks[0]
    assert [(r.page_index, r.bbox, r.page_size) for r in block.locations] == [
        (0, (10, 40, 60, 50), (140, 160)),
        (1, (10, 60, 50, 70), (140, 160)),
    ]


def test_serialized_crop_dimension_roundoff_keeps_verified_frame(tmp_path):
    node, layout = _fixture()
    provider = _cropped_pdf(crop=(2.3999901, 1.92, 102.40002, 101.91998))
    block = _decode(tmp_path, node, layout, provider=provider).blocks[0]
    assert block.location_precision == "enclosing"
    assert block.locations[0].bbox == pytest.approx((2.3999901, 58.08002, 52.3999901, 68.08002))
    assert block.locations[0].page_size == (140, 160)


@pytest.mark.parametrize("kwargs", [
    {"crop": (10, 20, 111, 120)},
    {"crop": (10, 20, 109.999, 120)},
    {"crop": (110, 20, 10, 120)},
    {"crop": (-10, 20, 90, 120)},
    {"crop": (50, 20, 150, 120)},
    {"media": (1, 1, 141, 161)},
    {"rotation": 90},
    {"unit": 2},
    {"media": (0, 0, 100.7, 100.7), "crop": (0.2, 0.2, 100.6, 100.6)},
])
def test_unverified_crop_frames_never_create_locations(tmp_path, kwargs):
    node, layout = _fixture()
    block = _decode(tmp_path, node, layout, provider=_cropped_pdf(**kwargs)).blocks[0]
    assert block.locations == []
    assert block.location_precision == "unresolved"


def test_crop_bbox_is_bounded_before_translating_to_larger_media_frame(tmp_path):
    node, layout = _fixture()
    layout["pdf_info"][0]["para_blocks"][0]["lines"][0]["spans"][0]["bbox"] = [0, 0, 101, 10]
    block = _decode(tmp_path, node, layout, provider=_cropped_pdf()).blocks[0]
    assert block.locations == []


@pytest.mark.parametrize("html, verified", [
    ("<table><tr><td>Threshold < 5</td></tr></table>", True),
    ("<table><tr><td>Threshold &#60; 5</td></tr></table>", True),
    ("<table><tr><td>Threshold < 6</td></tr></table>", False),
    ("<table><tr><td colspan='2'>Threshold < 5</td></tr></table>", False),
])
def test_table_evidence_normalizes_text_entities_and_preserves_structure(tmp_path, html, verified):
    node, layout = _fixture()
    node["type"] = "table"
    node["content"] = {"html": html}
    layout["pdf_info"][0]["para_blocks"] = [{
        "type": "table", "bbox": [0, 0, 50, 10],
        "blocks": [{"type": "table_body", "lines": [{"spans": [{
            "type": "table", "bbox": [0, 0, 50, 10],
            "html": "<table><tr><td>Threshold &lt; 5</td></tr></table>",
        }]}]}],
    }]
    block = _decode(tmp_path, node, layout).blocks[0]
    assert block.payload.kind == "table"
    assert bool(block.locations) is verified


def test_table_evidence_distinguishes_literal_escaped_markup_from_real_markup(tmp_path):
    node, layout = _fixture()
    node["type"] = "table"
    node["content"] = {"html": "<table><tr><td><b>Value</b></td></tr></table>"}
    layout["pdf_info"][0]["para_blocks"] = [{
        "type": "table", "bbox": [0, 0, 50, 10],
        "blocks": [{"type": "table_body", "lines": [{"spans": [{
            "type": "table", "bbox": [0, 0, 50, 10],
            "html": "<table><tr><td>&lt;b&gt;Value&lt;/b&gt;</td></tr></table>",
        }]}]}],
    }]
    block = _decode(tmp_path, node, layout).blocks[0]
    assert block.payload.kind == "table"
    assert block.locations == []


def test_table_attribute_whitespace_is_not_erased_when_verifying_evidence():
    from doc_pipeline.parsers.multimodal.pdf_evidence import _table_matches

    assert not _table_matches('<table><tr><td colspan="12">Cell</td></tr></table>',
                              '<table><tr><td colspan="1&#32;2">Cell</td></tr></table>')


def test_malformed_pdf_crop_box_leaves_content_without_a_location(tmp_path):
    from pypdf.generic import ArrayObject, NameObject, NumberObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    page[NameObject("/CropBox")] = ArrayObject([NumberObject(0), NumberObject(100)])
    stream = BytesIO()
    writer.write(stream)
    node, layout = _fixture()
    parsed = _decode(tmp_path, node, layout, provider=stream.getvalue())
    assert parsed.blocks[0].payload.plain_text == "First"
    assert parsed.blocks[0].locations == []
    assert "mineru_invalid_provider_pdf" in parsed.diagnostics


def _colored_pdf(colors, *, title="original", labels=False, oversized=False):
    writer = PdfWriter()
    writer.add_metadata({"/Title": title})
    for color in colors:
        page = writer.add_blank_page(width=100_000 if oversized else 100, height=100)
        form = DecodedStreamObject()
        form.set_data(f"{color} rg 10 10 80 80 re f".encode())
        form.update({
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject(NumberObject(v) for v in (0, 0, 100, 100)),
            NameObject("/Resources"): DictionaryObject(),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/XObject"): DictionaryObject({NameObject("/Draw"): writer._add_object(form)})
        })
        content = DecodedStreamObject()
        content.set_data(b"q /Draw Do Q")
        page[NameObject("/Contents")] = writer._add_object(content)
    if labels:
        writer.set_page_label(0, 0, style=PageLabelStyle.LOWERCASE_ROMAN)
        writer.set_page_label(1, 1, style=PageLabelStyle.DECIMAL, start=2)
        writer.set_page_label(2, 2, style=PageLabelStyle.DECIMAL, prefix="A-")
    data = BytesIO()
    writer.write(data)
    return data.getvalue()


def test_pdf_renderer_catches_changed_resources_and_swapped_pages(tmp_path):
    red, blue = "1 0 0", "0 0 1"
    original = _colored_pdf([red, blue])
    rewritten = _colored_pdf([red, blue], title="rewrite")
    changed = _colored_pdf([blue, blue])
    swapped = _colored_pdf([blue, red])
    path = tmp_path / "input.pdf"
    path.write_bytes(original)
    assert original != rewritten
    for candidate in (rewritten, changed, swapped):
        assert [page.get_contents().get_data() for page in PdfReader(BytesIO(candidate)).pages] == [
            page.get_contents().get_data() for page in PdfReader(BytesIO(original)).pages
        ]
    mappings, profile = page_correspondence(path, rewritten, identical=False)
    assert [row.basis for row in mappings] == ["equivalent_page", "equivalent_page"]
    assert profile["renderer"].startswith("pypdfium2/")
    mappings, _ = page_correspondence(path, changed, identical=False)
    assert [row.reason for row in mappings] == ["page_render_mismatch", None]
    mappings, _ = page_correspondence(path, swapped, identical=False)
    assert [row.basis for row in mappings] == ["unmapped", "unmapped"]


def test_pdf_original_labels_and_render_limits(tmp_path):
    path = tmp_path / "input.pdf"
    data = _colored_pdf(["1 0 0"] * 3, labels=True)
    path.write_bytes(data)
    mapped, _ = page_correspondence(path, data, identical=True)
    assert [(p.number, p.label, p.basis) for p in mapped] == [
        (1, "i", "identical_bytes"), (2, None, "identical_bytes"), (3, "A-1", "identical_bytes"),
    ]
    path.write_bytes(_colored_pdf(["1 0 0"], oversized=True))
    mapped, _ = page_correspondence(path, _colored_pdf(["1 0 0"], title="rewrite", oversized=True), identical=False)
    assert [p.reason for p in mapped] == ["render_size_limit"]


def test_rewritten_pdf_original_page_and_converted_bbox_have_distinct_evidence(tmp_path):
    original = _colored_pdf(["1 0 0", "0 0 1"])
    provider = _colored_pdf(["1 0 0", "0 0 1"], title="rewrite")
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(original)
    source = FileRef(path="source/original.pdf", sha256=hashlib.sha256(original).hexdigest())
    archive_path = tmp_path / "native.zip"
    archive_path.write_bytes(document_archive(provider))
    parsed = decode_archive(source, archive_path, tmp_path / "artifact", Limits(), original_path=source_path)
    heading = parsed.blocks[0]
    region, page = heading.locations
    assert region.source.path == "native/provider.pdf"
    assert page.kind == "page" and page.number == 1 and page.source == source
    assert (tmp_path / "artifact/native/provider.pdf").read_bytes() == provider
    assert all(row.citations.pages is None for row in exported_rows(parsed, source, "pdf"))
