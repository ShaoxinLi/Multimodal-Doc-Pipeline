import re
from collections import Counter, defaultdict
from html import escape
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, Comment, Declaration, Doctype, ProcessingInstruction
from markdown_it import MarkdownIt

from doc_pipeline.contracts.core import Range, line_ends, line_range
from doc_pipeline.contracts.document import (
    Algorithm,
    Block,
    Caption,
    CaptionPart,
    Code,
    Equation,
    Furniture,
    Heading,
    Image,
    MarkdownMapping,
    Opaque,
    ParsedContent,
    Table,
    Text,
)
from doc_pipeline.parsed_content.selection import TABLE_TAG, canonical_text, caption_plain_text, join_text

_MARKDOWN = MarkdownIt("commonmark", {"html": True}).enable("table")


# Keep invalid destinations visible to the renderer so they can be shown as source text.
_MARKDOWN.validateLink = lambda url: True


_HTML_TAGS = frozenset(
    "a img table thead tbody tfoot tr td th caption br hr sup sub em strong b i u s del "
    "p ul ol li pre code blockquote h1 h2 h3 h4 h5 h6 div span".split()
)


_ACTIVE_TAGS = frozenset("script style iframe object embed svg math textarea title xmp".split())


def _target_key(target: str) -> str:
    previous = None
    while previous != target and "%" in target:
        previous = target
        target = unquote(target)
    return target


def _safe_target(target: str, blocked: set[str], *, image: bool = False) -> bool:
    decoded = _target_key(target)
    if decoded in blocked or any(ord(char) < 32 for char in decoded):
        return False
    if decoded.lstrip().startswith(("/", "\\")) or "\\" in decoded:
        return False
    try:
        parsed = urlsplit(decoded)
    except ValueError:
        return False
    if parsed.scheme:
        return not image and parsed.scheme in {"http", "https", "mailto"}
    return not parsed.netloc and ".." not in parsed.path.split("/")


def sanitize_html(html: str, blocked: set[str]) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(string=lambda node: isinstance(node, (Comment, Declaration, Doctype, ProcessingInstruction))):
        node.extract()
    for tag in soup.find_all(True):
        if tag.name is None:
            continue
        if tag.name in _ACTIVE_TAGS:
            tag.decompose()
        elif tag.name not in _HTML_TAGS:
            tag.unwrap()
        else:
            attrs = {}
            allowed = {"a": ("href", "title", "id", "name"), "img": ("src", "alt", "title"),
                       "td": ("rowspan", "colspan"), "th": ("rowspan", "colspan"),
                       "ol": ("start",)}.get(tag.name, ())
            for key in allowed:
                value = tag.get(key)
                if not isinstance(value, str):
                    continue
                if key in {"id", "name"} and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_:.-]*", value):
                    continue
                if key in {"href", "src"} and not _safe_target(value, blocked, image=key == "src"):
                    if key == "src":
                        alt = tag.get("alt", "")
                        tag.replace_with(f"[{alt}] ({value})" if alt else value)
                    else:
                        tag.unwrap()
                    break
                if key in {"rowspan", "colspan", "start"} and not re.fullmatch(r"[0-9]{1,9}", value):
                    continue
                attrs[key] = value
            else:
                classes = tag.get("class")
                if (tag.name == "span" and classes == ["math", "inline"]
                        or tag.name == "div" and classes == ["math", "block"]):
                    attrs["class"] = " ".join(classes)
                tag.attrs = attrs
    return soup


def _safe_markdown(markdown: str, blocked: set[str]) -> str:
    def safe_html() -> str:
        html = _MARKDOWN.renderer.render(tokens, _MARKDOWN.options, {})
        # A single HTML block keeps sanitized text from being interpreted again as Markdown.
        return "<div>" + str(sanitize_html(html, blocked)).replace("\n", "&#10;") + "</div>"

    tokens = _MARKDOWN.parse(markdown)
    for token in tokens:
        for item in [token, *(token.children or [])]:
            if item.type == "html_block" or (
                item.type == "html_inline"
                and not re.fullmatch(r"</?(?:sup|sub|em|strong|b|i|u|s|br)\s*/?>", item.content)
            ):
                return safe_html()
            if item.type in {"image", "link_open"}:
                image = item.type == "image"
                target = item.attrGet("src" if image else "href")
                if isinstance(target, str) and not _safe_target(target, blocked, image=image):
                    return safe_html()
    return markdown


def _literal(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]<>#+.!|$~=\-])", r"\\\1", text.replace("&", "&amp;"))


def _caption_markdown(parts: list[CaptionPart], blocked: set[str]) -> list[str]:
    return [_safe_markdown(part.markdown, blocked) if isinstance(part, Caption) else _literal(part) for part in parts]


def _fence(text: str, language: str | None) -> str:
    size = max((len(match.group()) for match in re.finditer(r"`+", text)), default=0)
    fence = "`" * max(3, size + 1)
    language = language if language and re.fullmatch(r"[\w+.-]+", language) else ""
    newline = "" if text.endswith("\n") else "\n"
    return f"{fence}{language}\n{text}{newline}{fence}"


def _table_markdown(table: Table, blocked: set[str]) -> str:
    captions = [value for value in table.captions if caption_plain_text(value)]
    soup = sanitize_html(table.html, blocked)
    for node in list(soup.find_all(string=True)):
        if not node.strip() and node.parent.name in {"table", "thead", "tbody", "tfoot", "tr"}:
            node.extract()
    for cell in soup.find_all(("td", "th")):
        children = [child for child in cell.contents if str(child).strip()]
        if len(children) == 1 and getattr(children[0], "name", None) == "p":
            for child in list(cell.contents):
                if child is not children[0]:
                    child.extract()
            children[0].unwrap()
    simple = (table.verified_header_rows == [0] and len(table.rows) > 1
              and all(len(row) == len(table.rows[0]) for row in table.rows)
              and all(cell.rowspan == cell.colspan == 1 and "\n" not in cell.text
                      for row in table.rows for cell in row))
    rich = len(soup.find_all("table")) > 1 or soup.find(
        lambda tag: tag.name not in {"table", "thead", "tbody", "tfoot", "tr", "td", "th"}
    )
    if simple and not rich:
        rows = ["| " + " | ".join(_literal(cell.text) for cell in row) + " |" for row in table.rows]
        rows.insert(1, "| " + " | ".join("---" for _ in table.rows[0]) + " |")
        body = "\n".join(rows)
    elif TABLE_TAG.search(table.html) and soup.find("table"):
        embedded = Counter(" ".join(tag.get_text().split()) for tag in soup.find_all("caption"))
        if embedded[" ".join(join_text(captions, " / ").split())]:
            captions = []
        else:
            external = []
            for caption in captions:
                key = " ".join(caption_plain_text(caption).split())
                if embedded[key]:
                    embedded[key] -= 1
                else:
                    external.append(caption)
            captions = external
        body = str(soup).replace("\n", "&#10;").replace("</tr>", "</tr>\n").replace("<table>", "<table>\n")
    else:
        rows = []
        for index, row in enumerate(table.rows):
            tag = "th" if index in table.verified_header_rows else "td"
            cells = "".join(
                f'<{tag} rowspan="{cell.rowspan}" colspan="{cell.colspan}">{escape(cell.text)}</{tag}>' for cell in row
            )
            rows.append(f"<tr>{cells}</tr>")
        body = "<table>\n" + "\n".join(rows) + "\n</table>"
    return join_text([*_caption_markdown(captions, blocked), body, *_caption_markdown(table.footnotes, blocked)], "\n\n")


def _markdown_body(block: Block, blocked: set[str]) -> str:
    payload = block.payload
    match payload:
        case Heading(level=level, text=text, markdown=markdown):
            if markdown:
                return _safe_markdown(markdown, blocked)
            return "#" * min(level, 6) + " " + _literal(text.replace("\n", " ").replace("\r", " "))
        case Text(markdown=markdown, plain_text=text):
            return _safe_markdown(markdown, blocked) if markdown else _literal(text)
        case Table():
            return _table_markdown(payload, blocked)
        case Code(text=text, language=language):
            return join_text([*_caption_markdown(payload.captions, blocked), _fence(text, language),
                              *_caption_markdown(payload.footnotes, blocked)], "\n\n")
        case Algorithm():
            lines = []
            for line in (payload.markdown or _literal(payload.text)).split("\n"):
                indent = len(line) - len(line.lstrip(" "))
                content = line[indent:]
                content = re.sub(r"^(\d+) ( +)", lambda match: match[1] + " " + "&#160;" * len(match[2]), content)
                lines.append("&#160;" * indent + content + ("  " if line else ""))
            return join_text([*_caption_markdown(payload.captions, blocked), _safe_markdown("\n".join(lines), blocked),
                              *_caption_markdown(payload.footnotes, blocked)], "\n\n")
        case Equation(source=source, format=syntax):
            if re.search(r"(?m)^\s*\$\$\s*$", source) or syntax != "latex":
                return _fence(source, syntax)
            return _safe_markdown(f"$$\n{source}\n$$", blocked)
        case Image():
            target = payload.asset.path if payload.asset else payload.original_reference
            if payload.resolution != "blocked_reference" and target and _safe_target(target, blocked, image=True):
                destination = target.replace(" ", "%20").replace("<", "%3C").replace(">", "%3E")
                image = f"![{_literal(payload.alt)}](<{destination}>)"
            else:
                image = join_text([_literal(payload.alt), _literal(target)])
            return join_text([image, *_caption_markdown(payload.captions, blocked), _literal(payload.chart_text),
                              *_caption_markdown(payload.footnotes, blocked)], "\n\n")
        case Opaque(source_text=text, syntax=syntax):
            return _fence(text, syntax)
        case Furniture(text=text):
            return _literal(text)


def render_markdown(parsed_content: ParsedContent) -> tuple[str, list[MarkdownMapping]]:
    blocked_by_block: dict[str, set[str]] = defaultdict(set)
    for use in parsed_content.inline_assets:
        if use.resolution == "blocked_reference":
            blocked_by_block[use.owner_block_id].add(_target_key(use.original_reference))

    pieces = []
    mappings = []
    offset = 0
    line_number = 1
    for block in parsed_content.blocks:
        blocked = blocked_by_block[block.id]
        hidden = isinstance(block.payload, Furniture) and block.payload.role != "page_aside_text"
        body = "" if hidden else _markdown_body(block, blocked)
        canonical = canonical_text(block)
        start = offset
        mappings.append(MarkdownMapping(
            block_id=block.id,
            markdown_range=Range(start=start, end=start + len(body)),
            first_line=line_number,
            last_line_inclusive=line_number + line_range(line_ends(body), 0, len(body))[1] - 1,
            relation="exact" if body == canonical else "enclosing",
        ))
        piece = body + "\n\n" if body else ""
        pieces.append(piece)
        offset += len(piece)
        line_number += len(line_ends(piece))
    return "".join(pieces), mappings
