import hashlib
import json
import re
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree
from zipfile import ZipFile

from markdown_it import MarkdownIt
from PIL import Image as PillowImage

from doc_pipeline.contracts.core import FileRef
from doc_pipeline.contracts.document import (
    Algorithm,
    AssetResolution,
    Block,
    Code,
    Equation,
    Furniture,
    Heading,
    Image,
    InlineAssetUse,
    Opaque,
    Payload,
    Text,
)
from doc_pipeline.parsed_content.selection import canonical_text
from doc_pipeline.parsers.multimodal.archive import archive_member_parts
from doc_pipeline.parsers.multimodal.markup_converter import (
    caption_texts,
    convert_html_table,
    convert_inline_spans,
    escape_markdown_text,
    string_value,
)
from doc_pipeline.parsers.multimodal.pdf_evidence import BlockEvidence, PdfEvidenceMatcher
from doc_pipeline.services.io import write_bytes

_FURNITURE = {"page_header", "page_footer", "page_number", "page_aside_text"}


_RASTER_SUFFIX = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
    "GIF": ".gif",
    "BMP": ".bmp",
    "TIFF": ".tif",
    "JPEG2000": ".jp2",
}


_SVG_ELEMENTS = {
    "svg": {"width", "height", "viewBox"},
    "g": {"fill", "stroke"},
    "rect": {"x", "y", "width", "height", "rx", "ry", "fill", "stroke"},
    "circle": {"cx", "cy", "r", "fill", "stroke"},
    "ellipse": {"cx", "cy", "rx", "ry", "fill", "stroke"},
    "line": {"x1", "x2", "y1", "y2", "stroke"},
    "polyline": {"points", "fill", "stroke"},
    "polygon": {"points", "fill", "stroke"},
    "path": {"d", "fill", "stroke"},
}


def _static_svg(data: bytes) -> None:
    if not data.isascii() or any(marker in data for marker in (b"<?", b"<!", b"&")):
        raise ValueError("unsupported SVG XML")
    namespace = "{http://www.w3.org/2000/svg}"
    root = ElementTree.fromstring(data)
    if root.tag != namespace + "svg":
        raise ValueError("expected SVG root")
    for element in root.iter():
        name = element.tag.removeprefix(namespace)
        if (
            not element.tag.startswith(namespace)
            or name not in _SVG_ELEMENTS
            or element.attrib.keys() - _SVG_ELEMENTS[name]
            or (element.text or "").strip()
            or (element.tail or "").strip()
            or any(not re.fullmatch(r"[A-Za-z0-9#.,+ \-]+", value) for value in element.attrib.values())
        ):
            raise ValueError("SVG outside static subset")
    if not all(1 <= int(root.attrib[axis]) <= 8192 for axis in ("width", "height")):
        raise ValueError("SVG dimensions outside static subset")


def _image_suffix(reference: str, data: bytes) -> str:
    if PurePosixPath(reference).suffix.lower() == ".svg":
        _static_svg(data)
        return ".svg"
    with PillowImage.open(BytesIO(data)) as image:
        suffix = _RASTER_SUFFIX[image.format]
        if image.width * image.height > 40_000_000:
            raise ValueError("image dimensions exceed asset limit")
        image.verify()
    with PillowImage.open(BytesIO(data)) as image:
        image.load()
    return suffix


class BlockDecoder:
    def __init__(
        self,
        archive: ZipFile,
        output_dir: Path,
        member: str,
        locations: PdfEvidenceMatcher,
    ):
        self.archive = archive
        self.output_dir = output_dir
        self.member_dir = PurePosixPath(member).parent
        self.locations = locations
        self.diagnostics: list[str] = []
        self.inline_assets: list[InlineAssetUse] = []
        self.assets: dict[str, tuple[FileRef | None, AssetResolution]] = {}

    def _resolve_asset(self, reference: str) -> tuple[FileRef | None, AssetResolution]:
        try:
            archive_member_parts(reference)
        except ValueError:
            return None, "blocked_reference"
        if reference.endswith("/"):
            return None, "blocked_reference"
        try:
            data = self.archive.read((self.member_dir / reference).as_posix())
        except KeyError:
            return None, "missing_snapshot"
        try:
            suffix = _image_suffix(reference, data)
        except (
            ValueError,
            KeyError,
            OSError,
            SyntaxError,
            PillowImage.DecompressionBombError,
        ):
            return None, "blocked_reference"

        digest = hashlib.sha256(data).hexdigest()
        relative = f"assets/{digest}{suffix}"
        path = self.output_dir / relative
        write_bytes(path, data)
        return FileRef(path=relative, sha256=digest), "archived"

    def _asset(self, reference: str, block_id: str) -> tuple[FileRef | None, AssetResolution]:
        if reference not in self.assets:
            self.assets[reference] = self._resolve_asset(reference) if reference else (None, "missing_snapshot")
        asset, resolution = self.assets[reference]
        if asset is None:
            self.diagnostics.append(f"mineru_asset_{resolution} {block_id}")
        return asset, resolution

    def _inline_asset(
        self,
        reference: str,
        block_id: str,
        alt: str = "",
        table_cell: tuple[int, int] | None = None,
    ) -> FileRef | None:
        asset, resolution = self._asset(reference, block_id)
        self.inline_assets.append(
            InlineAssetUse(
                owner_block_id=block_id,
                table_cell=table_cell,
                original_reference=reference,
                alt=alt,
                asset=asset,
                resolution=resolution,
            )
        )
        return asset

    def decode(self, group_index: int, block_index: int, node: dict[str, Any]) -> Block:
        block_id = f"g{group_index:04d}-b{block_index:04d}"
        kind, content = node["type"], node.get("content")
        try:
            if not isinstance(content, dict):
                raise ValueError("expected content object")
            payload = self._payload(kind, content, block_id)
        except (KeyError, TypeError, ValueError, RecursionError, OverflowError):
            payload = Opaque(
                syntax="mineru_v2",
                reason="unsupported_or_invalid_v2_block",
                source_text=json.dumps(node, ensure_ascii=True, separators=(",", ":")),
            )
            self.diagnostics.append(f"mineru_opaque_block {block_id}")

        evidence = (
            BlockEvidence([], "provider_block_payload_unsupported")
            if isinstance(payload, Opaque)
            else self.locations.locate(node)
        )
        if isinstance(payload, Text) and evidence.paragraph_start is not None:
            payload = payload.model_copy(update={"paragraph_start": evidence.paragraph_start})
        block = Block(
            id=block_id,
            payload=payload,
            locations=evidence.regions,
            location_precision="enclosing" if evidence.regions else "unresolved",
            location_error=evidence.reason,
        )
        if not isinstance(payload, Image) and not canonical_text(block).strip():
            self.diagnostics.append(f"mineru_empty_block {block_id}")
        if not evidence.regions:
            self.diagnostics.append(f"mineru_source_location_unresolved {block_id} {evidence.reason}")
        return block

    def _payload(
        self,
        kind: str,
        content: dict[str, Any],
        block_id: str,
    ) -> Payload:
        if kind == "title":
            text, markdown = convert_inline_spans(content["title_content"])
            level = content["level"]
            if type(level) is not int:
                raise ValueError("expected heading level")
            return Heading(
                text=text,
                level=level,
                markdown=f"{'#' * min(level, 6)} {markdown.replace(chr(13), ' ').replace(chr(10), ' ')}",
            )
        if kind == "paragraph" or kind == "page_footnote":
            text, markdown = convert_inline_spans(content[f"{kind}_content"])
            return Text(
                plain_text=text, markdown=markdown, role="footnote" if kind == "page_footnote" else "prose"
            )
        if kind in {"list", "index"}:
            items = content["list_items"]
            if not isinstance(items, list):
                raise ValueError("expected list items")
            plain, markdown = [], []
            for item in items:
                text, md = convert_inline_spans(item["item_content"])
                level = item.get("ilevel", 0)
                if type(level) is not int or not 0 <= level <= 100:
                    raise ValueError("invalid list indentation")
                prefix = string_value(item.get("prefix", ""))
                if "\n" in prefix or "\r" in prefix:
                    raise ValueError("invalid list prefix")
                prefix = prefix.strip()
                marker = prefix or ("1." if content.get("attribute") == "ordered" else "-")
                indent = "    " * level
                plain.append(f"{indent}{prefix + ' ' if prefix else ''}{text}")
                if not re.fullmatch(r"(?:[-+*]|[0-9]{1,9}[.)])", marker):
                    marker = "- " + escape_markdown_text(marker)
                markdown.append(f"{indent}{marker} {md}")
            return Text(plain_text="\n".join(plain), markdown="\n".join(markdown), role="list")
        if kind == "algorithm":
            text, markdown = convert_inline_spans(content["algorithm_content"])
            return Algorithm(
                text=text.replace("\r\n", "\n").replace("\r", "\n"),
                markdown=markdown.replace("\r\n", "\n").replace("\r", "\n"),
                captions=caption_texts(content.get("algorithm_caption")),
                footnotes=caption_texts(content.get("algorithm_footnote")),
            )
        if kind == "code":
            text, _ = convert_inline_spans(content["code_content"], literal_text=True)
            language = content.get("code_language")
            return Code(
                text=text.replace("\r\n", "\n").replace("\r", "\n"),
                language=string_value(language) if language is not None else None,
                captions=caption_texts(content.get("code_caption")),
                footnotes=caption_texts(content.get("code_footnote")),
            )
        if kind == "equation_interline":
            equation = Equation(source=string_value(content["math_content"]), format=string_value(content["math_type"]))
            if reference := _image_reference(content):
                if not equation.source.strip():
                    asset, resolution = self._asset(reference, block_id)
                    return Image(asset=asset, original_reference=reference, resolution=resolution)
                self._inline_asset(reference, block_id)
            return equation
        if kind in _FURNITURE:
            text, _ = convert_inline_spans(content[f"{kind}_content"])
            return Furniture(text=text, role=kind)
        if kind in {"image", "table", "chart"}:
            captions = caption_texts(content.get(f"{kind}_caption"))
            footnotes = caption_texts(content.get(f"{kind}_footnote"))
            extra = string_value(content.get("content", "")) if kind != "table" else ""
            reference = _image_reference(content)
            chart_html = ""
            if kind == "chart" and extra.strip():
                markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
                tokens = markdown.parse(extra)
                if tokens and tokens[0].type == "table_open" and tokens[-1].type == "table_close":
                    chart_html = markdown.renderer.render(tokens, markdown.options, {})
            if (
                kind == "table"
                or chart_html
                or (kind == "chart" and re.match(r"^\s*<table(?:\s|>)", extra, re.IGNORECASE))
            ):
                html = string_value(content["html"]) if kind == "table" else chart_html or extra
                table = convert_html_table(
                    html,
                    captions,
                    footnotes,
                    lambda src, alt, cell: self._inline_asset(src, block_id, alt, cell),
                )
                if reference:
                    self._inline_asset(reference, block_id)
                return table
            alt = string_value(content.get("alt", ""))
            asset, resolution = self._asset(reference, block_id)
            return Image(
                asset=asset,
                original_reference=reference,
                alt=alt,
                captions=captions,
                footnotes=footnotes,
                chart_text=extra,
                resolution=resolution,
            )
        raise ValueError("unknown content block type")


def _image_reference(content: dict[str, Any]) -> str:
    reference = content.get("image_source")
    if isinstance(reference, dict):
        reference = reference.get("path")
    return "" if reference is None else string_value(reference)
