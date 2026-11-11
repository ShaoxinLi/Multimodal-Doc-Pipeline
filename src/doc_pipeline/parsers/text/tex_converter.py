from __future__ import annotations

import re

from pylatexenc.latex2text import LatexNodes2Text
from pylatexenc.latexwalker import (
    LatexCharsNode,
    LatexCommentNode,
    LatexEnvironmentNode,
    LatexGroupNode,
    LatexMacroNode,
    LatexMathNode,
    LatexSpecialsNode,
)

_TEXT_ARGUMENT_MACROS = {
    "textbf",
    "textit",
    "emph",
    "textrm",
    "texttt",
    "textsf",
    "textsc",
    "textsl",
    "textup",
    "textnormal",
    "underline",
    "mbox",
    "footnote",
    "href",
}
_SWITCHES = {
    "em",
    "itshape",
    "bfseries",
    "ttfamily",
    "sffamily",
    "rmfamily",
    "scshape",
    "slshape",
    "upshape",
    "mdseries",
    "normalfont",
}

_SAFE_MACROS = (
    _TEXT_ARGUMENT_MACROS
    | _SWITCHES
    | {
        "LaTeX",
        "TeX",
        "%",
        "&",
        "#",
        "_",
        "$",
        "{",
        "}",
        "ldots",
        "dots",
        "newline",
        "\\",
        "url",
        "lstinline",
        "item",
        ",",
        " ",
        "@",
    }
)

_MATH_MACROS = {
    "label",
    "tag",
    "frac",
    "sqrt",
    "int",
    "sum",
    "prod",
    "cdot",
    "times",
    "alpha",
    "beta",
    "gamma",
    "theta",
    "lambda",
    "pi",
    "infty",
    "le",
    "leq",
    "ge",
    "geq",
    "ne",
    "neq",
    "left",
    "right",
}


def arguments(node):
    return node.nodeargd.argnlist if node.nodeargd else []


def final_argument(node):
    args = arguments(node)
    return args[-1] if args else None


class TexContentConverter:
    def __init__(self, text: str):
        self.text = text
        self.converter = LatexNodes2Text(math_mode="verbatim")

    def unknown(self, items: list, in_math=False) -> bool:
        for node in items:
            if (
                isinstance(node, LatexMacroNode)
                and node.macroname == "href"
                and node.nodeargd
                and node.nodeargd.argnlist[0] is not None
            ):
                return True
            if isinstance(node, LatexMacroNode) and node.macroname not in (
                _SAFE_MACROS | _MATH_MACROS if in_math else _SAFE_MACROS
            ):
                return True
            if isinstance(node, LatexMacroNode) and node.nodeargd:
                if any(self.unknown([arg], in_math) for arg in node.nodeargd.argnlist if arg is not None):
                    return True
            if isinstance(node, LatexEnvironmentNode):
                if (
                    node.environmentname == "em"
                    and not (node.nodeargd and any(arg is not None for arg in node.nodeargd.argnlist))
                    and not re.search(r"\n[ \t]*\n", self.raw_node(node))
                    and not any(
                        isinstance(child, LatexMathNode) and child.displaytype != "inline"
                        for child in node.nodelist
                    )
                    and not self.unknown(node.nodelist, in_math)
                ):
                    continue
                return True
            if isinstance(node, LatexMathNode) and self.unknown(node.nodelist, True):
                return True
            if isinstance(node, LatexGroupNode) and self.unknown(node.nodelist, in_math):
                return True
        return False

    def raw_node(self, node):
        return self.text[node.pos : node.pos + node.len]

    def literal_argument(self, node):
        if isinstance(node, LatexCharsNode):
            return node.chars
        if isinstance(node, LatexGroupNode) and all(
            isinstance(part, LatexCharsNode) for part in node.nodelist
        ):
            return self.raw_node(node)[1:-1]
        return None

    def plain_text(self, items: list, br: str = "\n") -> str:
        out, plain = [], []

        def drain():
            if plain:
                out.append(self.converter.nodelist_to_text(plain))
                plain.clear()

        for node in items:
            name = node.macroname if isinstance(node, LatexMacroNode) else None
            if name in _TEXT_ARGUMENT_MACROS:
                drain()
                arg = final_argument(node)
                inner = self.plain_text([arg], br) if arg is not None else ""
                if name == "footnote":
                    inner = f"[{inner}]"
                elif name == "href" and (url := node.nodeargd.argnlist[-2]) is not None:
                    target = self.raw_node(url)
                    if isinstance(url, LatexGroupNode):
                        target = target[1:-1]
                    inner += f" <{target}>"
                out.append(inner)
            elif name == "lstinline":
                drain()
                out.append(node.nodeargd.verbatim_text)
            elif name in {"LaTeX", "TeX"}:
                drain()
                out.append(name + (" " if node.macro_post_space else ""))
            elif name in _SWITCHES:
                drain()
            elif name in {"\\", "newline"}:
                drain()
                out.append(br)
            elif isinstance(node, LatexGroupNode) or (
                isinstance(node, LatexEnvironmentNode) and node.environmentname == "em"
            ):
                drain()
                out.append(self.plain_text(node.nodelist, br))
            else:
                plain.append(node)
        drain()
        return "".join(out)

    def simple_rows(self, tabular: object) -> list[list[str]] | None:
        spec = final_argument(tabular)
        columns = self.literal_argument(spec)
        if columns is None or not re.fullmatch(r"[lcr| ]+", columns):
            return None
        count = sum(char in "lcr" for char in columns)
        rows, row, cell = [], [], []
        for child in tabular.nodelist:
            if isinstance(child, LatexSpecialsNode) and child.specials_chars == "&":
                row.append("".join(cell).strip())
                cell = []
            elif isinstance(child, LatexMacroNode) and child.macroname == "\\":
                if child.nodeargd and any(arg is not None for arg in child.nodeargd.argnlist):
                    return None
                row.append("".join(cell).strip())
                rows.append(row)
                row, cell = [], []
            elif isinstance(child, LatexMacroNode) and child.macroname == "hline":
                if row or "".join(cell).strip():
                    return None
            elif isinstance(child, LatexCommentNode):
                continue
            elif isinstance(child, LatexMathNode) and not self.unknown([child]):
                cell.append(self.raw_node(child))
            elif self.unknown([child]):
                return None
            else:
                cell.append(self.plain_text([child]))
        if row or "".join(cell).strip():
            row.append("".join(cell).strip())
            rows.append(row)
        return rows if rows and all(len(row) == count for row in rows) else None

    def float_parts(self, node: object) -> tuple[list[str], list[str], list, str | None]:
        captions, refs, tables = [], [], []
        if node.environmentname.startswith("tabular"):
            return captions, refs, [node], None
        for child in node.nodelist:
            if isinstance(child, LatexCommentNode) or (
                isinstance(child, LatexCharsNode) and not child.chars.strip()
            ):
                continue
            if isinstance(child, LatexEnvironmentNode) and child.environmentname == "tabular":
                tables.append(child)
                continue
            if not isinstance(child, LatexMacroNode):
                return captions, refs, tables, "unsupported float body node"
            if child.macroname in {"centering", "label"}:
                continue
            if child.macroname not in {"caption", "includegraphics"}:
                return captions, refs, tables, "unsupported float macro: " + child.macroname
            arg = final_argument(child)
            if arg is None or self.unknown([arg]):
                return captions, refs, tables, "unsupported float argument"
            if child.macroname == "caption":
                captions.append(self.plain_text([arg]))
            elif (reference := self.literal_argument(arg)) is not None:
                refs.append(reference)
            else:
                return captions, refs, tables, "nonliteral image path"
        return captions, refs, tables, None
