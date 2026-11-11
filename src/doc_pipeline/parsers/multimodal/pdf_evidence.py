import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from doc_pipeline.contracts.core import FileRef, PdfRegion

_MARGIN_TOLERANCE = 3.0
_MIN_INDENT = 5.0


@dataclass(frozen=True)
class LayoutBlock:
    node: dict[str, Any]
    page_index: int
    page_size: tuple[float, float]


@dataclass(frozen=True)
class BlockEvidence:
    regions: list[PdfRegion]
    reason: str | None
    paragraph_start: bool | None = None


@dataclass(frozen=True)
class PdfPageFrame:
    """A verified crop-relative layout mapped to media-top-left citation coordinates."""

    media_size: tuple[float, float]
    crop_size: tuple[float, float]
    crop_offset: tuple[float, float]


def pdf_page_frames(data: bytes) -> list[PdfPageFrame | None]:
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted or not reader.pages:
            return []
        result = []
        for page in reader.pages:
            media = tuple(float(value) for value in page.mediabox)
            crop = tuple(float(value) for value in page.cropbox)
            if (
                len(media) != 4 or len(crop) != 4
                or any(not math.isfinite(value) for value in (*media, *crop))
                or page.rotation != 0 or page.get("/UserUnit", 1) != 1
                or media[:2] != (0, 0)
                or not 0 <= crop[0] < crop[2] <= media[2]
                or not 0 <= crop[1] < crop[3] <= media[3]
            ):
                result.append(None)
                continue
            result.append(PdfPageFrame(
                media_size=(media[2], media[3]),
                crop_size=(crop[2] - crop[0], crop[3] - crop[1]),
                crop_offset=(crop[0], media[3] - crop[3]),
            ))
        return result
    except (PyPdfError, ValueError, TypeError, KeyError, IndexError, OverflowError, RecursionError):
        return []


def _spans(node: dict[str, Any]):
    if not isinstance(node, dict):
        raise ValueError("invalid layout block")
    for child in node.get("blocks", []):
        yield from _spans(child)
    for line in node.get("lines", []):
        if not isinstance(line, dict):
            raise ValueError("invalid layout line")
        for span in line.get("spans", []):
            if not isinstance(span, dict):
                raise ValueError("invalid layout span")
            yield span


def _layout_text(node: dict[str, Any]) -> str:
    parts = [_layout_text(child) for child in node.get("blocks", [])]
    for line in node.get("lines", []):
        value = []
        for span in line.get("spans", []):
            content = span.get("content", "")
            if not isinstance(content, str):
                raise ValueError("invalid span content")
            value.append(f"${content}$" if span.get("type") == "inline_equation" else content)
        parts.append("".join(value))
    return "\n".join(parts)


def _text_matches(value: str, layout: str) -> bool:
    parts = re.split(r"(?<=\w)-[ \t]*\n(?=\w)", layout)
    pattern = "-?".join(re.escape(re.sub(r"\s+", "", part)) for part in parts)
    return re.fullmatch(pattern, re.sub(r"\s+", "", value)) is not None


def _table_matches(value: str, layout: str) -> bool:
    def same(left, right):
        if isinstance(left, NavigableString) and isinstance(right, NavigableString):
            return type(left) is type(right) and _text_matches(str(left), str(right))
        if not isinstance(left, Tag) or not isinstance(right, Tag) or left.name != right.name or left.attrs != right.attrs:
            return False
        a = [child for child in left.contents if not isinstance(child, NavigableString) or child.strip()]
        b = [child for child in right.contents if not isinstance(child, NavigableString) or child.strip()]
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b))

    return same(BeautifulSoup(value, "html.parser"), BeautifulSoup(layout, "html.parser"))


def _span_text(spans: Any) -> str:
    if not isinstance(spans, list):
        raise ValueError("invalid content spans")
    result = []
    for span in spans:
        value = span["content"]
        if not isinstance(value, str):
            raise ValueError("invalid inline text")
        result.append(f"${value}$" if span["type"] == "equation_inline" else value)
    return "".join(result)


def _ancillary_matches(content: dict, kind: str, layout: dict, layout_kind: str) -> bool:
    for field in ("caption", "footnote"):
        value = content.get(f"{kind}_{field}", [])
        value = value if isinstance(value, str) else _span_text(value)
        children = [
            child for child in layout.get("blocks", []) if child.get("type") == f"{layout_kind}_{field}"
        ]
        if not _text_matches(value, "\n".join(_layout_text(child) for child in children)):
            return False
    return True


def _content_matches(block: dict[str, Any], layout: dict[str, Any]) -> bool:
    kind, content = block["type"], block["content"]
    simple = {
        "paragraph": "text",
        "title": "title",
        "page_footnote": "page_footnote",
        "page_header": "page_header",
        "page_footer": "page_footer",
        "page_number": "page_number",
        "page_aside_text": "page_aside_text",
    }
    if kind in simple:
        if layout.get("type") != simple[kind]:
            return False
        value = _span_text(content[f"{kind}_content"])
        return bool(value.strip()) and _text_matches(value, _layout_text(layout))
    if kind in {"list", "index"} and layout.get("type") in {"list", "index", "ref_text"}:
        value = "\n".join(_span_text(item["item_content"]) for item in content["list_items"])
        return bool(value.strip()) and _text_matches(value, _layout_text(layout))
    if kind in {"algorithm", "code"} and layout.get("type") == "code":
        body = [child for child in layout.get("blocks", []) if child.get("type") == "code_body"]
        value = _span_text(content[f"{kind}_content"])
        return (
            len(body) == 1
            and bool(value.strip())
            and layout.get("sub_type", "code") == kind
            and _text_matches(value, _layout_text(body[0]))
            and _ancillary_matches(content, kind, layout, "code")
        )
    if kind == "equation_interline" and layout.get("type") == "interline_equation":
        return bool(content.get("math_content")) and content["math_content"] == _layout_text(layout)
    if kind in {"image", "chart", "table"} and layout.get("type") == kind:
        spans = list(_spans(layout))
        reference = content.get("image_source", {})
        path = reference.get("path", "") if isinstance(reference, dict) else reference
        assets = [span.get("image_path") for span in spans if span.get("image_path")]
        asset_match = isinstance(path, str) and path.startswith("images/") and path[7:] in assets
        html_match = (
            kind == "table"
            and isinstance(content.get("html"), str)
            and any(
                _table_matches(
                    content["html"],
                    re.sub(r"<eq>(.*?)</eq>", r" $\1$ ", span["html"], flags=re.DOTALL),
                )
                for span in spans
                if isinstance(span.get("html"), str)
            )
        )
        if not _ancillary_matches(content, kind, layout, kind):
            return False
        if kind == "table":
            return bool(html_match and (not path or asset_match))
        extra = content.get("content", "")
        if extra and not any(extra in (span.get("content"), span.get("html")) for span in spans):
            return False
        return bool(asset_match)
    return False


def _bbox(value: Any, size: tuple[float, float]) -> tuple[float, float, float, float]:
    if (
        not isinstance(value, list)
        or len(value) != 4
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)
        or not 0 <= value[0] <= value[2] <= size[0]
        or not 0 <= value[1] <= value[3] <= size[1]
    ):
        raise ValueError("invalid page bbox")
    return tuple(value)


def _span_key(span: dict[str, Any]) -> tuple:
    return (
        span.get("type"),
        tuple(span["bbox"]),
        span.get("content"),
        span.get("image_path"),
        span.get("html"),
    )


def _line_starts(node: dict[str, Any], size: tuple[float, float]) -> tuple[float, float | None]:
    lines = node.get("lines")
    if node.get("type") != "text" or node.get("blocks") or not isinstance(lines, list) or not lines:
        raise ValueError("no paragraph lines")
    boxes = []
    for line in lines:
        if any(span.get("cross_page", False) is not False for span in line["spans"]):
            raise ValueError("cross-page line")
        boxes.append(_bbox(line["bbox"], size))
    if any(b[1] <= a[1] or b[0] >= a[2] or a[0] >= b[2] for a, b in zip(boxes, boxes[1:])):
        raise ValueError("lines leave one column")
    starts = [box[0] for box in boxes[1:]]
    if starts and max(starts) - min(starts) > _MARGIN_TOLERANCE:
        raise ValueError("unstable body margin")
    return boxes[0][0], min(starts) if starts else None


def _compatible_page_size(layout: tuple, pdf: tuple[float, float]) -> bool:
    if len(layout) != 2 or any(
        type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in layout
    ):
        return False
    return (
        all(math.isclose(value, actual, rel_tol=0, abs_tol=0.0001) for value, actual in zip(layout, pdf))
        or layout == tuple(math.floor(value) for value in pdf)
    )


class PdfEvidenceMatcher:
    def __init__(
        self,
        layout: Any,
        provider_ref: FileRef | None,
        frames: list[PdfPageFrame | None],
    ):
        self.provider_ref = provider_ref
        self.blocks: list[LayoutBlock] = []
        self.preproc: dict[int, Counter[tuple]] = {}
        self.paragraphs: dict[int, list[dict[str, Any]]] = {}
        self.frames = frames
        self.reason = (
            "provider_pdf_missing_or_invalid" if provider_ref is None else "provider_layout_unverified"
        )
        if provider_ref is None or not isinstance(layout, dict):
            return
        try:
            pages = layout["pdf_info"]
            if not isinstance(pages, list):
                return
            seen = set()
            for page in pages:
                index, size = page["page_idx"], tuple(page["page_size"])
                if (
                    type(index) is not int
                    or index in seen
                    or not 0 <= index < len(frames)
                    or frames[index] is None
                    or not _compatible_page_size(size, frames[index].crop_size)
                    or (frames[index].crop_size != frames[index].media_size
                        and _compatible_page_size(size, frames[index].media_size))
                ):
                    raise ValueError("layout page dimensions or identity not verified")
                seen.add(index)
                self.preproc[index] = Counter(
                    _span_key(span) for block in page.get("preproc_blocks", []) for span in _spans(block)
                )
                for field in ("para_blocks", "discarded_blocks"):
                    for block in page.get(field, []):
                        list(_spans(block))
                        _bbox(block.get("bbox"), frames[index].crop_size)
                        self.blocks.append(LayoutBlock(block, index, size))
                self.paragraphs[index] = [
                    block for block in page.get("para_blocks", []) if block.get("type") == "text"
                ]
            self.reason = "provider_block_not_uniquely_matched"
        except (KeyError, TypeError, ValueError, IndexError, RecursionError):
            self.blocks.clear()
            self.preproc.clear()
            self.paragraphs.clear()

    def _paragraph_start(self, block: LayoutBlock) -> bool | None:
        size = self.frames[block.page_index].crop_size
        try:
            first, margin = _line_starts(block.node, size)
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        margin = first if margin is None else margin
        indented = 0
        for other in self.paragraphs[block.page_index]:
            if other is block.node:
                continue
            try:
                other_first, other_margin = _line_starts(other, size)
            except (KeyError, TypeError, ValueError, AttributeError):
                continue
            indented += (
                other_margin is not None
                and other_first - other_margin >= _MIN_INDENT
                and abs(other_margin - margin) <= _MARGIN_TOLERANCE
            )
        if indented < 2:
            return None
        if first - margin >= _MIN_INDENT:
            return True
        return False if abs(first - margin) <= _MARGIN_TOLERANCE else None

    def locate(self, node: dict[str, Any]) -> BlockEvidence:
        if self.provider_ref is None:
            return BlockEvidence([], self.reason)
        candidates = []
        geometry_matches = 0
        try:
            _bbox(node.get("bbox"), (1000, 1000))
            for block in self.blocks:
                normalized = [
                    int(value * 1000 / block.page_size[index % 2])
                    for index, value in enumerate(block.node["bbox"])
                ]
                if normalized == node.get("bbox"):
                    geometry_matches += 1
                    if _content_matches(node, block.node):
                        candidates.append(block)
            if len(candidates) != 1:
                if not self.blocks:
                    return BlockEvidence([], self.reason)
                if len(candidates) > 1:
                    return BlockEvidence([], "provider_block_match_ambiguous")
                return BlockEvidence(
                    [],
                    "provider_block_content_unverified"
                    if geometry_matches
                    else "provider_block_bbox_unmatched",
                )
            block = candidates[0]
            spans = list(_spans(block.node))
            if any(type(span.get("cross_page", False)) is not bool for span in spans):
                return BlockEvidence([], "provider_cross_page_spans_unverified")
            cross_page = any(span.get("cross_page") is True for span in spans)
            boxes: dict[int, list[tuple]] = defaultdict(list)
            if cross_page:
                for span in spans:
                    marker = span.get("cross_page", False)
                    index = block.page_index + int(marker)
                    matches = self.preproc.get(index, {}).get(_span_key(span), 0)
                    if matches != 1:
                        return BlockEvidence([], "provider_cross_page_spans_unverified")
                    boxes[index].append(_bbox(span.get("bbox"), self.frames[index].crop_size))
            else:
                boxes[block.page_index].append(_bbox(block.node["bbox"], self.frames[block.page_index].crop_size))
                for span in spans:
                    boxes[block.page_index].append(_bbox(span.get("bbox"), self.frames[block.page_index].crop_size))
            regions = []
            for index, coordinates in sorted(boxes.items()):
                frame = self.frames[index]
                dx, dy = frame.crop_offset
                bbox = (
                    min(b[0] for b in coordinates) + dx,
                    min(b[1] for b in coordinates) + dy,
                    max(b[2] for b in coordinates) + dx,
                    max(b[3] for b in coordinates) + dy,
                )
                regions.append(
                    PdfRegion(
                        source=self.provider_ref,
                        page_index=index,
                        bbox=bbox,
                        page_size=frame.media_size,
                    )
                )
            paragraph_start = self._paragraph_start(block) if node["type"] == "paragraph" else None
            return BlockEvidence(regions, None, paragraph_start)
        except (KeyError, TypeError, ValueError, IndexError, RecursionError):
            return BlockEvidence([], "provider_block_geometry_or_content_unverified")
