import errno
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import httpx
import pytest
from PIL import Image

from doc_pipeline import (
    Artifact,
    ArtifactError,
    Config,
    InputFile,
    PipelineError,
    SourceIdentity,
    process_file,
    process_files,
)
from doc_pipeline.artifacts import store
from doc_pipeline.contracts.artifact import canonical, chunk_digest, digest, jsonl
from tests.support import read_output


def test_fingerprint_golden_vector():
    # SHA-256 of the sorted UTF-8 object, including both occurrences of the same media.
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
    report = process_file(source, out, assets={"sketch.png": asset}, identity=identity, on_progress=mutate)
    root = out.rename(tmp_path / "moved")
    artifact = Artifact.open(root, expected_id=report["artifact_id"])
    assert artifact.source() == source_bytes
    assert artifact.manifest.document_id_origin == "supplied"
    assert artifact.manifest.dependencies["sketch.png"].sha256 == digest(original_asset)
    row = artifact.chunk(0)
    assert row.citations.source_name == "原件.md"
    assert row.citations.source_uri == "catalog://source/42/8"
    assert row.citations.source_lines == [(3, 3)]
    assert row.metadata.document_id == "catalog/42"
    assert row.metadata.document_version == "8"
    assert row.metadata.source_sha256 == digest(source_bytes)
    assert row.metadata.chunk_sha256 == chunk_digest(row.body, row.heading_path, row.embedding_input,
                                                     [digest(original_asset)] * 2)
    [key] = row.assets
    assert artifact.asset(key) == original_asset
    selection, blocks = artifact.evidence(0)
    [body] = [item for item in selection.selections if item.role == "body"]
    [block] = [item for item in blocks if item.block.id == body.block_id]
    assert len(body.locations) == 2
    assert [use.asset.path for use in block.resource_uses] == [key, key]
    assert row.body == block.canonical_text[body.block_text_range.start:body.block_text_range.end]
    with pytest.raises(ArtifactError, match="identity"):
        Artifact.open(root, expected_id="0" * 64)
    with pytest.raises(KeyError):
        artifact.asset("../input")


@pytest.mark.parametrize("alt", ["", "DIAGRAM"])
def test_inline_media_belongs_to_its_selected_text_fragment(tmp_path, alt):
    source = tmp_path / "article.md"
    source.write_text("Opening sentence. " * 20 + f"![{alt}](figure.png) " + "Closing sentence. " * 20)
    image = tmp_path / "figure.png"
    Image.new("RGB", (4, 4), "red").save(image)
    out = tmp_path / "out"
    process_file(source, out, Config(chunking={"target_input_bytes": 64, "max_input_bytes": 128}))
    artifact = Artifact.open(out)
    rows = [artifact.chunk(i) for i in range(artifact.manifest.chunk_count)]
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
        row = Artifact.open(output).chunk(0)
        assert row.body == "" and row.embedding_input is None
        rows.append(row)
    assert rows[0].metadata.chunk_sha256 != rows[1].metadata.chunk_sha256


def test_chunk_fingerprint_ignores_location_and_ordinal(tmp_path):
    path = tmp_path / "chapter.md"
    path.write_text("# First\n\nSame body.\n")
    one = tmp_path / "one"
    process_file(path, one)
    first = Artifact.open(one).chunk(0)
    path.write_text("# Earlier\n\nPrior body.\n\n# First\n\nSame body.\n")
    two = tmp_path / "two"
    process_file(path, two)
    second = Artifact.open(two).chunk(1)
    assert first.id != second.id
    assert first.citations.source_lines != second.citations.source_lines
    assert first.artifact_id != second.artifact_id
    assert first.metadata.chunk_sha256 == second.metadata.chunk_sha256


def test_repeat_and_embedding_profile_do_not_change_artifact_identity(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("Stable content.")
    expected = process_file(source, tmp_path / "one")["artifact_id"]
    batch = tmp_path / "batch"
    process_files([InputFile(path=source)], batch)
    assert Artifact.open(batch / "0001").manifest.artifact_id == expected

    def handle(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json={"model": payload["model"], "data": [{"index": 0, "embedding": [1, 2]}]})

    for model in ("alpha", "beta"):
        config = Config(embedding={"model": model, "api_base_url": "https://provider.test", "api_key": "hidden-key"})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            root = tmp_path / model
            process_file(source, root, config, client=client)
        assert Artifact.open(root).manifest.artifact_id == expected
        report, [chunk], [vector] = read_output(root)
        assert vector["profile"]["model"] == model and vector["artifact_id"] == expected
        assert vector["input_sha256"] == digest(chunk["embedding_input"].encode())
        assert "hidden-key" not in str(report)
        assert b"https://provider.test" not in (root / "embeddings.jsonl").read_bytes()


def test_embedding_failure_leaves_verified_artifact(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("Keep the parsed text.")
    root = tmp_path / "out"
    config = Config(embedding={"model": "alpha", "api_base_url": "https://provider.test"})

    def fail_embedding(event):
        if event.stage == "embedding":
            raise OSError("injected failure after publication")

    with pytest.raises(OSError, match="after publication"):
        process_file(source, root, config, on_progress=fail_embedding)
    artifact = Artifact.open(root)
    assert artifact.chunk(0).body == "Keep the parsed text."


@pytest.fixture
def bundle(tmp_path):
    path = tmp_path / "guide.md"
    path.write_text("Read this with ![a](a.png).")
    image = tmp_path / "a.png"
    Image.new("RGB", (3, 3), "red").save(image)
    root = tmp_path / "out"
    process_file(path, root, assets={"a.png": image})
    return root


@pytest.mark.parametrize("file", ["document.json", "chunks.jsonl", "content.md", "blocks.jsonl",
                                  "provenance.jsonl", "source/original.md", "source/dependencies/0000", "asset"])
def test_open_rejects_corruption_in_every_local_file_kind(bundle, file):
    if file == "asset":
        file = Artifact.open(bundle).chunk(0).assets[0]
    path = bundle / file
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(ArtifactError):
        Artifact.open(bundle)


@pytest.mark.parametrize("field,value", [("artifact_id", "f" * 64), ("id", 42)])
def test_open_rejects_mismatched_chunk_identity(bundle, field, value):
    row = json.loads((bundle / "chunks.jsonl").read_bytes())
    row[field] = value
    (bundle / "chunks.jsonl").write_bytes(jsonl([row]))
    with pytest.raises(ArtifactError, match="chunk identity"):
        Artifact.open(bundle)


def test_open_rejects_missing_and_escaping_paths_and_reads_verify_again(bundle, tmp_path):
    artifact = Artifact.open(bundle)
    [key] = artifact.chunk(0).assets
    media = bundle / key
    external = tmp_path / "outside.png"
    media.rename(external)
    with pytest.raises(ArtifactError, match="cannot read"):
        artifact.asset(key)
    media.symlink_to(external)
    with pytest.raises(ArtifactError, match="unexpected file"):
        Artifact.open(bundle)
    with pytest.raises(ArtifactError, match="cannot read"):
        artifact.asset(key)


def test_open_rejects_rehashed_evidence_with_wrong_block_reference(bundle):
    root = bundle
    path = root / "provenance.jsonl"
    row = json.loads(path.read_bytes())
    row["selections"][0]["block_id"] = "absent"
    path.write_bytes(jsonl([row]))
    manifest = json.loads((root / "document.json").read_bytes())
    manifest.pop("artifact_id")
    manifest["files"][path.name] = digest(path.read_bytes())
    artifact_id = digest(canonical(manifest))
    (root / "document.json").write_bytes(canonical({**manifest, "artifact_id": artifact_id}) + b"\n")
    row = json.loads((root / "chunks.jsonl").read_bytes())
    (root / "chunks.jsonl").write_bytes(jsonl([{**row, "artifact_id": artifact_id}]))
    with pytest.raises(ArtifactError, match="reference"):
        Artifact.open(root)


def test_context_and_source_lines_share_only_cr_lf_boundaries(tmp_path):
    original = "\ufeffA\u2028B\u0085C\x0cD\rE\r\nF\nG".encode()
    source = tmp_path / "line.txt"
    source.write_bytes(original)
    root = tmp_path / "out"
    process_file(source, root)
    artifact = Artifact.open(root)
    row = artifact.chunk(0)
    assert row.citations.source_lines == [(1, 4)]
    assert artifact.context(0) == ["A\u2028B\u0085C\x0cD\nE\nF\nG\n"]
    assert artifact.context(0, before=100, after=100) == [artifact.content()]
    assert artifact.source() == original
    assert artifact.neighbors(0) == []
    with pytest.raises(IndexError):
        artifact.chunk(-1)


@pytest.mark.parametrize("failure", [False, True])
def test_mount_fallback_publishes_manifest_last(tmp_path, monkeypatch, failure):
    path = tmp_path / "guide.md"
    path.write_text("Body.")
    root = tmp_path / "out"
    root.mkdir()
    original_rename = Path.rename
    original_move = store.shutil.move

    def rename(source, target):
        if target == root:
            raise OSError(errno.EBUSY, "mounted")
        return original_rename(source, target)

    def move(source, target):
        with pytest.raises(ArtifactError):
            Artifact.open(root)
        if failure and source.name == "content.md":
            raise OSError("injected move failure")
        return original_move(source, target)

    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(store.shutil, "move", move)
    if failure:
        with pytest.raises(OSError, match="injected move failure"):
            process_file(path, root)
        assert list(root.iterdir()) == []
        with pytest.raises(ArtifactError):
            Artifact.open(root)
    else:
        process_file(path, root)
        assert Artifact.open(root).source() == path.read_bytes()


def test_concurrent_publishers_cannot_share_destination(tmp_path):
    path = tmp_path / "guide.md"
    path.write_text("Body.")
    root = tmp_path / "out"
    entered, release = Event(), Event()

    def pause(event):
        if event.stage == "parse":
            entered.set()
            assert release.wait(5)

    with ThreadPoolExecutor() as pool:
        first = pool.submit(process_file, path, root, on_progress=pause)
        try:
            assert entered.wait(5)
            with pytest.raises(ArtifactError):
                Artifact.open(root)
            with pytest.raises(PipelineError, match="output_in_use"):
                process_file(path, root)
        finally:
            release.set()
        first.result()
    assert Artifact.open(root).source() == path.read_bytes()


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
    artifact = Artifact.open(output)
    objects = {item.kind: item for i in range(artifact.manifest.chunk_count)
               for item in artifact.chunk(i).citations.objects}
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
    artifact = Artifact.open(output)
    rows = [artifact.chunk(i) for i in range(3)]
    assert [row.citations.objects[0].part.table_rows for row in rows] == [(1, 2), (2, 3), (3, 4)]
    assert [len(row.assets) for row in rows] == [1, 1, 0]
    for row, media in zip(rows, ([first], [second, second], []), strict=True):
        assert row.metadata.chunk_sha256 == chunk_digest(
            row.body, row.heading_path, row.embedding_input, [digest(item.read_bytes()) for item in media],
        )
