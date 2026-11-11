import json
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from pypdf import PdfWriter

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def pdf_bytes(pages=2, *, width=100, height=100) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=width, height=height)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def write_source_pdf(tmp_path: Path) -> Path:
    source = tmp_path / "input.pdf"
    source.write_bytes(pdf_bytes(1))
    return source


def document_archive(origin_pdf: bytes) -> bytes:
    groups, pages = [], []
    for index in range(2):
        group, blocks = [], []
        for kind, layout_kind, value, bbox in (
            ("title", "title", f"Pipeline probe section {index + 1}", [10, 10, 90, 20]),
            ("paragraph", "text", "Repeated paragraph.", [10, 30, 90, 40]),
        ):
            content = {f"{kind}_content": [{"type": "text", "content": value}]}
            if kind == "title":
                content["level"] = 1
            group.append({"type": kind, "content": content, "bbox": [coordinate * 10 for coordinate in bbox]})
            blocks.append({
                "type": layout_kind,
                "bbox": bbox,
                "lines": [{"spans": [{"type": "text", "content": value, "bbox": bbox}]}],
            })
        groups.append(group)
        pages.append({"page_idx": index, "page_size": [100, 100], "para_blocks": blocks})
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("result_content_list_v2.json", json.dumps(groups))
        archive.writestr("layout.json", json.dumps({"pdf_info": pages}))
        archive.writestr("result_origin.pdf", origin_pdf)
    return stream.getvalue()


def read_output(root):
    return (
        json.loads((root / "report.json").read_text()),
        [json.loads(line) for line in (root / "chunks.jsonl").read_text().splitlines()],
        [json.loads(line) for line in (root / "embeddings.jsonl").read_text().splitlines()],
    )
