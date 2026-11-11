from __future__ import annotations

import json
import re
from dataclasses import replace
from html import escape

import yaml
from bs4 import BeautifulSoup, Comment
from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.footnote import footnote_plugin
from mdit_py_plugins.front_matter import front_matter_plugin

from doc_pipeline.contracts.document import (
    Cell,
    Code,
    Equation,
    Furniture,
    Heading,
    Opaque,
    Table,
    Text,
    TextAsset,
    TextBlock,
)
from doc_pipeline.parsed_content.selection import table_cell_origins
from doc_pipeline.parsers.text.assets import AssetResolver, asset_html, image_payload, safe_link
from doc_pipeline.parsers.text.source import TextSource


def _plain(tokens: list[Token]) -> str:
    out = []
    for token in tokens:
        if token.type == "math_inline":
            out.append(f"${token.content}$")
        elif token.type in {"text", "code_inline"}:
            out.append(token.content)
        elif token.type in {"softbreak", "hardbreak"}:
            out.append("\n")
        elif token.type == "footnote_ref":
            out.append(f"[^{token.meta.get('label', token.meta['id'])}]")
        elif token.children:
            out.append(_plain(token.children))
        elif token.type == "image":
            out.append(token.content)
    return "".join(out)


# Browsers end comments at "-->" or "--!>" and treat "<!-->" and "<!--->" as empty comments.
_COMMENT = r"<!--(?!-?>)(?:(?!--!?>)[\s\S])*-->"
_ANCHOR = r"""<a(?:\s+(?:id|name)\s*=\s*(?:"[^"]*"|'[^']*'))+\s*"""
_INERT_BLOCK = re.compile(rf"\s*(?:(?:{_COMMENT}|{_ANCHOR}(?:/>|>\s*</a\s*>))\s*)+", re.IGNORECASE)
_INERT_INLINE = re.compile(rf"{_COMMENT}|{_ANCHOR}/>", re.IGNORECASE)
_ANCHOR_OPEN = re.compile(rf"{_ANCHOR}>", re.IGNORECASE)
_ANCHOR_CLOSE = re.compile(r"</a\s*>", re.IGNORECASE)


def _inert_html(tokens: list[Token]) -> bool:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token.type == "html_block" and not _INERT_BLOCK.fullmatch(token.content):
            return False
        if token.type != "html_inline" or _INERT_INLINE.fullmatch(token.content):
            continue
        if not (
            _ANCHOR_OPEN.fullmatch(token.content)
            and index < len(tokens)
            and tokens[index].type == "html_inline"
            and _ANCHOR_CLOSE.fullmatch(tokens[index].content)
        ):
            return False
        index += 1
    return True


def _html_table(raw: str) -> Table:
    soup = BeautifulSoup(raw, "html.parser")
    tables = soup.find_all("table")
    if len(tables) != 1 or [node for node in soup.contents if str(node).strip()] != tables:
        raise ValueError("expected a single HTML table")
    allowed = {
        "table": {"caption", "thead", "tbody", "tfoot", "tr"},
        "thead": {"tr"},
        "tbody": {"tr"},
        "tfoot": {"tr"},
        "tr": {"th", "td"},
        "th": {"br"},
        "td": {"br"},
        "caption": {"br"},
        "br": set(),
    }
    table = tables[0]
    for tag in table.find_all(True):
        if tag.name not in allowed.get(tag.parent.name, set()) or tag.attrs:
            raise ValueError("HTML table outside simple text-cell subset")
    if table.attrs or any(
        isinstance(node, Comment) or (str(node).strip() and node.parent.name not in {"caption", "th", "td"})
        for node in table.find_all(string=True)
    ):
        raise ValueError("unsupported HTML table markup")
    rows, headers = [], []
    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"], recursive=False)
        if not cells:
            raise ValueError("empty HTML table row")
        if row.parent.name == "thead" or all(cell.name == "th" for cell in cells):
            headers.append(len(rows))
        rows.append([Cell(text=cell.get_text(separator="\n").strip()) for cell in cells])
    if not rows or len({len(row) for row in rows}) != 1:
        raise ValueError("empty or ragged HTML table")
    captions = table.find_all("caption", recursive=False)
    if len(captions) > 1:
        raise ValueError("multiple HTML table captions")
    return Table(
        html=str(table),
        rows=rows,
        verified_header_rows=headers,
        captions=[tag.get_text() for tag in captions],
    )


def _groups(tokens: list[Token]):
    index = 0
    while index < len(tokens):
        top = tokens[index]
        index += 1
        if top.level or top.nesting == -1 or top.map is None:
            continue
        start = index - 1
        if top.nesting == 1:
            while index < len(tokens):
                token = tokens[index]
                index += 1
                if token.level == 0 and token.nesting == -1:
                    break
        yield tokens[start:index]


def _row_width(raw: str) -> int:
    # Match markdown-it's GFM separator handling, including escaped final pipes.
    cols = re.split(r"(?<!\\)\|", raw.strip())
    return len(cols) - int(cols[0] == "") - int(len(cols) > 1 and cols[-1] == "")


def parse_markdown(source: TextSource, resolve: AssetResolver) -> tuple[list[TextBlock], list[str]]:
    parser = MarkdownIt("commonmark", {"html": True, "inline_definitions": True, "store_labels": True})
    parser.enable("table").use(dollarmath_plugin).use(front_matter_plugin).use(
        footnote_plugin, move_to_end=False
    )
    # Recognize even forbidden destinations so image occurrences can be reported.
    parser.validateLink = lambda _: True
    parser.normalizeLink = lambda reference: reference
    parser.renderer.rules["image"] = lambda tokens, index, *_: asset_html(tokens[index].meta["text_asset"])
    parser.renderer.rules["footnote_ref"] = lambda tokens, index, *_: escape(_plain([tokens[index]]))
    env: dict = {}
    records = []
    for group in _groups(parser.parse(source.text, env)):
        top = group[0]
        start, end = (source.line_offset(line) for line in top.map)
        raw = source.text[start:end]

        def opaque(reason: str) -> TextBlock:
            return TextBlock(start, end, Opaque(source_text=raw, syntax="md", reason=reason))

        if top.type == "front_matter":
            try:
                metadata = yaml.safe_load(top.content)
                if not isinstance(metadata, dict) or any(not isinstance(k, str) for k in metadata):
                    raise ValueError("expected string-keyed mapping")
                json.dumps(metadata, allow_nan=False)
            except (ValueError, TypeError, yaml.YAMLError, RecursionError):
                records.append(opaque("invalid front matter"))
            else:
                records.append(TextBlock(start, end, Furniture(text=raw, role="metadata")))
            continue
        if top.type in {"definition", "hr"}:
            records.append(TextBlock(start, end, Furniture(text=raw, role="syntax")))
            continue
        if top.type in {"fence", "code_block"}:
            language = (top.info.strip().split() or [None])[0]
            records.append(TextBlock(start, end, Code(text=top.content, language=language)))
            continue
        if top.type.startswith("math_block"):
            records.append(TextBlock(start, end, Equation(source=top.content.strip())))
            continue
        if top.type == "html_block":
            if _INERT_BLOCK.fullmatch(raw):
                records.append(TextBlock(start, end, Furniture(text=raw, role="syntax")))
                continue
            try:
                table = _html_table(raw)
            except ValueError as exc:
                records.append(opaque(str(exc)))
            else:
                records.append(TextBlock(start, end, table))
            continue

        inlines = [token for token in group if token.type == "inline"]
        children = [child for token in inlines for child in token.children or []]
        if not (_inert_html(group) and all(_inert_html(token.children or []) for token in inlines)):
            records.append(opaque("unsupported inline or nested HTML"))
            continue
        if top.type == "paragraph_open" and all(
            child.type in {"html_inline", "softbreak"} for child in children
        ):
            records.append(TextBlock(start, end, Furniture(text=raw, role="syntax")))
            continue
        table = None
        if top.type == "table_open":
            rows = []
            headers = []
            header = False
            row_tokens = []
            for token in group:
                if token.type in {"thead_open", "thead_close"}:
                    header = token.type == "thead_open"
                elif token.type == "tr_open":
                    row_tokens.append(token)
                    if header:
                        headers.append(len(rows))
                    rows.append([])
                elif token.type == "inline":
                    rows[-1].append(Cell(text=_plain(token.children or [])))
            if any(
                _row_width(source.text[source.line_offset(row.map[0]) : source.line_offset(row.map[1])])
                > len(rows[0])
                for row in row_tokens
            ):
                records.append(opaque("surplus GFM table cells"))
                continue
            table = Table(html="", rows=rows, verified_header_rows=headers)
            try:
                origins = table_cell_origins(table)
            except ValueError as exc:
                records.append(opaque(str(exc)))
                continue

        assets: list[TextAsset] = []
        row = column = -1
        rewrite = top.type == "table_open"
        for token in group:
            if token.type == "tr_open":
                row += 1
                column = -1
            elif token.type in {"th_open", "td_open"}:
                column += 1
            elif token.type == "inline":
                for child in token.children or []:
                    if child.type == "link_open":
                        if not safe_link(child.attrs["href"]):
                            del child.attrs["href"]
                            rewrite = True
                        rewrite = rewrite or bool(child.meta.get("label"))
                    if child.type != "image":
                        continue
                    asset = resolve(child.attrs["src"], _plain(child.children or []) or child.content)
                    if table is not None:
                        asset = replace(asset, table_cell=(row, origins[row][column]))
                    if label := child.meta.get("label"):
                        bounds = env["references"][label]["map"]
                        asset = replace(asset, definition=tuple(source.line_offset(line) for line in bounds))
                    child.meta["text_asset"] = asset
                    assets.append(asset)
                    rewrite = True
        if (
            top.type == "paragraph_open"
            and len(assets) == 1
            and assets[0].file
            and not assets[0].definition
            and len(children) == 1
            and children[0].type == "image"
        ):
            records.append(TextBlock(start, end, image_payload(assets[0])))
            continue
        rendered = parser.renderer.render(group, parser.options, env).strip() if rewrite else raw.rstrip("\n")
        if table is not None:
            records.append(TextBlock(start, end, table.model_copy(update={"html": rendered}), images=assets))
            continue
        value = "\n".join(
            _plain(token.children or []) if token.type == "inline" else token.content
            for token in group
            if token.type in {"inline", "code_block", "fence", "math_block"}
        )
        if top.type == "heading_open":
            payload = Heading(text=value, level=int(top.tag[1:]), markdown=rendered)
        else:
            role = {
                "bullet_list_open": "list",
                "ordered_list_open": "list",
                "footnote_reference_open": "footnote",
            }.get(top.type, "prose")
            atomic = any(
                token.type in {"fence", "code_block", "math_block", "table_open"} for token in group[1:]
            )
            if role == "list":
                for item in (token for token in group if token.type == "list_item_open"):
                    first, last = (source.line_offset(line) for line in item.map)
                    lines = source.text[first:last].split("\n")
                    if item.level > 1 or sum(bool(line.strip()) for line in lines) > 1:
                        atomic = True
                        break
            payload = Text(markdown=rendered, plain_text=value, role=role, atomic=atomic)
        records.append(TextBlock(start, end, payload, images=assets))
    return records, []
