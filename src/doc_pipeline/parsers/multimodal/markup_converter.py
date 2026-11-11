import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Comment, Declaration, Doctype, NavigableString, ProcessingInstruction, Tag

from doc_pipeline.contracts.core import FileRef
from doc_pipeline.contracts.document import Caption, CaptionPart, Cell, Table
from doc_pipeline.parsed_content.md_renderer import sanitize_html
from doc_pipeline.parsed_content.selection import caption_plain_text, table_cell_origins

_TABLE_TAGS = {
    "table",
    "thead",
    "tbody",
    "tfoot",
    "tr",
    "td",
    "th",
    "caption",
    "p",
    "br",
    "sup",
    "sub",
    "em",
    "strong",
    "b",
    "i",
    "ul",
    "ol",
    "li",
    "img",
    "a",
}


_ACTIVE_TAGS = {"script", "style", "iframe", "object", "svg", "template", "textarea", "xmp"}


def string_value(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected text")
    value.encode("utf-8")
    return value


def escape_markdown_text(text: str) -> str:
    text = re.sub(r"([#$&*<>\[\\\]^_`|~])", r"\\\1", text)
    return re.sub(r"(?m)^(\s*(?:\d{1,9}(?=[.)]))?)([+\-.)])(?=\s|$)", r"\1\\\2", text)


def _styled_text(value: str, span: dict[str, Any]) -> tuple[str, str]:
    soup = BeautifulSoup(value, "html.parser")
    allowed = {"sup", "sub", "b", "strong", "i", "em", "u", "s"}
    if soup.find(True) and all(tag.name in allowed and not tag.attrs for tag in soup.find_all(True)):
        plain = soup.get_text()
        markdown = "".join(
            str(child) if isinstance(child, Tag) else escape_markdown_text(str(child)) for child in soup.contents
        )
    else:
        plain, markdown = value, escape_markdown_text(value)
    styles = span.get("style", [])
    styles = styles if isinstance(styles, list) else []
    for field, tag in (
        ("bold", "strong"),
        ("italic", "em"),
        ("underline", "u"),
        ("strikethrough", "s"),
        ("superscript", "sup"),
        ("subscript", "sub"),
    ):
        if span.get(field) is True or field in styles:
            markdown = f"<{tag}>{markdown}</{tag}>"
    return plain, markdown


def convert_inline_spans(spans: Any, *, literal_text: bool = False) -> tuple[str, str]:
    if not isinstance(spans, list):
        raise ValueError("expected inline spans")
    plain = []
    markdown = []
    previous_kind = None
    for span in spans:
        if not isinstance(span, dict):
            raise ValueError("expected inline span")
        kind = span["type"]
        if kind == "hyperlink" and "children" in span:
            value, rendered = convert_inline_spans(span["children"], literal_text=literal_text)
        else:
            value = string_value(span["content"])
            value, rendered = (
                _styled_text(value, span)
                if kind == "text" and not literal_text
                else (value, escape_markdown_text(value))
            )
        if previous_kind == "equation_inline" and (kind == "equation_inline" or re.match(r"[A-Za-z]", value)):
            plain.append(" ")
            markdown.append(" ")
        if kind == "equation_inline":
            plain.append(f"${value}$")
            markdown.append(
                escape_markdown_text(plain[-1]) if re.search(r"(?<!\\)[$<>]|[\r\n]", value) else plain[-1]
            )
        elif kind == "code_inline":
            plain.append(value)
            fence = "`" * (max((len(m.group()) for m in re.finditer(r"`+", value)), default=0) + 1)
            padding = " " if value.startswith("`") or value.endswith("`") else ""
            markdown.append(f"{fence}{padding}{value}{padding}{fence}")
        elif kind == "md":
            plain.append(value)
            markdown.append(value)
        elif kind in {"text", "phonetic", "hyperlink"}:
            plain.append(value)
            if kind == "hyperlink":
                url = string_value(span.get("url", ""))
                if urlsplit(url).scheme.lower() in {"http", "https", "mailto"} and not re.search(
                    r"[\s<>]", url
                ):
                    rendered = f"[{rendered}](<{url}>)"
            markdown.append(rendered)
        else:
            raise ValueError("unknown inline span")
        previous_kind = kind
    return "".join(plain), "".join(markdown)


def caption_texts(value: Any) -> list[CaptionPart]:
    if value is None:
        return []
    if isinstance(value, str):
        return [string_value(value)] if value.strip() else []
    if not isinstance(value, list):
        raise ValueError("expected caption text or spans")
    groups: list[list[dict[str, Any]]] = []
    for span in value:
        if not isinstance(span, dict):
            raise ValueError("expected inline span")
        kind = span["type"]
        # V2 flattens separate captions into one span list and drops some spaces after equations.
        continues_equation = (
            groups and groups[-1][-1]["type"] == "equation_inline" and kind == "text"
            and re.match(r"[a-z.,;:!?)\]…。，；：！？]", string_value(span["content"]).lstrip())
        )
        if not groups or (kind != "equation_inline" and not continues_equation):
            groups.append([])
        groups[-1].append(span)

    captions: list[CaptionPart] = []
    for group in groups:
        text, markdown = convert_inline_spans(group)
        if text.strip():
            if any(span["type"] == "equation_inline" for span in group):
                captions.append(Caption(plain_text=text, markdown=markdown))
            else:
                captions.append(text)
    return captions


def _cell_text(tag: Tag) -> str:
    parts = []

    def visit(node):
        if isinstance(node, NavigableString):
            parts.append(str(node))
            return
        if node.name == "br":
            parts.append("\n")
        elif node.name == "img":
            parts.append(str(node.get("alt", "")))
        else:
            separator = (
                "\n"
                if node.name in {"table", "tr", "li", "p", "caption"}
                else (" " if node.name in {"td", "th"} else "")
            )
            parts.append(separator)
            for child in node.children:
                visit(child)
            parts.append(separator)

    for child in tag.children:
        visit(child)
    return "\n".join(line.strip() for line in "".join(parts).split("\n") if line.strip())


def convert_html_table(
    html: str,
    captions: list[CaptionPart],
    footnotes: list[CaptionPart],
    resolve_image: Callable[[str, str, tuple[int, int] | None], FileRef | None],
) -> Table:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(True):
        if tag.name in _ACTIVE_TAGS:
            tag.decompose()
    for comment in soup.find_all(
        string=lambda node: isinstance(node, (Comment, Declaration, Doctype, ProcessingInstruction)),
    ):
        comment.extract()
    tables = [table for table in soup.find_all("table") if table.find_parent("table") is None]
    if len(tables) != 1:
        raise ValueError("expected one top-level table")
    table_tag = tables[0]
    for tag in table_tag.find_all(True):
        if tag.name not in _TABLE_TAGS:
            tag.unwrap()
        elif tag.name not in {"td", "th", "img", "a"}:
            tag.attrs = {}
        elif tag.name == "a":
            if not isinstance(tag.get("href"), str) or not tag["href"]:
                tag.unwrap()
            else:
                tag.attrs = {
                    key: value for key in ("href", "title") if isinstance(value := tag.get(key), str)
                }
    table_tag.attrs = {}
    nested = len(table_tag.find_all("table")) > 0
    for cell in table_tag.find_all(("td", "th")):
        spans = Cell(text="", colspan=int(cell.get("colspan", 1)), rowspan=int(cell.get("rowspan", 1)))
        cell.attrs = {
            name: str(value)
            for name, value in (("colspan", spans.colspan), ("rowspan", spans.rowspan))
            if value != 1
        }
    for caption in [tag for tag in table_tag.find_all("caption") if tag.find_parent("table") is table_tag]:
        text = _cell_text(caption)
        if text and text not in map(caption_plain_text, captions):
            captions.append(text)
        caption.decompose()

    rows = []
    headers = []
    images: list[tuple[Tag, str, str, int, int]] = []
    outer_rows = [row for row in table_tag.find_all("tr") if row.find_parent("table") is table_tag]
    for row in outer_rows:
        row_tags = row.find_all(("td", "th"), recursive=False)
        cells = []
        for cell_index, cell in enumerate(row_tags):
            colspan = int(cell.get("colspan", 1))
            rowspan = int(cell.get("rowspan", 1))
            for image in cell.find_all("img"):
                reference = string_value(image.get("src", ""))
                alt = string_value(image.get("alt", ""))
                images.append((image, reference, alt, len(rows), cell_index))
            cells.append(Cell(text=_cell_text(cell), colspan=colspan, rowspan=rowspan))
        if cells and (row.find_parent("thead") or all(cell.name == "th" for cell in row_tags)):
            headers.append(len(rows))
        rows.append(cells)
    if (
        not any(rows)
        or sum(len(row) for row in rows)
        != len([cell for cell in table_tag.find_all(("td", "th")) if cell.find_parent("table") is table_tag])
        or len(images) != len(table_tag.find_all("img"))
    ):
        raise ValueError("unsupported table structure")
    table = Table(html="", rows=rows, verified_header_rows=headers, captions=captions, footnotes=footnotes)
    origins = None if nested else table_cell_origins(table)
    for image, reference, alt, row_index, cell_index in images:
        cell = None if origins is None else (row_index, origins[row_index][cell_index])
        asset = resolve_image(reference, alt, cell)
        if asset is None:
            image.replace_with(alt or reference)
        else:
            image.attrs = {"src": asset.path, "alt": alt}
    return table.model_copy(update={"html": str(sanitize_html(str(table_tag), set()))})
