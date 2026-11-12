import hashlib
import json
import posixpath
import re
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from doc_pipeline.contracts.config import Limits
from doc_pipeline.contracts.core import FileRef, PagePosition, PipelineError, SheetPosition, SlidePosition
from doc_pipeline.contracts.document import (
    Block,
    Diagnostic,
    Heading,
    Opaque,
    ParsedContent,
    ProviderMetadata,
)
from doc_pipeline.parsers.multimodal.archive import open_validated_archive
from doc_pipeline.parsers.multimodal.block_decoder import BlockDecoder
from doc_pipeline.parsers.multimodal.markup_converter import string_value
from doc_pipeline.parsers.multimodal.pdf_evidence import (
    PdfEvidenceMatcher,
    page_correspondence,
    pdf_page_frames,
)

_CONTENT_LIST_NAME = "content_list_v2.json"
_XML_LIMIT = 20_000_000
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_STRICT_REL_ID = "{http://purl.oclc.org/ooxml/officeDocument/relationships}id"


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


def _layout_member(names: list[str], member_dir: PurePosixPath, diagnostics: list[Diagnostic]) -> str | None:
    basename = "layout.json"
    siblings = (member_dir / basename).as_posix()
    if siblings in names:
        return siblings
    matches = [name for name in names if PurePosixPath(name).name == basename]
    if len(matches) == 1:
        return matches[0]
    diagnostics.append(Diagnostic(
        code="mineru_ambiguous_layout" if matches else "mineru_missing_layout",
        action="unverified", document_wide=True,
    ))
    return None


def _provider_metadata(layout: dict[str, Any]) -> ProviderMetadata:
    backend = layout.get("_backend")
    version = layout.get("_version_name")
    ocr_enabled = layout.get("_ocr_enable")
    return ProviderMetadata(
        backend=backend if isinstance(backend, str) and backend.strip() else None,
        version=version if isinstance(version, str) and version.strip() else None,
        ocr_enabled=ocr_enabled if type(ocr_enabled) is bool else None,
    )


def decode_archive(
    source_ref: FileRef | None,
    archive_path: Path | ZipFile,
    output_dir: Path,
    limits: Limits,
    *, source_type: str | None = None, original_path: Path | None = None, origin_member: str | None = None,
) -> ParsedContent:
    archived_source = isinstance(archive_path, ZipFile)
    scope = nullcontext(archive_path) if archived_source else open_validated_archive(archive_path, limits)
    with scope as archive:
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

        diagnostics: list[Diagnostic] = []
        if source_type is None and source_ref is not None:
            source_type = Path(source_ref.path).suffix.lower().lstrip(".")
        pdf_pages, office = [], {}
        if original_path is not None and source_ref is not None and source_type in {"pptx", "xlsx"}:
            origins = [name for name in names if PurePosixPath(name).name.endswith(f"_origin.{source_type}")]
            if archived_source:
                if original_path.suffix == "." + source_type:
                    office = office_locations(original_path, source_type, groups, source_ref)
            elif len(origins) == 1:
                if hashlib.sha256(archive.read(origins[0])).hexdigest() == source_ref.sha256:
                    office = office_locations(original_path, source_type, groups, source_ref)
                else:
                    diagnostics.append(Diagnostic(
                        code="mineru_office_origin_mismatch", action="unverified", document_wide=True,
                    ))
            else:
                diagnostics.append(Diagnostic(
                    code="mineru_office_origin_missing_or_ambiguous", action="unverified", document_wide=True,
                ))
        pdf_ref = None
        frames = []
        candidates = [name for name in names if PurePosixPath(name).name.endswith("_origin.pdf")]
        if archived_source:
            candidates = [origin_member] if original_path is not None and original_path.suffix == ".pdf" else []
        if len(candidates) == 1:
            if archived_source:
                pdf, digest = original_path.read_bytes(), source_ref.sha256
            else:
                pdf = archive.read(candidates[0])
                digest = hashlib.sha256(pdf).hexdigest()
            if original_path is not None and source_type == "pdf":
                pdf_pages = page_correspondence(original_path, pdf, identical=digest == source_ref.sha256)
            frames = pdf_page_frames(pdf)
            if frames:
                if source_type == "pdf" and source_ref is not None and digest == source_ref.sha256:
                    pdf_ref = source_ref
                else:
                    pdf_ref = FileRef(path=candidates[0], sha256=digest)
            else:
                diagnostics.append(Diagnostic(
                    code="mineru_invalid_provider_pdf", action="unverified", document_wide=True,
                ))
        elif len(candidates) > 1:
            diagnostics.append(Diagnostic(
                code="mineru_ambiguous_provider_pdf", action="unverified", document_wide=True,
            ))

        layout = None
        provider_metadata = ProviderMetadata()
        optional = _layout_member(names, PurePosixPath(member).parent, diagnostics)
        if optional is not None:
            try:
                layout = _load_json(archive.read(optional))
                if isinstance(layout, dict):
                    provider_metadata = _provider_metadata(layout)
                if not isinstance(layout, dict) or not isinstance(layout.get("pdf_info"), list):
                    raise ValueError("expected layout object with page list")
            except (ValueError, RecursionError):
                diagnostics.append(Diagnostic(code="mineru_invalid_layout", action="unverified", document_wide=True))

        locations = PdfEvidenceMatcher(layout, pdf_ref, frames)
        decoder = BlockDecoder(archive, output_dir, member, locations)
        slides = source_type in {"ppt", "pptx"}
        mapped_pages = {page.number: page for page in pdf_pages if page.basis == "equivalent_page"}
        blocks = []
        for group_index, group in enumerate(groups):
            for block_index, node in enumerate(group):
                block = decoder.decode(group_index, block_index, node)
                if slides:
                    block = block.model_copy(update={"semantic_scope": group_index})
                if group_index in office:
                    block = block.model_copy(update={
                        "locations": [*block.locations, office[group_index]],
                        "location_precision": "enclosing",
                        "location_error": None,
                    })
                elif source_type == "pdf" and source_ref is not None and pdf_ref != source_ref and block.locations:
                    original_pages = [
                        PagePosition(source=source_ref, number=loc.page_index + 1) for loc in block.locations
                        if loc.page_index + 1 in mapped_pages
                    ]
                    block = block.model_copy(update={
                        "locations": [*block.locations, *original_pages],
                        "location_error": None if len(original_pages) == len(block.locations)
                        else "original_pdf_page_unverified",
                    })
                blocks.append(block)

    if not blocks:
        diagnostics.append(Diagnostic(code="mineru_empty_document", action="noted", document_wide=True))
    blocks, inferred_headings = (blocks, False) if slides else restore_numbered_subsections(blocks)
    if inferred_headings:
        diagnostics.append(Diagnostic(
            code="mineru_heading_levels_inferred_from_numbering", action="inferred", document_wide=True,
        ))
    return ParsedContent(
        blocks=blocks,
        inline_assets=decoder.inline_assets,
        parse_status="partial" if any(isinstance(block.payload, Opaque) for block in blocks) else "complete",
        diagnostics=[*diagnostics, *decoder.diagnostics],
        pdf_pages=pdf_pages,
        provider_metadata=provider_metadata,
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


def _xml(archive, name: str):
    if archive.getinfo(name).file_size > _XML_LIMIT:
        raise ValueError("oversize OOXML XML")
    return ElementTree.fromstring(archive.read(name))


def _compact(text: str) -> str:
    return re.sub(r"\s", "", text)


def _spans(value):
    if isinstance(value, dict):
        content = value.get("content")
        if value.get("type") in {"text", "hyperlink", "equation_inline"} and isinstance(content, str):
            span = _compact(content)
            if span:
                yield span
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _spans(child)
    elif isinstance(value, list):
        for child in value:
            yield from _spans(child)


def _slides(archive, groups, ref):
    deck = _xml(archive, "ppt/presentation.xml")
    slides = deck.findall("{*}sldIdLst/{*}sldId")
    if len(slides) != len(groups):
        return {}
    relations = _xml(archive, "ppt/_rels/presentation.xml.rels")
    slide_relations = [rel for rel in relations
                       if rel.get("TargetMode") != "External" and rel.get("Type", "").endswith("/slide")]
    targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in slide_relations}
    if len(targets) != len(slide_relations):
        return {}
    originals = []
    for slide in slides:
        key = slide.get(_REL_ID) or slide.get(_STRICT_REL_ID)
        target = targets[key]
        path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "ppt/" + target)
        if not path.startswith("ppt/"):
            return {}
        xml = _xml(archive, path)
        originals.append(_compact("".join(node.text or "" for node in xml.findall(".//{*}t"))))
    positions = {}
    for i, group in enumerate(groups):
        extracted = set(_spans(group))
        matches = [j for j, original in enumerate(originals) if extracted and all(s in original for s in extracted)]
        if matches == [i]:
            positions[i] = SlidePosition(source=ref, number=i + 1)
    return positions


def _sheets(archive, groups, ref):
    book = _xml(archive, "xl/workbook.xml")
    sheets = book.findall("{*}sheets/{*}sheet")
    if len(sheets) != len(groups):
        return {}
    positions = {}
    known_names = {sheet.attrib["name"] for sheet in sheets}
    for i, (sheet, group) in enumerate(zip(sheets, groups, strict=True)):
        name = sheet.attrib["name"]
        titles = []
        for node in group:
            if node["type"] == "title" and isinstance(node.get("content"), dict):
                spans = node["content"].get("title_content", [])
                if isinstance(spans, list):
                    titles.append("".join(s["content"] for s in spans if isinstance(s, dict)
                                          and isinstance(s.get("content"), str)))
        if len(sheets) == 1 or known_names.intersection(titles) == {name}:
            positions[i] = SheetPosition(source=ref, name=name)
    return positions


def office_locations(path: Path, source_type: str, groups, ref: FileRef):
    try:
        with ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                return {}
            return (_slides if source_type == "pptx" else _sheets)(archive, groups, ref)
    except (BadZipFile, ElementTree.ParseError, OSError, KeyError, TypeError, ValueError, RecursionError):
        return {}
