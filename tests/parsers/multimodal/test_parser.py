import pytest

from doc_pipeline.contracts.core import PipelineError
from doc_pipeline.contracts.document import Block, Heading
from doc_pipeline.parsed_content.selection import canonical_text
from doc_pipeline.parsers.multimodal.parser import restore_numbered_subsections
from tests.parsers.multimodal.support import _content_archive, _decode, _zip
from tests.support import document_archive, pdf_bytes


def test_repeated_paragraphs_keep_content_without_claiming_ambiguous_locations(tmp_path):
    archive = tmp_path / "result.zip"
    archive.write_bytes(document_archive(pdf_bytes()))
    bundle = tmp_path / "result"
    parsed_content = _decode(archive, bundle)

    assert parsed_content.parse_status == "complete"
    assert not (bundle / "provider.pdf").exists()
    first_title, first_paragraph, second_title, second_paragraph = parsed_content.blocks
    assert first_title.payload.text == "Pipeline probe section 1"
    assert second_title.payload.text == "Pipeline probe section 2"
    for page, block in enumerate((first_title, second_title)):
        [region] = block.locations
        assert block.location_precision == "enclosing"
        assert region.page_index == page
        assert region.source.path == "provider.pdf"
    for block in (first_paragraph, second_paragraph):
        assert canonical_text(block) == "Repeated paragraph."
        assert block.locations == []
        assert block.location_precision == "unresolved"
        assert block.location_error == "provider_block_match_ambiguous"


@pytest.mark.parametrize(
    "groups",
    [
        {"pages": []},
        [{"type": "paragraph", "content": {}}],
        [[{"type": "", "content": {}}]],
    ],
)
def test_rejects_unsupported_content_groups_shape(tmp_path, groups):
    archive = _content_archive(tmp_path / "result.zip", groups, [("full.md", "fallback")])
    with pytest.raises(PipelineError) as error:
        _decode(archive, tmp_path / "bundle")
    assert error.value.code == "unsupported_provider_schema"


@pytest.mark.parametrize("names", [[], ["a/content_list_v2.json", "b_content_list_v2.json"]])
def test_requires_exactly_one_content_artifact(tmp_path, names):
    archive = _zip(tmp_path / "result.zip", [(name, "[]") for name in names] + [("full.md", "fallback")])
    with pytest.raises(PipelineError) as error:
        _decode(archive, tmp_path / "bundle")
    assert error.value.code == "unsupported_provider_schema"


def headings(*texts):
    return [
        Block(
            id=str(index),
            payload=Heading(text=text, level=2, markdown="## " + text),
        )
        for index, text in enumerate(texts)
    ]


def test_complete_roman_outline_recovers_lettered_children_with_explicit_inference():
    blocks = headings(
        "I. INTRODUCTION", "II. METHOD", "A. Model", "B. Training", "III. RESULTS", "A. Metrics"
    )
    result, changed = restore_numbered_subsections(blocks)
    assert changed
    assert [block.payload.level for block in result] == [2, 2, 3, 3, 2, 3]
    assert result[3].payload.markdown == "### B. Training"
    assert [block.payload.text for block in result] == [block.payload.text for block in blocks]


def test_ambiguous_or_incomplete_numbering_preserves_provider_levels():
    for blocks in [
        headings("II. METHOD", "A. Model", "III. RESULTS"),
        headings("I. INTRODUCTION", "III. METHOD", "A. Model"),
        headings("I. INTRODUCTION", "II. METHOD", "B. Training"),
        headings("I. INTRODUCTION", "II. METHOD", "A. Model", "C. Training"),
    ]:
        result, changed = restore_numbered_subsections(blocks)
        assert not changed
        assert result == blocks


def test_decimal_subsections_retain_their_explicit_parent_path():
    blocks = headings("3 Preliminaries", "3.1 Boolean Networks", "3.1.1 Definitions", "3.2 Models",
                      "4. Bi-correlation test", "4.1 Description")
    result, changed = restore_numbered_subsections(blocks)
    assert changed
    assert [block.payload.level for block in result] == [2, 3, 4, 3, 2, 3]
    assert result[-1].payload.markdown == "### 4.1 Description"
    for ambiguous in [headings("3 Other", "4.1 Missing parent"), headings("3 Parent", "References", "3.1 Appendix")]:
        assert restore_numbered_subsections(ambiguous) == (ambiguous, False)


@pytest.mark.parametrize("suffix", ["ppt", "pptx", "PPTX", "pdf", "docx", "png"])
def test_only_slide_sources_receive_semantic_group_scopes(tmp_path, suffix):
    from doc_pipeline.contracts.config import Limits
    from doc_pipeline.contracts.core import FileRef
    from doc_pipeline.parsers.multimodal.parser import decode_archive

    def paragraph(value):
        return {"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": value}]}}

    archive = _content_archive(tmp_path / "result.zip", [[paragraph("first")], [], [paragraph("second")]])
    parsed = decode_archive(FileRef(path=f"source.{suffix}", sha256="0" * 64), archive, tmp_path / "bundle", Limits())
    assert [block.semantic_scope for block in parsed.blocks] == ([0, 2] if suffix.lower() in {"ppt", "pptx"} else [None, None])
    assert all(block.locations == [] for block in parsed.blocks)


def test_validated_slide_type_survives_a_resolved_source_without_an_extension(tmp_path):
    from doc_pipeline.contracts.config import Limits
    from doc_pipeline.contracts.core import FileRef
    from doc_pipeline.parsers.multimodal.parser import decode_archive

    node = {"type": "paragraph", "content": {"paragraph_content": [{"type": "text", "content": "Slide"}]}}
    archive = _content_archive(tmp_path / "result.zip", [[node], [node]])
    parsed = decode_archive(FileRef(path="resolved/bytes", sha256="0" * 64), archive, tmp_path / "bundle",
                            Limits(), source_type="ppt")
    assert [block.semantic_scope for block in parsed.blocks] == [0, 1]
