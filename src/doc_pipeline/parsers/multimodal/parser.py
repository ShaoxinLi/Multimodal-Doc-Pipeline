import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

from doc_pipeline.contracts.config import Limits
from doc_pipeline.contracts.core import FileRef, PipelineError
from doc_pipeline.contracts.document import Block, Heading, Opaque, ParsedContent
from doc_pipeline.parsers.multimodal.archive import open_validated_archive
from doc_pipeline.parsers.multimodal.block_decoder import BlockDecoder
from doc_pipeline.parsers.multimodal.markup_converter import string_value
from doc_pipeline.parsers.multimodal.pdf_evidence import PdfEvidenceMatcher, pdf_page_frames

_CONTENT_LIST_NAME = "content_list_v2.json"


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON object key")
    return result


def _load_json(data: bytes) -> Any:
    return json.loads(data, parse_constant=_reject_constant, object_pairs_hook=_unique_object)


def _page_groups(data: bytes) -> list[list[dict[str, Any]]]:
    try:
        groups = _load_json(data)
        if not isinstance(groups, list) or any(not isinstance(group, list) for group in groups):
            raise ValueError("expected V2 page groups")
        for group in groups:
            for node in group:
                if not isinstance(node, dict) or not string_value(node.get("type")).strip():
                    raise ValueError("expected typed V2 content")
    except (ValueError, RecursionError) as error:
        raise PipelineError("parse", "unsupported_provider_schema") from error
    return groups


def _optional_member(
    names: list[str],
    member_dir: PurePosixPath,
    basename: str,
    diagnostics: list[str],
) -> str | None:
    siblings = (member_dir / basename).as_posix()
    if siblings in names:
        return siblings
    matches = [name for name in names if PurePosixPath(name).name == basename]
    if len(matches) > 1:
        diagnostics.append(f"mineru_ambiguous_artifact {basename}")
    return matches[0] if len(matches) == 1 else None


def decode_archive(
    source_ref: FileRef,
    archive_path: Path,
    output_dir: Path,
    limits: Limits,
    *, source_type: str | None = None,
) -> ParsedContent:
    with open_validated_archive(archive_path, limits) as archive:
        names = [member.filename for member in archive.infolist() if not member.is_dir()]
        primary = [
            name
            for name in names
            if PurePosixPath(name).name == _CONTENT_LIST_NAME or PurePosixPath(name).name.endswith(f"_{_CONTENT_LIST_NAME}")
        ]
        if len(primary) != 1:
            raise PipelineError("parse", "unsupported_provider_schema")
        member = primary[0]
        groups = _page_groups(archive.read(member))

        diagnostics = []
        pdf_ref = None
        frames = []
        candidates = [name for name in names if PurePosixPath(name).name.endswith("_origin.pdf")]
        if len(candidates) == 1:
            pdf = archive.read(candidates[0])
            frames = pdf_page_frames(pdf)
            if frames:
                digest = hashlib.sha256(pdf).hexdigest()
                if digest == source_ref.sha256:
                    pdf_ref = source_ref
                else:
                    pdf_ref = FileRef(path="provider.pdf", sha256=digest)
            else:
                diagnostics.append("mineru_invalid_provider_pdf")
        elif len(candidates) > 1:
            diagnostics.append("mineru_ambiguous_provider_pdf")

        layout = None
        optional = _optional_member(names, PurePosixPath(member).parent, "layout.json", diagnostics)
        if optional is not None:
            try:
                layout = _load_json(archive.read(optional))
                if not isinstance(layout, dict) or not isinstance(layout.get("pdf_info"), list):
                    raise ValueError("expected layout object with page list")
            except (ValueError, RecursionError):
                diagnostics.append("mineru_invalid_layout")
        else:
            diagnostics.append("mineru_missing_layout")

        locations = PdfEvidenceMatcher(layout, pdf_ref, frames)
        decoder = BlockDecoder(archive, output_dir, member, locations)
        slides = (source_type or Path(source_ref.path).suffix.lower().lstrip(".")) in {"ppt", "pptx"}
        blocks = []
        for group_index, group in enumerate(groups):
            for block_index, node in enumerate(group):
                block = decoder.decode(group_index, block_index, node)
                if slides:
                    block = block.model_copy(update={"semantic_scope": group_index})
                blocks.append(block)

    if any(block.location_precision == "unresolved" for block in blocks):
        diagnostics.append("mineru_source_locations_unresolved")
    if not blocks:
        diagnostics.append("mineru_empty_document")
    blocks, inferred_headings = (blocks, False) if slides else restore_numbered_subsections(blocks)
    if inferred_headings:
        diagnostics.append("mineru_heading_levels_inferred_from_numbering")
    return ParsedContent(
        blocks=blocks,
        inline_assets=decoder.inline_assets,
        parse_status="partial" if any(isinstance(block.payload, Opaque) for block in blocks) else "complete",
        diagnostics=[*diagnostics, *decoder.diagnostics],
    )


def _roman(number: int) -> str:
    value = ""
    for digit, symbol in ((50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
        count, number = divmod(number, digit)
        value += symbol * count
    return value


def restore_numbered_subsections(blocks: list[Block]) -> tuple[list[Block], bool]:
    blocks, changed = _restore_roman_subsections(blocks)
    result = list(blocks)
    parents: list[tuple[tuple[str, ...], Heading]] = []
    for index, block in enumerate(blocks):
        heading = block.payload
        if not isinstance(heading, Heading):
            continue
        match = re.match(r"^(\d+(?:\.\d+)*)(?:\.?\s+)\S", heading.text.strip())
        if match is None:
            parents.clear()
            continue
        number = tuple(match[1].split("."))
        while parents and parents[-1][0] != number[:-1]:
            parents.pop()
        if parents and heading.level <= parents[-1][1].level:
            level = parents[-1][1].level + 1
            inline = re.sub(r"^#{1,6}\s+", "", heading.markdown or heading.text)
            heading = heading.model_copy(update={"level": level, "markdown": "#" * min(level, 6) + " " + inline})
            result[index] = block.model_copy(update={"payload": heading})
            changed = True
        parents.append((number, heading))
    return result, changed


def _restore_roman_subsections(blocks: list[Block]) -> tuple[list[Block], bool]:
    headings = [
        (index, block.payload) for index, block in enumerate(blocks) if isinstance(block.payload, Heading)
    ]
    major = []
    for index, heading in headings:
        match = re.fullmatch(r"([IVXL]+)\.\s+(.+)", heading.text.strip())
        if match and match[2].isupper():
            major.append((index, heading, match[1]))
    if (
        len(major) < 2
        or len(major) > 50
        or [label for _, _, label in major] != [_roman(number) for number in range(1, len(major) + 1)]
        or len({heading.level for _, heading, _ in major}) != 1
    ):
        return blocks, False

    result = list(blocks)
    changed = False
    for position, (start, parent, _) in enumerate(major):
        end = major[position + 1][0] if position + 1 < len(major) else len(blocks)
        children = []
        for index, heading in headings:
            if not start < index < end:
                continue
            match = re.fullmatch(r"([A-Z])\.\s+.+", heading.text.strip())
            if not match:
                break
            children.append((index, heading, match[1]))
        if (
            not children
            or len(children) > 26
            or [label for _, _, label in children] != [chr(65 + i) for i in range(len(children))]
            or any(heading.level not in {parent.level, parent.level + 1} for _, heading, _ in children)
        ):
            continue
        for index, heading, _ in children:
            if heading.level == parent.level:
                level = parent.level + 1
                inline = re.sub(r"^#{1,6}\s+", "", heading.markdown or heading.text)
                payload = heading.model_copy(
                    update={
                        "level": level,
                        "markdown": "#" * min(level, 6) + " " + inline,
                    }
                )
                result[index] = blocks[index].model_copy(update={"payload": payload})
                changed = True
    return result, changed
