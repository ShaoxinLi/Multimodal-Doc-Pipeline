from hashlib import sha256

from doc_pipeline.contracts.core import FileRef, InputFile
from doc_pipeline.parsed_content.selection import canonical_text
from doc_pipeline.parsers.text.parser import parse_text_document


def parse(tmp_path, filename, text, *, encoding="utf-8", assets=None):
    raw = text.encode(encoding) if isinstance(text, str) else text
    path = tmp_path / filename
    path.write_bytes(raw)
    source = InputFile(
        path=path,
        encoding=encoding,
        assets=assets or {},
    )
    ref = FileRef(path="raw/source", sha256=sha256(raw).hexdigest())
    return parse_text_document(source, ref, tmp_path)


def assert_provenance(parsed_content, original):
    cursor = 0
    for block in parsed_content.blocks:
        span = block.locations[0]
        start, end = span.codepoints.start, span.codepoints.end
        assert cursor <= start < end <= len(original)
        assert not original[cursor:start].strip()
        assert span.source.path == "raw/source"
        if block.location_precision == "exact":
            assert canonical_text(block) == original[start:end]
        cursor = end
    assert not original[cursor:].strip()
