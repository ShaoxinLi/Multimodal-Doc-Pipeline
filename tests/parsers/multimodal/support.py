import hashlib
import json
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image as PillowImage

from doc_pipeline.contracts.config import Limits
from doc_pipeline.contracts.core import FileRef
from doc_pipeline.parsers.multimodal.parser import decode_archive
from tests.support import pdf_bytes


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _zip(path, entries):
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return path


def _decode(path, bundle):
    source_ref = FileRef(path="source/original.pdf", sha256=_sha(pdf_bytes(1)))
    return decode_archive(source_ref, path, bundle, Limits())


def _content_archive(path, groups, entries=()):
    return _zip(path, [("provider/result_content_list_v2.json", json.dumps(groups)), *entries])


def _node(kind, **content):
    return {"type": kind, "content": content}


def convert_inline_spans(text, kind="text"):
    return {"type": kind, "content": text}


def _png():
    stream = BytesIO()
    PillowImage.new("RGB", (2, 2), "red").save(stream, format="PNG")
    return stream.getvalue()
