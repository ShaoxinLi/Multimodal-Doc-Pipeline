import json
import logging
import uuid

import httpx
import pytest
from markdown_it import MarkdownIt
from PIL import Image

from doc_pipeline import (
    Config,
    InputFile,
    PipelineError,
    SourceIdentity,
    UrlInput,
    pipeline,
    process_file,
    process_files,
)
from doc_pipeline.chunking.citations import chunk_digest, digest
from doc_pipeline.contracts.chunk import ExportedChunk
from doc_pipeline.contracts.config import Limits
from tests.support import read_output, read_report


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_exported_markdown_citations_cover_their_blocks_across_newline_styles(tmp_path, newline):
    path = tmp_path / "input.md"
    path.write_bytes(
        newline.join(["# Heading", "", "<div>", "Hidden literal", "</div>", "", "Afterward", ""]).encode()
    )
    output = tmp_path / "out"
    process_file(path, output)
    _, chunks, _ = read_output(output)
    markdown = (output / "content.md").read_bytes().decode()
    tokens = [
        token
        for token in MarkdownIt().parse(markdown)
        if token.type in {"fence", "paragraph_open"}
    ]
    expected = {(token.map[0] + 1, token.map[1]) for token in tokens}
    assert {tuple(citation) for chunk in chunks for citation in chunk["citations"]["content_ranges"]} == expected


def _read_chunks(root):
    return [ExportedChunk.model_validate_json(line) for line in (root / "chunks.jsonl").read_text().split("\n") if line]


def test_native_url_follows_redirect_and_preserves_signed_query_and_source_bytes(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    source_url = "https://source.test/notes/caf%C3%A9.md?signature=Private%2fSigned+Query&item=1#viewer"
    raw = "# Source\r\n\r\ncafé\r\n\r\n![Explicit](plot.png) ![Missing](nearby.png)\r\n".encode("latin-1")
    Image.new("RGB", (2, 2)).save(tmp_path / "plot.png")
    Image.new("RGB", (2, 2)).save(tmp_path / "nearby.png")
    monkeypatch.chdir(tmp_path)
    requests, progress = [], []

    def handle(request):
        requests.append(request)
        assert not {"authorization", "cookie", "x-private"} & set(request.headers)
        if request.headers["host"] == "source.test":
            assert request.url.host == "source.test"
            assert request.url.raw_path == b"/notes/caf%C3%A9.md?signature=Private%2fSigned+Query&item=1"
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/document?key=a%2Bb"})
        assert request.headers["host"] == "127.0.0.1"
        assert request.url.host == "127.0.0.1"
        assert request.url.raw_path == b"/document?key=a%2Bb"
        return httpx.Response(200, content=raw)

    out = tmp_path / "out"
    item = UrlInput(url=source_url, encoding="latin-1", assets={"plot.png": tmp_path / "plot.png"})
    with httpx.Client(transport=httpx.MockTransport(handle), auth=("a", "b"), cookies={"session": "private"},
                      headers={"X-Private": "private"}, params={"private": "default"}) as client:
        report = process_file(item, out, client=client, on_progress=progress.append)
    assert len(requests) == 2
    assert [event.stage for event in progress] == ["intake", "source_download", "parse", "complete"]
    assert all(event.path.name == "café.md" for event in progress)
    assert (out / "source" / "café.md").read_bytes() == raw
    assert "café" in (out / "content.md").read_text()
    assert report["stages"]["acquire"]["state"] == "succeeded"
    assert report["stages"]["acquire"]["service"] == "n/a"
    assert report["source"]["bytes"] == len(raw)
    assert report["source"]["uri"] == "https://source.test/notes/caf%C3%A9.md"
    assert report["source"]["document_id"] == str(uuid.uuid5(uuid.NAMESPACE_URL, source_url.partition("#")[0]))
    assert report["source"]["sha256"] == digest(raw)
    assert len(list((out / "assets").iterdir())) == 1
    assert any(issue["code"] == "asset_unavailable" and issue["targets"] == [
        {"unit": "asset", "values": ["nearby.png"]},
    ] for issue in report["issues"])
    for row in _read_chunks(out):
        assert row.citations.source_uri == report["source"]["uri"]
        assert row.metadata.source_sha256 == digest(raw)
    assert "Private" not in caplog.text + (out / "report.json").read_text() + (out / "chunks.jsonl").read_text()


@pytest.mark.parametrize("location", ["ftp://source.test/private?query=secret",
                                      "http://user:password@source.test/private?query=secret"])
def test_native_url_revalidates_a_redirect_before_sending_it(tmp_path, location):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": location})

    out = tmp_path / "out"
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(PipelineError, match="unsafe_source_url"):
            process_file(UrlInput(url="http://source.test/source.txt?query=secret"), out, client=client)
    assert len(requests) == 1
    report = read_report(out)
    assert report["run"]["state"] == "failed"
    assert report["source"]["uri"] == "http://source.test/source.txt"
    assert not (out / "source").exists()
    assert "secret" not in (out / "report.json").read_text()


def test_credentialed_url_fails_without_exposing_its_authority_or_query(tmp_path):
    output = tmp_path / "out"
    with pytest.raises(PipelineError, match="unsafe_source_url"):
        process_file(UrlInput(url="https://private-user:private-key@source.test/file.pdf?query=private-signature"),
                     output)
    assert read_report(output)["source"]["uri"] is None
    assert "private-" not in (output / "report.json").read_text()


def test_native_url_size_limit_stops_stream_and_publishes_only_failure_report(tmp_path, monkeypatch):
    consumed = []

    class SourceStream(httpx.SyncByteStream):
        def __iter__(self):
            for chunk in (b"hello", b" world", b" unread"):
                consumed.append(chunk)
                yield chunk

    monkeypatch.setattr(pipeline, "Limits", lambda: Limits(max_source_bytes=10))
    identity = SourceIdentity(document_id="registry-7", version="3", uri="catalog://source/7", name="Note")
    out = tmp_path / "out"
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SourceStream()))) as client:
        with pytest.raises(PipelineError, match="source_too_large"):
            process_file(UrlInput(url="http://8.8.8.8/document.txt?private=key"), out,
                         identity=identity, client=client)
    assert consumed == [b"hello", b" world"]
    assert {item.name for item in out.iterdir()} == {"report.json"}
    report = read_report(out)
    assert report["source"] == {
        "name": "Note", "uri": "catalog://source/7", "document_id": "registry-7", "document_version": "3",
        "format": "txt", "sha256": None, "bytes": None,
    }


def test_fingerprint_golden_vector():
    body = "雪😀\r\n"
    assets = ["a" * 64, "b" * 64, "a" * 64]
    assert chunk_digest(body, ["绪论"], None, assets) == "4a4faea7d31926b3ba72003e7fd2490d4c58d047b1043dd29eac219fc7c5129d"
    assert chunk_digest(body, ["绪论"], None, assets[:2]) != chunk_digest(body, ["绪论"], None, assets)
    assert chunk_digest("雪😀\n", ["绪论"], None, assets) != chunk_digest(body, ["绪论"], None, assets)


def test_captured_source_and_explicit_assets_survive_mutation_and_relocation(tmp_path):
    source = tmp_path / "intake.md"
    source_bytes = "# Heading\n\n![first][drawing] ![second][drawing]\n\n[drawing]: sketch.png\n".encode()
    source.write_bytes(source_bytes)
    asset = tmp_path / "provided.png"
    Image.new("RGB", (4, 4), "red").save(asset)
    original_asset = asset.read_bytes()
    identity = SourceIdentity(document_id="catalog/42", version="8", uri="catalog://source/42/8", name="原件.md")

    def mutate(event):
        assert event.path == source
        if event.stage == "parse":
            source.unlink()
            Image.new("RGB", (4, 4), "blue").save(asset)

    out = tmp_path / "out"
    process_file(source, out, assets={"sketch.png": asset}, identity=identity, on_progress=mutate)
    root = out.rename(tmp_path / "moved")
    [row] = _read_chunks(root)
    assert row.citations.source_name == "原件.md"
    assert row.citations.source_uri == "catalog://source/42/8"
    assert row.citations.source_lines == [(3, 3)]
    assert row.metadata.document_id == "catalog/42"
    assert row.metadata.document_version == "8"
    assert row.metadata.source_sha256 == digest(source_bytes)
    assert row.metadata.chunk_sha256 == chunk_digest(row.body, row.heading_path, row.embedding_input,
                                                     [digest(original_asset)] * 2)
    [key] = row.assets
    assert (root / key).read_bytes() == original_asset



@pytest.mark.parametrize("alt", ["", "DIAGRAM"])
def test_inline_media_belongs_to_its_selected_text_fragment(tmp_path, alt):
    source = tmp_path / "article.md"
    source.write_text("Opening sentence. " * 20 + f"![{alt}](figure.png) " + "Closing sentence. " * 20)
    image = tmp_path / "figure.png"
    Image.new("RGB", (4, 4), "red").save(image)
    out = tmp_path / "out"
    process_file(source, out, Config(chunking={"target_input_bytes": 64, "max_input_bytes": 128}))
    rows = _read_chunks(out)
    assert len(rows) > 3
    [with_media] = [row for row in rows if row.assets]
    assert with_media.metadata.chunk_sha256 == chunk_digest(
        with_media.body, with_media.heading_path, with_media.embedding_input, [digest(image.read_bytes())],
    )
    if alt:
        assert alt in with_media.body
    else:
        assert with_media.id > 0 and with_media.id < rows[-1].id


def test_image_only_chunks_distinguish_the_media_bytes(tmp_path):
    path = tmp_path / "image.md"
    path.write_text("![](plot.png)")
    asset = tmp_path / "plot.png"
    rows = []
    for name, color in (("one", "red"), ("two", "blue")):
        Image.new("RGB", (4, 4), color).save(asset)
        output = tmp_path / name
        process_file(path, output)
        row = _read_chunks(output)[0]
        assert row.body == "" and row.embedding_input is None
        rows.append(row)
    assert rows[0].metadata.chunk_sha256 != rows[1].metadata.chunk_sha256


def test_chunk_fingerprint_ignores_location_and_ordinal(tmp_path):
    path = tmp_path / "chapter.md"
    path.write_text("# First\n\nSame body.\n")
    one = tmp_path / "one"
    process_file(path, one)
    first = _read_chunks(one)[0]
    path.write_text("# Earlier\n\nPrior body.\n\n# First\n\nSame body.\n")
    two = tmp_path / "two"
    process_file(path, two)
    second = _read_chunks(two)[1]
    assert first.id != second.id
    assert first.citations.source_lines != second.citations.source_lines
    assert first.metadata.chunk_sha256 == second.metadata.chunk_sha256


def test_repeat_and_embedding_profile_preserve_chunks_and_redact_credentials(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("Stable content.")
    process_file(source, tmp_path / "one")
    expected = read_output(tmp_path / "one")[1]
    batch = tmp_path / "batch"
    process_files([InputFile(path=source)], batch)
    assert read_output(batch / "0001")[1] == expected

    def handle(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json={"model": payload["model"], "data": [{"index": 0, "embedding": [1, 2]}]})

    for model in ("alpha", "beta"):
        config = Config(embedding={"model": model, "api_base_url": "https://provider.test", "api_key": "hidden-key"})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            root = tmp_path / model
            process_file(source, root, config, client=client)
        assert read_output(root)[1] == expected
        report, [chunk], [vector] = read_output(root)
        assert vector["profile"]["model"] == model
        assert "artifact_id" not in vector
        assert vector["input_sha256"] == digest(chunk["embedding_input"].encode())
        assert "hidden-key" not in str(report)
        assert b"https://provider.test" not in (root / "embeddings.jsonl").read_bytes()


def test_content_and_source_lines_share_only_cr_lf_boundaries(tmp_path):
    original = "\ufeffA\u2028B\u0085C\x0cD\rE\r\nF\nG".encode()
    source = tmp_path / "line.txt"
    source.write_bytes(original)
    root = tmp_path / "out"
    process_file(source, root)
    [row] = _read_chunks(root)
    assert row.citations.source_lines == [(1, 4)]
    assert (root / "content.md").read_text() == "A\u2028B\u0085C\x0cD\nE\nF\nG\n\n"
    assert row.metadata.source_sha256 == digest(original)


def test_tex_machine_anchors_are_separate_from_display_labels(tmp_path):
    source = tmp_path / "sample.tex"
    source.write_text(r"""
\begin{document}
\section{Proof}\label{sec:proof}

\begin{equation}
x=2\label{eq:double}\tag{A}
\end{equation}

\begin{table}
\caption{Table 3 Measurements}\label{tab:measurements}
\begin{tabular}{ll}
A & B \\
\end{tabular}
\end{table}

\begin{figure}
\includegraphics{figure.png}
\caption{Figure 4 Drawing}\label{fig:drawing}
\end{figure}
\end{document}
""")
    Image.new("RGB", (2, 2)).save(tmp_path / "figure.png")
    output = tmp_path / "out"
    process_file(source, output)
    objects = {item.kind: item for row in _read_chunks(output) for item in row.citations.objects}
    assert objects["equation"].labels == ["(A)"]
    assert objects["equation"].anchors == ["eq:double"]
    assert objects["table"].labels == ["Table 3"]
    assert objects["table"].anchors == ["tab:measurements"]
    assert objects["figure"].labels == ["Figure 4"]
    assert objects["figure"].anchors == ["fig:drawing"]


def test_split_table_hash_includes_only_media_in_selected_rows(tmp_path):
    source = tmp_path / "table.md"
    first, second = tmp_path / "one.png", tmp_path / "two.png"
    Image.new("RGB", (2, 2), "red").save(first)
    Image.new("RGB", (2, 2), "blue").save(second)
    source.write_text(
        "| Header ![decor](two.png) | Value |\n| --- | --- |\n"
        + f"| ![](one.png) | {'A' * 80} |\n"
        + f"| ![](two.png) ![](two.png) | {'B' * 80} |\n"
        + f"| Text | {'C' * 80} |\n"
    )
    output = tmp_path / "out"
    process_file(source, output, Config(chunking={"target_input_bytes": 64, "max_input_bytes": 128}))
    rows = _read_chunks(output)
    assert len(rows) == 3
    assert [row.citations.objects[0].part.table_rows for row in rows] == [(1, 2), (2, 3), (3, 4)]
    assert [len(row.assets) for row in rows] == [1, 1, 0]
    for row, media in zip(rows, ([first], [second, second], []), strict=True):
        assert row.metadata.chunk_sha256 == chunk_digest(
            row.body, row.heading_path, row.embedding_input, [digest(item.read_bytes()) for item in media],
        )


@pytest.mark.parametrize("media", [False, True])
def test_exact_document_outputs_and_json_field_order(tmp_path, media):
    source = tmp_path / "input.md"
    source.write_text("# Heading\n\nBody." + (" ![plot](plot.png)" if media else ""))
    if media:
        Image.new("RGB", (2, 2), "red").save(tmp_path / "plot.png")
    output = tmp_path / "out"
    process_file(source, output)
    report, rows, _ = read_output(output)
    expected = {"content.md", "chunks.jsonl", "embeddings.jsonl", "report.json"}
    if media:
        expected.add("assets")
    assert {entry.name for entry in output.iterdir()} == expected
    referenced = {key for row in rows for key in row["assets"]}
    assert {entry.relative_to(output).as_posix() for entry in output.rglob("*") if entry.is_file()} == (
        expected - {"assets"} | referenced
    )
    assert "artifact_id" not in report
    for row in rows:
        assert list(row) == ["id", "body", "heading_path", "embedding_input", "assets", "citations", "metadata"]
        assert list(row["citations"]) == [
            "source_name", "source_uri", "pages", "slides", "sheets", "source_lines", "objects",
            "content_ranges", "location_status",
        ]
        assert list(row["metadata"]) == [
            "document_id", "document_version", "source_sha256", "chunk_sha256", "source_type", "content_status",
        ]
