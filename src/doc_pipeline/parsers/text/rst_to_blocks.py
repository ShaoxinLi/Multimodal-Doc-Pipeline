from __future__ import annotations

import io
import re
import textwrap
from threading import Lock

from docutils import nodes
from docutils.frontend import get_default_settings
from docutils.parsers.rst import Parser, states
from docutils.utils import new_document

from doc_pipeline.contracts.document import Code, Equation, Furniture, Heading, Opaque, Text, TextBlock
from doc_pipeline.parsers.text.assets import AssetResolver, image_payload
from doc_pipeline.parsers.text.rst_renderer import RstRenderer, render_table
from doc_pipeline.parsers.text.source import TextSource

_RST_PARSE_LOCK = Lock()
_DIRECTIVES = {
    "code",
    "code-block",
    "math",
    "image",
    "figure",
    "table",
    "list-table",
    "csv-table",
    "admonition",
    "attention",
    "caution",
    "danger",
    "error",
    "hint",
    "important",
    "note",
    "tip",
    "warning",
}


class _RestrictedDirectiveState:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.nested_sm_kwargs = {**self.nested_sm_kwargs, "state_classes": _RESTRICTED_STATES}
        self.nested_sm_cache = []

    def run_directive(self, directive, match, type_name, option_presets):
        if type_name.lower() not in _DIRECTIVES:
            return self.unknown_directive(type_name)
        return super().run_directive(directive, match, type_name, option_presets)


# Keep Docutils' state names for its transitions.
_RESTRICTED_STATES = tuple(
    type(state.__name__, (_RestrictedDirectiveState, state), {}) for state in states.state_classes
)


def parse_rst(source: TextSource, resolve: AssetResolver) -> tuple[list[TextBlock], list[str]]:
    diagnostics: list[str] = []
    if re.search(r"[\v\f\x85\u2028\u2029]", source.text):
        return [], ["RST has unsupported line separators"]
    lines = source.text.split("\n")
    warnings = io.StringIO()
    settings = get_default_settings(Parser)
    settings.file_insertion_enabled = False
    settings.raw_enabled = False
    settings.halt_level = 6
    settings.report_level = 2
    settings.warning_stream = warnings
    settings.syntax_highlight = "none"
    tree = new_document("<rst-source>", settings)
    parser = Parser()
    parser.state_classes = _RESTRICTED_STATES
    # Docutils mutates a process-wide role registry during parsing.
    with _RST_PARSE_LOCK:
        parser.parse(source.text, tree)
    # report_level hides INFO messages from the stream, but their nodes stay in the
    # tree and would otherwise leak into astext() and reject valid containers.
    for message in list(tree.findall(nodes.system_message)):
        if message.get("level", 0) < 2:
            message.parent.remove(message)
    renderer = RstRenderer(tree, resolve)

    flat = []

    def flatten(container: object, depth: int = 0) -> None:
        for node in container.children:
            if isinstance(node, nodes.section):
                flatten(node, depth + 1)
            else:
                flat.append((node, depth))

    flatten(tree)

    def adornment(line: str) -> bool:
        return bool(re.fullmatch(r"([^\w\s])\1*", line.strip()))

    def first_line(node: object) -> int | None:
        values = [n.line for n in node.findall(include_self=True) if getattr(n, "line", None)]
        return min(values) - 1 if values else None

    def anchor(node: object) -> int | None:
        start = first_line(node)
        if start is None or not 0 <= start < len(lines):
            return None
        if isinstance(node, nodes.title):
            # Docutils titles point at their underline, not their first text line.
            start = node.line - 1
            raw = node.rawsource.split("\n")
            title_start = start - len(raw)
            if title_start < 0 or lines[title_start:start] != raw or not adornment(lines[start]):
                return None
            if title_start and lines[title_start - 1].strip() == lines[start].strip():
                title_start -= 1
            return title_start
        if isinstance(node, nodes.literal_block) and "code" in node.get("classes", []):
            i = start
            while i >= 0:
                if re.match(r"^\s*\.\. (?:code|code-block)::", lines[i]):
                    return i
                if lines[i].strip() and not lines[i][0].isspace():
                    return None
                i -= 1
            return None
        if isinstance(node, nodes.literal_block):
            # Docutils consumes a standalone "::" paragraph without a node.
            i = start - 1
            while i >= 0 and not lines[i].strip():
                i -= 1
            if i >= 0 and lines[i].strip() == "::" and (i == 0 or not lines[i - 1].strip()):
                return i
        return start

    leads = {nodes.bullet_list: r"[-+*•‣⁃]\s+", nodes.enumerated_list: r"(?:\(?[\w#]+[.)])\s+"}

    def first_child_prefix(node: object) -> str | None:
        parent = node.parent
        siblings = [c for c in parent.children if not isinstance(c, nodes.label)] if parent else []
        if not siblings or siblings[0] is not node:
            return None
        if isinstance(parent, nodes.list_item):
            return leads.get(type(parent.parent))
        if isinstance(parent, nodes.footnote):
            return r"\.\. \[[^\]\s]+\]\s+"
        if isinstance(parent, nodes.Admonition):
            return r"\.\. [\w-]+::\s+"
        return None

    containers = (
        nodes.bullet_list,
        nodes.enumerated_list,
        nodes.block_quote,
        nodes.field_list,
        nodes.definition_list,
        nodes.list_item,
        nodes.definition_list_item,
        nodes.definition,
        nodes.footnote,
        nodes.Admonition,
    )
    divisible = tuple(kind for kind in containers if kind is not nodes.field_list)
    records: list[TextBlock] = []

    def interpret(sequence: list, bound: int) -> None:
        starts = [anchor(n) for n, _ in sequence]
        for index, (node, depth) in enumerate(sequence):
            start = starts[index]
            if start is None:
                if not isinstance(node, (nodes.target, nodes.system_message)):
                    diagnostics.append(f"RST {node.tagname} has no verified source anchor; retained opaque.")
                continue
            later = [value for value in starts[index + 1 :] if value is not None and value > start]
            end = min(later + [bound])
            if start >= end:
                continue
            # Some directives relocate nodes or report the line after their source.
            base_indent = len(lines[start]) - len(lines[start].lstrip())
            for boundary in range(start + 1, end):
                line = lines[boundary]
                indent = len(line) - len(line.lstrip())
                peer_indent = (
                    indent < base_indent if isinstance(node, nodes.block_quote) else indent <= base_indent
                )
                if line.lstrip().startswith(".. ") and peer_indent:
                    end = boundary
                    break
            while end > start and not lines[end - 1].strip():
                end -= 1
            if end <= start:
                continue
            content = node.astext()
            images = []
            payload = None
            rendered = ""
            role = "prose"
            atomic = False
            try:
                if isinstance(node, nodes.title):
                    end = node.line
                    rendered = renderer.inline(node)
                    images = renderer.images_for(node)
                    payload = Heading(
                        text=content,
                        level=max(1, depth),
                        markdown=f"{'#' * min(max(1, depth), 6)} {rendered}",
                    )
                elif isinstance(node, nodes.paragraph):
                    expected = node.rawsource.split("\n")
                    actual = [s.expandtabs(8).strip() for s in lines[start : start + len(expected)]]
                    prefix = first_child_prefix(node)
                    if prefix and actual and re.fullmatch(prefix + re.escape(expected[0].strip()), actual[0]):
                        actual[0] = expected[0].strip()
                    if actual != [s.strip() for s in expected]:
                        raise ValueError("paragraph source mismatch")
                    end = start + len(expected)
                    rendered = renderer.markdown(node)
                    images = renderer.images_for(node)
                elif isinstance(node, nodes.term):
                    if lines[start].strip() != node.rawsource.strip():
                        raise ValueError("term source mismatch")
                    end = start + 1
                    rendered = renderer.markdown(node)
                elif isinstance(node, nodes.target):
                    expected = node.rawsource.split("\n")
                    if [s.strip() for s in lines[start : start + len(expected)]] != [
                        s.strip() for s in expected
                    ]:
                        raise ValueError("target source mismatch")
                    end = start + len(expected)
                    payload = Furniture(text="", role="syntax")
                elif isinstance(node, containers):
                    if isinstance(node, nodes.list_item):
                        pattern = r"^\s*" + leads[type(node.parent)]
                    elif isinstance(node, nodes.Admonition):
                        pattern = r"^\s*\.\. " + node.tagname + "::"
                    else:
                        pattern = {
                            nodes.bullet_list: r"^\s*[-+*•‣⁃]\s+",
                            nodes.enumerated_list: r"^\s*(?:\(?[\w#]+[.)])\s+",
                            nodes.block_quote: r"^\s+\S",
                            nodes.field_list: r"^\s*:[^:]+:",
                            nodes.definition_list: r"^\S"
                            if isinstance(node.parent, (nodes.document, nodes.section))
                            else r"^\s*\S",
                            nodes.definition_list_item: r"^\s*\S",
                            nodes.definition: r"^\s+\S",
                            nodes.footnote: r"^\s*\.\. \[[^\]\s]+\](?:\s|$)",
                        }[type(node)]
                    if not re.match(pattern, lines[start], re.IGNORECASE):
                        raise ValueError("container source mismatch")
                    atomic = any(
                        isinstance(child, (nodes.literal_block, nodes.math_block)) for child in node.findall()
                    )
                    if isinstance(node, nodes.footnote):
                        body = [child for child in node.children if not isinstance(child, nodes.label)]
                        marker = re.match(r"^\s*\.\. (\[[^\]\s]+\])", lines[start])[1]
                        content = "\n\n".join(child.astext() for child in body)
                        rendered = (
                            re.sub(r"([\\`*_\[\]<>])", r"\\\1", marker)
                            + " "
                            + "\n\n".join(renderer.markdown(child) for child in body)
                        )
                        role = "footnote"
                    else:
                        rendered = renderer.markdown(node)
                    images = renderer.images_for(node, children=body if isinstance(node, nodes.footnote) else None)
                    if isinstance(node, nodes.list_item):
                        ordinal = node.parent.get("start", 1) + node.parent.index(node)
                        marker = f"{ordinal}. " if isinstance(node.parent, nodes.enumerated_list) else "- "
                        rendered = marker + rendered.replace("\n", "\n" + " " * len(marker))
                    if isinstance(node, nodes.field_list) and not records:
                        payload = Furniture(text="", role="metadata")
                    elif isinstance(node, (nodes.list_item, nodes.bullet_list, nodes.enumerated_list)):
                        role = "list"
                        atomic = atomic or any(
                            len(item.children) != 1
                            or not isinstance(item.children[0], nodes.paragraph)
                            or "\n" in item.children[0].astext()
                            for item in node.findall(nodes.list_item, include_self=True)
                        )
                elif isinstance(node, nodes.table):
                    if not re.match(r"^\s*(?:[+=]|\.\. (?:list-table|csv-table|table)::)", lines[start]):
                        raise ValueError("table source mismatch")
                    payload = render_table(node)
                elif isinstance(node, (nodes.figure, nodes.image)):
                    if not re.match(r"^\s*\.\. (?:image|figure)::", lines[start]):
                        raise ValueError("image source mismatch")
                    images = renderer.images_for(node)
                    captions = [child.astext() for child in node.children if isinstance(child, nodes.caption)]
                    if (
                        len(images) == 1
                        and images[0].file
                        and all(isinstance(child, (nodes.image, nodes.caption)) for child in node.children)
                    ):
                        payload = image_payload(images[0], captions)
                        images = []
                    else:
                        fragments = (
                            [renderer.markdown(child) for child in node.children]
                            if isinstance(node, nodes.figure)
                            else [renderer.markdown(node)]
                        )
                        rendered = "\n\n".join(fragments)
                        atomic = True
                        content = "\n".join(
                            part
                            for part in [
                                *(image.alt for image in images),
                                *(
                                    child.astext()
                                    for child in node.children
                                    if not isinstance(child, nodes.image)
                                ),
                            ]
                            if part
                        )
                elif isinstance(node, (nodes.literal_block, nodes.math_block)):
                    if isinstance(node, nodes.math_block):
                        if not re.match(r"^\s*\.\. math::", lines[start]):
                            raise ValueError("math source mismatch")
                        payload = Equation(source=content)
                    else:
                        directive = re.match(r"^\s*\.\. (?:code|code-block)::", lines[start])
                        if not directive:
                            body = first_line(node)
                            expected = node.rawsource.split("\n")
                            # Docutils reads lines as expandtabs(tab_width).rstrip() before parsing.
                            actual = textwrap.dedent(
                                "\n".join(
                                    line.expandtabs(settings.tab_width).rstrip()
                                    for line in lines[body : body + len(expected)]
                                )
                            )
                            if actual != node.rawsource:
                                raise ValueError("literal source mismatch")
                            end = body + len(expected)
                        classes = node.get("classes", [])
                        payload = Code(text=content, language=classes[-1] if len(classes) > 1 else None)
                else:
                    continue
            except ValueError as exc:
                if isinstance(node, divisible) and node.children:
                    diagnostics.append(f"RST line {start + 1}: {exc}; children interpreted separately")
                    interpret([(child, depth) for child in node.children], end)
                    continue
                payload = Opaque(source_text="", syntax="rst", reason=str(exc))
                images = []
            a, b = source.line_offset(start), source.line_offset(end)
            if records and a < records[-1].end:
                diagnostics.append(f"RST line {start + 1}: {node.tagname} overlaps previous record")
                continue
            if payload is None:
                payload = Text(plain_text=content, markdown=rendered, role=role,
                               atomic=atomic or any(asset.codepoints is None for asset in images))
            records.append(TextBlock(a, b, payload, images=images))

    interpret(flat, len(lines))
    diagnostics.extend(line for line in warnings.getvalue().splitlines() if line.strip())
    return records, diagnostics
