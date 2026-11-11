from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape

from pylatexenc.latexwalker import (
    LatexCharsNode,
    LatexCommentNode,
    LatexEnvironmentNode,
    LatexGroupNode,
    LatexMacroNode,
    LatexMathNode,
    LatexSpecialsNode,
    LatexWalker,
    LatexWalkerParseError,
    get_default_latex_context_db,
)
from pylatexenc.macrospec import EnvironmentSpec, MacroSpec, ParsedVerbatimArgs

from doc_pipeline.contracts.document import (
    Cell,
    Code,
    Equation,
    Furniture,
    Heading,
    Opaque,
    Table,
    Text,
    TextBlock,
)
from doc_pipeline.parsers.text.assets import AssetResolver, asset_html, image_payload
from doc_pipeline.parsers.text.source import TextSource, markdown_escape
from doc_pipeline.parsers.text.tex_converter import TexContentConverter, arguments, final_argument


@dataclass
class _Frame:
    kind: str = ""
    lead: str = ""
    indent: str = ""
    parent_indent: str = ""
    marker: str | None = None


_LISTS = {"itemize", "enumerate", "description"}
_QUOTES = {"quote", "quotation", "verse"}
_LISTING_END = r"\end{lstlisting}"


def _skip_options(s: str, pos: int) -> int:
    pos += len(s[pos:]) - len(s[pos:].lstrip(" \t"))
    if s[pos : pos + 1] != "[":
        return pos
    depth = 0
    for index in range(pos + 1, len(s)):
        if s[index] == "{":
            depth += 1
        elif s[index] == "}":
            depth -= 1
            if depth < 0:
                break
        elif s[index] == "]" and not depth:
            return index + 1
    raise LatexWalkerParseError(s=s, pos=pos, msg="unterminated listings options")


class _ListingBody:
    def parse_args(self, w, pos, parsing_state=None):
        body = _skip_options(w.s, pos)
        line_end = w.s.find("\n", body)
        end = w.s.find(_LISTING_END, body)
        if end == -1:
            raise LatexWalkerParseError(s=w.s, pos=pos, msg=r"Cannot find matching \end{lstlisting}")
        if line_end == -1 or end < line_end or w.s[body:line_end].strip():
            raise LatexWalkerParseError(s=w.s, pos=body, msg="text after lstlisting options")
        code = w.make_node(
            LatexCharsNode, parsing_state=parsing_state, chars=w.s[line_end + 1 : end], pos=line_end + 1,
            len=end - line_end - 1,
        )
        return ParsedVerbatimArgs(verbatim_chars_node=code), pos, end - pos


class _InlineListing:
    def parse_args(self, w, pos, parsing_state=None):
        start = _skip_options(w.s, pos)
        delimiter = w.s[start : start + 1]
        if not delimiter or delimiter.isspace() or delimiter.isalpha():
            raise LatexWalkerParseError(s=w.s, pos=start, msg=r"unsupported \lstinline delimiter")
        closing, end = delimiter, start + 1
        if delimiter == "{":
            closing, depth = "}", 1
            while end < len(w.s) and w.s[end] != "\n":
                depth += {"{": 1, "}": -1}.get(w.s[end], 0)
                if not depth:
                    break
                end += 1
            else:
                end = -1
        else:
            end = w.s.find(delimiter, end)
        if end == -1 or "\n" in w.s[start:end]:
            raise LatexWalkerParseError(s=w.s, pos=start, msg=r"unterminated \lstinline argument")
        code = w.make_node(
            LatexCharsNode, parsing_state=parsing_state, chars=w.s[start + 1 : end], pos=start + 1,
            len=end - start - 1,
        )
        argd = ParsedVerbatimArgs(verbatim_chars_node=code, verbatim_delimiters=(delimiter, closing))
        return argd, pos, end + 1 - pos


def _nodes_through_document(text: str, context) -> list | None:
    walker = LatexWalker(text, latex_context=context, tolerant_parsing=False)
    nodes, pos = [], 0
    try:
        while pos < len(text):
            found, start, length = walker.get_latex_nodes(pos=pos, read_max_nodes=1)
            if not found:
                return None
            nodes.extend(found)
            pos = start + length
            if isinstance(found[-1], LatexEnvironmentNode) and found[-1].environmentname == "document":
                return nodes
    except (LatexWalkerParseError, RecursionError):
        return None
    return None


def parse_tex(source: TextSource, resolve: AssetResolver) -> tuple[list[TextBlock], list[str]]:
    text = source.text
    context = get_default_latex_context_db()
    context.add_context_category(
        "bounded-text",
        macros=[
            MacroSpec("caption", "*[{"),
            MacroSpec("includegraphics", "*[{"),
            MacroSpec("label", "{"),
            MacroSpec("href", "[{{"),
            MacroSpec("lstinline", args_parser=_InlineListing()),
        ],
        environments=[EnvironmentSpec("lstlisting", args_parser=_ListingBody())],
        prepend=True,
    )
    diagnostics = []
    try:
        nodes, _, _ = LatexWalker(text, latex_context=context, tolerant_parsing=False).get_latex_nodes()
    except (LatexWalkerParseError, RecursionError) as error:
        nodes = _nodes_through_document(text, context)
        if nodes is None:
            return [], [f"TeX parse failed; entire source retained as opaque: {error}"]
        diagnostics.append(
            f"TeX parse failed after the document environment; trailing source retained as opaque: {error}"
        )
    records: list[TextBlock] = []
    title: tuple[str, int, int] | None = None
    syntax_nodes = TexContentConverter(text)

    def emit(a, b, payload, *, images=None, related=None):
        if text[a:b].strip():
            records.append(TextBlock(a, b, payload, images=images or [], related_ranges=related or []))

    def syntax(a, b):
        emit(a, b, Furniture(text="", role="syntax"))

    def opaque(a, b, reason):
        emit(a, b, Opaque(source_text="", syntax="tex", reason=reason))

    headings = dict(part=1, chapter=1, section=1, subsection=2, subsubsection=3, paragraph=4)
    declared = {"title", "author", "date"}
    layout = {"documentclass", "usepackage", "maketitle", "label", "centering"}
    standalone_source_commands = {
        "input",
        "include",
        "write",
        "write18",
        "openin",
        "openout",
        "newcommand",
        "renewcommand",
        "def",
        "catcode",
        "special",
    }

    def framed(a: int, b: int, items: list, frame: _Frame) -> None:
        label = None
        if isinstance(items[0], LatexMacroNode) and items[0].macroname == "item":
            args = arguments(items[0])
            if args and args[0] is not None:
                label = " ".join(syntax_nodes.plain_text([args[0]]).split())
            items = items[1:]
        body = syntax_nodes.plain_text(items, "\0")
        if frame.kind == "verse":
            lines = [" ".join(line.split()) for line in body.split("\0")]
            lines = [line for line in lines if line] or [""]
        else:
            lines = [" ".join(body.replace("\0", " ").split())]
        if label is None and not any(lines):
            frame.marker = None
            syntax(a, b)
            return
        previous = next(
            (record for record in reversed(records) if not isinstance(record.payload, Furniture)), None
        )
        continues_text_container = previous is not None and isinstance(previous.payload, Text)
        lead = frame.lead if continues_text_container else "> " * frame.lead.count(">")
        marker, frame.marker = frame.marker, None
        first, indent, md_label = lead, frame.indent, ""
        if marker:
            first = (lead[: -len(marker)] if continues_text_container else lead) + marker
            indent = frame.parent_indent + marker
        if label is not None:
            suffix = ": " if frame.kind == "description" else " "
            md_label = f"**{markdown_escape(label)}**{suffix}"
            label += suffix
        rendered = first + md_label + ("\\\n" + lead).join(markdown_escape(line) for line in lines)
        content = indent + (label or "") + ("\n" + indent).join(lines)
        role = "list" if frame.kind in _LISTS else "prose"
        atomic = role == "list" and (bool(frame.parent_indent) or marker is None)
        emit(a, b, Text(markdown=rendered.rstrip(), plain_text=content.rstrip(), role=role, atomic=atomic))

    def walk_list(node: object, parent: _Frame | None) -> None:
        base = parent or _Frame()
        segments = [[]]
        for child in node.nodelist:
            if isinstance(child, LatexMacroNode) and child.macroname == "item" and segments[-1]:
                segments.append([])
            segments[-1].append(child)
        number = 0
        for segment in segments:
            frame = _Frame(
                kind=node.environmentname,
                lead=base.lead,
                indent=base.indent + "  ",
                parent_indent=base.indent,
            )
            if isinstance(segment[0], LatexMacroNode) and segment[0].macroname == "item":
                number += 1
                frame.marker = f"{number}. " if node.environmentname == "enumerate" else "- "
                frame.lead += " " * len(frame.marker)
            walk(segment, frame)

    def merge_list(start: int, outer: _Frame | None) -> None:
        body = records[start:]
        if any(
            record.images
            or not (
                isinstance(record.payload, Text)
                and record.payload.role == "list"
                or isinstance(record.payload, Furniture)
                and record.payload.role != "metadata"
                and not text[record.start : record.end].lstrip().startswith(r"\item")
            )
            for record in body
        ):
            return
        content = [
            (index, record) for index, record in enumerate(body, start) if isinstance(record.payload, Text)
        ]
        if len(content) < 2:
            return
        first_index, first = content[0]
        last_index, last = content[-1]
        separator = "\n\n" if outer is None or ">" not in outer.lead else f"\n{outer.lead.rstrip()}\n"
        merged = Text(
            role="list",
            atomic=any(record.payload.atomic for _, record in content),
            plain_text="\n".join(record.payload.plain_text for _, record in content),
            markdown=separator.join(record.payload.markdown for _, record in content),
        )
        records[first_index : last_index + 1] = [TextBlock(first.start, last.end, merged)]

    def walk(items, frame=None):
        nonlocal title
        pending = []

        def flush():
            if not pending:
                return
            a, b = pending[0].pos, pending[-1].pos + pending[-1].len
            if frame:
                a += len(text[a:b]) - len(text[a:b].lstrip())
                b -= len(text[a:b]) - len(text[a:b].rstrip())
            if syntax_nodes.unknown(pending):
                opaque(a, b, "unsupported TeX inline syntax")
                if frame:
                    frame.marker = None
            elif any(isinstance(node, LatexMacroNode) and node.macroname == "item" for node in pending) and (
                frame is None or frame.kind not in _LISTS
            ):
                opaque(a, b, "TeX list marker outside a supported list")
            elif frame:
                framed(a, b, pending, frame)
            else:
                value = syntax_nodes.plain_text(pending).strip()
                if value:
                    emit(a, b, Text(plain_text=value, markdown=markdown_escape(value)))
                else:
                    syntax(a, b)
            pending.clear()

        for node in items:
            a, b = node.pos, node.pos + node.len
            raw = text[a:b]
            if isinstance(node, LatexCharsNode):
                start = 0
                for boundary in re.finditer(r"\n[ \t]*\n", raw):
                    if boundary.start() > start:
                        pending.append(
                            LatexCharsNode(
                                raw[start : boundary.start()], pos=a + start, len=boundary.start() - start
                            )
                        )
                    flush()
                    start = boundary.end()
                if start < len(raw):
                    pending.append(LatexCharsNode(raw[start:], pos=a + start, len=len(raw) - start))
                continue
            if isinstance(node, LatexCommentNode):
                if pending and text[text.rfind("\n", 0, a) + 1 : a].strip():
                    pending.append(node)
                else:
                    flush()
                    syntax(a, b)
                continue
            if isinstance(node, LatexMacroNode):
                name = node.macroname
                arg = final_argument(node)
                if name in headings or name in declared:
                    flush()
                    if arg is None or syntax_nodes.unknown([arg]):
                        opaque(a, b, f"unsupported {name} argument")
                    else:
                        if name in headings:
                            emit(a, b, Heading(text=syntax_nodes.plain_text([arg]), level=headings[name]))
                        else:
                            if name == "title" and title is None:
                                title = (syntax_nodes.plain_text([arg]), a, b)
                            emit(a, b, Furniture(text="", role="metadata"))
                elif name in layout:
                    flush()
                    if name == "maketitle" and title is not None:
                        value, start, end = title
                        emit(a, b, Heading(text=value, level=1), related=[(start, end)])
                    else:
                        syntax(a, b)
                else:
                    if name in standalone_source_commands and not text[text.rfind("\n", 0, a) + 1 : a].strip():
                        flush()
                    pending.append(node)
                continue
            if isinstance(node, (LatexGroupNode, LatexSpecialsNode)) or (
                isinstance(node, LatexMathNode) and node.displaytype == "inline"
            ):
                pending.append(node)
                continue
            if (
                isinstance(node, LatexEnvironmentNode)
                and node.environmentname == "em"
                and not syntax_nodes.unknown([node])
            ):
                pending.append(node)
                continue
            flush()
            if isinstance(node, LatexMathNode):
                if syntax_nodes.unknown([node]):
                    opaque(a, b, "unsupported TeX display math macro")
                else:
                    body = raw[len(node.delimiters[0]) : -len(node.delimiters[1])].strip()
                    emit(a, b, Equation(source=body))
                continue
            if not isinstance(node, LatexEnvironmentNode):
                opaque(a, b, "unsupported TeX node")
                continue
            name = node.environmentname
            if name == "document":
                if node.nodelist:
                    syntax(a, node.nodelist[0].pos)
                    walk(node.nodelist)
                    last = node.nodelist[-1].pos + node.nodelist[-1].len
                    syntax(last, b)
                else:
                    syntax(a, b)
            elif name in {"equation", "equation*", "displaymath"} and not syntax_nodes.unknown(
                node.nodelist, True
            ):
                body = re.sub(r"^\\begin\{[^}]+\}|\\end\{[^}]+\}$", "", raw).strip()
                emit(a, b, Equation(source=body))
            elif name in {"verbatim", "verbatim*"}:
                body = re.sub(r"^\\begin\{[^}]+\}|\\end\{[^}]+\}$", "", raw).strip("\r\n")
                emit(a, b, Code(text=body))
            elif name == "lstlisting":
                code = final_argument(node)
                language = re.search(r"(?:\[|,)\s*language\s*=\s*([\w+#-]+)\s*[,\]]", text[a : code.pos])
                body = re.sub(r"\n?[ \t]*\Z", "", code.chars)
                emit(a, b, Code(text=body, language=language and language[1]))
            elif name in {"figure", "figure*", "table", "table*", "tabular"}:
                captions, refs, tables, reason = syntax_nodes.float_parts(node)
                if name.startswith("figure") and not reason and refs and not tables:
                    images = [resolve(reference, "") for reference in refs]
                    if len(images) == 1 and images[0].file:
                        emit(a, b, image_payload(images[0], captions))
                    else:
                        rendered = [asset_html(image) for image in images]
                        rendered.extend(f"<p>{escape(caption)}</p>" for caption in captions)
                        emit(
                            a,
                            b,
                            Text(markdown="\n".join(rendered), plain_text="\n".join(captions)),
                            images=images,
                        )
                elif (
                    not reason
                    and not refs
                    and len(tables) == 1
                    and (rows := syntax_nodes.simple_rows(tables[0]))
                ):
                    caption_html = f"<caption>{escape(' / '.join(captions))}</caption>" if captions else ""
                    row_html = [
                        "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>" for row in rows
                    ]
                    table = Table(
                        html="<table>" + caption_html + "".join(row_html) + "</table>",
                        rows=[[Cell(text=cell) for cell in row] for row in rows],
                        captions=captions,
                    )
                    emit(a, b, table)
                else:
                    opaque(a, b, reason or "unsupported TeX float or cell structure")
            elif name in {"em", "abstract"} | _LISTS | _QUOTES:
                if any(arg is not None for arg in arguments(node)):
                    opaque(a, b, f"unsupported TeX {name} argument")
                elif not node.nodelist:
                    syntax(a, b)
                else:
                    record_start = len(records)
                    first = node.nodelist[0].pos
                    last = node.nodelist[-1].pos + node.nodelist[-1].len
                    syntax(a, first)
                    if name in _LISTS:
                        walk_list(node, frame)
                        merge_list(record_start, frame)
                    elif name in {"em", "abstract"}:
                        walk(node.nodelist, frame)
                    else:
                        outer = frame or _Frame()
                        walk(node.nodelist, _Frame(kind=name, lead=outer.lead + "> ", indent=outer.indent))
                    syntax(last, b)
                    if name in _QUOTES:
                        body_records = records[record_start:]
                        body = [record.payload for record in body_records if isinstance(record.payload, Text)]
                        if body and all(
                            not record.images
                            and (
                                (isinstance(record.payload, Furniture) and record.payload.role != "metadata")
                                or (isinstance(record.payload, Text) and record.payload.role == "prose")
                            )
                            for record in body_records
                        ):
                            content = "\n\n".join(payload.plain_text for payload in body)
                            separator = "\n" + outer.lead + ">\n"
                            rendered = separator.join(payload.markdown for payload in body)
                            del records[record_start:]
                            emit(a, b, Text(plain_text=content, markdown=rendered))
            else:
                opaque(a, b, f"unsupported TeX environment {name}")
        flush()

    walk(nodes)
    return records, diagnostics
