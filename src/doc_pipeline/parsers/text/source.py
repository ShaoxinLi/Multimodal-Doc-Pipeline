from __future__ import annotations

import re
from bisect import bisect_right

from doc_pipeline.contracts.core import FileRef, Range, TextSpan

_NEWLINE = re.compile(r"\r\n|\r|\n")


class TextSource:
    def __init__(self, original: str, file: FileRef):
        self.original = original
        self.file = file
        self.bom = original.startswith("\ufeff")
        self.text = _NEWLINE.sub("\n", original.removeprefix("\ufeff"))
        self._original_lines = [0, *(match.end() for match in _NEWLINE.finditer(original))]
        self._lines = [0, *(match.end() for match in re.finditer("\n", self.text))]

    def line_offset(self, index: int) -> int:
        return self._lines[index] if index < len(self._lines) else len(self.text)

    def span(self, start: int, end: int) -> TextSpan:
        first_line = bisect_right(self._lines, start)
        last_line = bisect_right(self._lines, end - 1)
        return self.original_span(
            self._original_offset(start), self._original_offset(end), first_line, last_line
        )

    def original_span(self, start: int, end: int, first_line: int, last_line: int) -> TextSpan:
        return TextSpan(
            source=self.file,
            codepoints=Range(start=start, end=end),
            first_line=first_line,
            last_line_inclusive=last_line,
        )

    def _original_offset(self, offset: int) -> int:
        line = bisect_right(self._lines, offset) - 1
        return self._original_lines[line] + offset - self._lines[line] + int(line == 0 and self.bom)


def markdown_escape(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|$~=\-])", r"\\\1", value).replace("&", "&amp;")
