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
        read_report(root),
        _read_jsonl(root / "chunks.jsonl"),
        _read_jsonl(root / "embeddings.jsonl"),
    )


def read_report(directory):
    return json.loads((directory / "report.json").read_text())


def read_batch_reports(output, run):
    return [
        json.loads((output / item["report"]).read_text())
        for item in run["files"]
    ]


def failure_issue(report, stage):
    [issue] = [
        issue for issue in report["issues"]
        if issue["stage"] == stage and issue["action"] == "failed"
        and issue["targets"] == [{"unit": "document", "values": "n/a"}]
    ]
    return issue


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def assert_document_output(output):
    path = output / "report.json"
    report = read_report(output)
    assert set(report) == {"schema_version", "pipeline", "run", "source", "stages", "result", "issues"}, path
    assert report["schema_version"] == 2, path
    assert set(report["run"]) == {"id", "index", "started_at", "elapsed_seconds", "state"}, path
    assert set(report["source"]) == {"name", "uri", "document_id", "document_version", "format", "sha256", "bytes"}, path
    assert set(report["stages"]) == {"intake", "acquire", "parse", "render", "chunk", "embed", "write"}, path
    stage_extras = {
        "intake": {"encoding"},
        "acquire": {"service"},
        "chunk": {"settings"},
        "embed": {"profile", "usage"},
    }
    for name, stage in report["stages"].items():
        assert set(stage) == {"state", "seconds", "reason"} | stage_extras.get(name, set()), (path, name)
        assert stage["seconds"] in (None, "n/a") or stage["seconds"] >= 0, (path, name)
    files = {item.name for item in output.iterdir()}
    assert files <= {"content.md", "chunks.jsonl", "embeddings.jsonl", "report.json", "assets"}, path
    result = report["result"]
    assert set(result) == {"chunks", "locations", "embedding"}, path
    if report["run"]["state"] == "completed":
        assert {"content.md", "chunks.jsonl", "embeddings.jsonl", "report.json"} <= files, path
        chunks = _read_jsonl(output / "chunks.jsonl")
        vectors = _read_jsonl(output / "embeddings.jsonl")
        assert [chunk["id"] for chunk in chunks] == list(range(len(chunks))), path
        for group, labels, values in (
            ("chunks", {"parsed", "partial", "unparsed"}, [c["metadata"]["content_status"] for c in chunks]),
            ("locations", {"known", "partial", "unavailable"}, [c["citations"]["location_status"] for c in chunks]),
        ):
            assert set(result[group]) == labels, path
            assert result[group] == {label: values.count(label) for label in labels}, (path, group)
        embedding = result["embedding"]
        if embedding != "n/a":
            assert set(embedding) == {"succeeded", "failed", "ineligible"}, path
            assert sum(embedding.values()) == len(chunks), path
            assert embedding["succeeded"] == len(vectors), path
            assert embedding["ineligible"] == sum(c["embedding_input"] is None for c in chunks), path
        else:
            assert not vectors, path
        vector_ids = [row["chunk_id"] for row in vectors]
        assert len(set(vector_ids)) == len(vector_ids), path
        assert set(vector_ids) <= {c["id"] for c in chunks if c["embedding_input"] is not None}, path
        content_lines = len((output / "content.md").read_text().splitlines())
    else:
        assert result["chunks"] is None and result["locations"] is None, path
        assert result["embedding"] in (None, "n/a"), path
        assert files == {"report.json"}, path
        chunks, content_lines = [], 0
    grouped = set()
    for issue in report["issues"]:
        assert set(issue) == {"stage", "code", "reason", "action", "retryable", "occurrences", "targets", "unlocated"}, path
        identity = tuple(issue[key] for key in ("stage", "code", "reason", "action", "retryable"))
        assert identity not in grouped, (path, identity)
        grouped.add(identity)
        assert 0 <= issue["unlocated"] <= issue["occurrences"] and issue["occurrences"] > 0, path
        for target in issue["targets"]:
            assert set(target) == {"unit", "values"}, path
            unit, values = target["unit"], target["values"]
            assert unit in {"chunk", "content_line", "source_line", "source_page", "asset", "document"}, path
            if unit == "document":
                assert values == "n/a", path
            elif unit == "asset":
                assert values == sorted(set(values)), path
            else:
                previous = -2
                for first, last in values:
                    assert (0 if unit == "chunk" else 1) <= first <= last and first > previous + 1, path
                    previous = last
                    if report["run"]["state"] == "completed":
                        if unit == "chunk":
                            assert last < len(chunks), path
                        elif unit == "content_line":
                            assert last <= content_lines, path
    return report


def assert_batch_output(output, run):
    path = output / "run.json"
    assert json.loads(path.read_text(encoding="utf-8")) == run, path
    assert set(run) == {"schema_version", "run", "settings", "mineru_requests", "files"}, path
    assert run["schema_version"] == 2, path
    entries = run["files"]
    assert [entry["index"] for entry in entries] == list(range(len(entries))), path
    for entry in entries:
        assert set(entry) == {"index", "report", "unreported"}, path
        if entry["report"] is None:
            assert isinstance(entry["unreported"], dict), path
            continue
        assert entry["unreported"] == "n/a", path
        report_path = output / entry["report"]
        assert report_path.name == "report.json", report_path
        report = assert_document_output(report_path.parent)
        assert report["run"]["index"] == entry["index"], report_path
        assert report["run"]["id"] == run["run"]["id"], report_path
