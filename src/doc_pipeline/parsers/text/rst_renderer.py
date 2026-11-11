import html
import re
from collections import Counter
from dataclasses import replace

from docutils import nodes

from doc_pipeline.contracts.core import Range
from doc_pipeline.contracts.document import Cell, Table
from doc_pipeline.parsed_content.selection import table_cell_origins
from doc_pipeline.parsers.text.assets import AssetResolver, asset_html, safe_link


def fence(value: str, language: str = "") -> str:
    tick = "`" * max(3, 1 + max((len(x) for x in re.findall(r"`+", value)), default=0))
    return f"{tick}{language}\n{value.rstrip()}\n{tick}"


class RstRenderer:
    def __init__(self, tree, resolve: AssetResolver):
        self.resolve = resolve
        notes = list(tree.findall(nodes.footnote))
        self.note_names = {name for note in notes for name in note["names"]}
        self.anonymous_notes = Counter(
            note.get("auto") for note in notes if note.get("auto") and not note["names"]
        )
        self.anonymous_refs = Counter(
            ref.get("auto")
            for ref in tree.findall(nodes.footnote_reference)
            if ref.get("auto") and not ref.get("refname")
        )

    def resolved(self, ref: object) -> bool:
        if ref.get("refname"):
            return ref["refname"] in self.note_names
        return bool(ref.get("auto")) and self.anonymous_refs[ref["auto"]] <= self.anonymous_notes[ref["auto"]]

    def inline(self, node: object) -> str:
        if isinstance(node, nodes.Text):
            return re.sub(r"([\\`*_\[\]<>])", r"\\\1", node.astext()).replace("&", "&amp;")
        content = "".join(self.inline(child) for child in node.children)
        if isinstance(node, nodes.strong):
            return "**" + content + "**"
        if isinstance(node, nodes.emphasis):
            return "*" + content + "*"
        if isinstance(node, nodes.literal):
            tick = "`" * max(1, 1 + max((len(x) for x in re.findall(r"`+", node.astext())), default=0))
            return tick + " " + node.astext() + " " + tick
        if isinstance(node, nodes.math):
            return "$" + node.astext() + "$"
        if isinstance(node, nodes.reference) and node.get("refuri") and safe_link(node["refuri"]):
            return "[" + content + "](<" + node["refuri"].replace(">", "%3E") + ">)"
        if isinstance(node, nodes.footnote_reference) and self.resolved(node):
            return re.sub(r"([\\`*_\[\]<>])", r"\\\1", node.rawsource)
        if isinstance(
            node,
            (
                nodes.substitution_reference,
                nodes.footnote_reference,
                nodes.citation_reference,
                nodes.problematic,
                nodes.system_message,
            ),
        ):
            raise ValueError("unresolved substitution or note reference")
        if isinstance(node, nodes.image):
            return asset_html(self.resolve(node["uri"], node.get("alt", "")))
        return content

    def markdown(self, node: object) -> str:
        if isinstance(node, nodes.target):
            return ""
        if isinstance(
            node,
            (
                nodes.paragraph,
                nodes.caption,
                nodes.title,
                nodes.field_name,
                nodes.term,
                nodes.classifier,
                nodes.attribution,
            ),
        ):
            return self.inline(node)
        if isinstance(node, nodes.literal_block):
            classes = node.get("classes", [])
            return fence(node.astext(), classes[-1] if len(classes) > 1 else "")
        if isinstance(node, nodes.math_block):
            return "$$\n" + node.astext() + "\n$$"
        if isinstance(node, (nodes.bullet_list, nodes.enumerated_list)):
            parts = []
            for i, child in enumerate(node.children, node.get("start", 1)):
                marker = f"{i}. " if isinstance(node, nodes.enumerated_list) else "- "
                body = self.markdown(child)
                parts.append(marker + body.replace("\n", "\n" + " " * len(marker)))
            return "\n".join(parts)
        if isinstance(node, nodes.block_quote):
            return "\n".join(
                "> " + line for line in "\n\n".join(self.markdown(c) for c in node.children).split("\n")
            )
        if isinstance(node, nodes.field):
            return "**" + self.inline(node.children[0]) + ":** " + self.markdown(node.children[1])
        if isinstance(node, nodes.image):
            return self.inline(node)
        if isinstance(node, nodes.Admonition):
            titles = [self.inline(c) for c in node.children if isinstance(c, nodes.title)]
            body = [self.markdown(c) for c in node.children if not isinstance(c, nodes.title)]
            label = "**" + (titles[0] if titles else node.tagname.capitalize()) + "**"
            return "\n".join(("> " + line).rstrip() for line in "\n\n".join([label] + body).split("\n"))
        if isinstance(
            node,
            (
                nodes.list_item,
                nodes.field_list,
                nodes.field_body,
                nodes.legend,
                nodes.definition_list,
                nodes.definition_list_item,
                nodes.definition,
            ),
        ):
            return "\n\n".join(self.markdown(c) for c in node.children)
        raise ValueError(f"unsupported nested {node.tagname}")

    def images_for(self, node, *, children=None):
        def occurrences(element, start):
            if isinstance(element, nodes.image):
                yield element, Range(start=start, end=start + len(element.astext()))
            elif isinstance(element, nodes.Element):
                parts = [child.astext() for child in element.children]
                separator = element.child_text_separator
                if separator.join(parts) == element.astext():
                    for child, part in zip(element.children, parts, strict=True):
                        yield from occurrences(child, start)
                        start += len(part) + len(separator)

        if children is None:
            spans = {id(image): span for image, span in occurrences(node, 0)}
        else:
            spans, offset = {}, 0
            for child in children:
                spans.update((id(image), span) for image, span in occurrences(child, offset))
                offset += len(child.astext()) + 2
        return [
            replace(self.resolve(image["uri"], image.get("alt", "")), codepoints=spans.get(id(image)))
            for image in node.findall(nodes.image, include_self=True)
        ]


def render_table(node: nodes.table) -> Table:
    if len(list(node.findall(nodes.table, include_self=True))) != 1:
        raise ValueError("nested table retained without flattening")
    captions = [n.astext() for n in node.children if isinstance(n, nodes.title)]
    rows, rendered_rows, headers = [], [], []
    for row in node.findall(nodes.row):
        is_header = isinstance(row.parent, nodes.thead)
        if is_header:
            headers.append(len(rows))
        row_cells = [
            Cell(
                text=cell.astext(),
                rowspan=cell.get("morerows", 0) + 1,
                colspan=cell.get("morecols", 0) + 1,
            )
            for cell in row.children
        ]
        rows.append(row_cells)
        cells = []
        tag = "th" if is_header else "td"
        for entry, cell in zip(row.children, row_cells, strict=True):
            if any(
                isinstance(n, (nodes.literal_block, nodes.math_block, nodes.image)) for n in entry.findall()
            ):
                raise ValueError("complex table cell retained without flattening")
            if any(isinstance(n, (nodes.problematic, nodes.system_message)) for n in entry.findall()):
                raise ValueError("unresolved inline syntax in table cell")
            attributes = "".join(
                f' {attr}="{span}"'
                for attr, span in (("rowspan", cell.rowspan), ("colspan", cell.colspan))
                if span > 1
            )
            content = html.escape(cell.text).replace("\n", "<br>")
            cells.append(f"<{tag}{attributes}>{content}</{tag}>")
        rendered_rows.append("<tr>" + "".join(cells) + "</tr>")
    caption_html = "".join(f"<caption>{html.escape(caption)}</caption>" for caption in captions)
    table_html = "<table>" + caption_html + "".join(rendered_rows) + "</table>"
    payload = Table(html=table_html, rows=rows, verified_header_rows=headers, captions=captions)
    table_cell_origins(payload)
    return payload
