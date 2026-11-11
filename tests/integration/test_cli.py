import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest
from PIL import Image

from tests.support import PROJECT_ROOT, document_archive, pdf_bytes, read_output, write_source_pdf


def cli_command(*arguments):
    return [sys.executable, "-m", "doc_pipeline", *map(str, arguments)]


def run_cli(*arguments):
    return subprocess.run(
        cli_command(*arguments), cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=15
    )


def test_cli_accepts_file_and_existing_empty_mount_directory(tmp_path):
    source = tmp_path / "input.md"
    original = "# Guide\n\n文件处理说明。\n\n![plot](plot.png)\n"
    source.write_text(original, encoding="utf-8")
    asset = tmp_path / "plot.png"
    Image.new("RGB", (4, 4), "blue").save(asset)
    output = tmp_path / "out"
    output.mkdir()

    result = run_cli(source, "--out", output, "--asset", f"plot.png={asset}")

    assert result.returncode == 0, result.stderr
    report, chunks, vectors = read_output(output)
    assert json.loads(result.stdout) == report
    assert report["source"]["path"] == str(source.resolve())
    assert report["chunk_count"] == len(chunks) > 0
    assert report["embedding"] is None
    assert vectors == report["embedding_errors"] == []
    markdown = (output / "content.md").read_text(encoding="utf-8")
    assert "文件处理说明。" in markdown
    [exported_asset] = (output / "assets").iterdir()
    assert exported_asset.read_bytes() == asset.read_bytes()
    assert exported_asset.relative_to(output).as_posix() in markdown
    assert {path.name for path in output.iterdir()} == {
        "content.md",
        "chunks.jsonl",
        "embeddings.jsonl",
        "report.json",
        "assets",
    }
    assert source.read_text(encoding="utf-8") == original


def test_existing_outputs_are_not_overwritten(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("Body.")
    output = tmp_path / "out"
    output.mkdir()
    marker = output / "report.json"
    marker.write_text("existing caller file")

    result = run_cli(source, "--out", output)

    assert result.returncode == 65
    assert json.loads(result.stdout)["error"] == "output_not_empty"
    assert marker.read_text() == "existing caller file"
    assert list(output.iterdir()) == [marker]


def test_embedding_failure_keeps_content_and_returns_nonzero(tmp_path, monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    source = tmp_path / "input.txt"
    source.write_text("Body")
    config = tmp_path / "config.toml"
    config.write_text('[embedding]\napi_base_url="https://embed.test/v1"\nmodel="test"\ndimensions=2\n')
    output = tmp_path / "out"

    result = run_cli(source, "--out", output, "--config", config)

    assert result.returncode == 75
    report, [chunk], vectors = read_output(output)
    assert json.loads(result.stdout) == report
    assert (output / "content.md").read_text().strip() == "Body"
    assert chunk["body"] == "Body"
    assert vectors == []
    assert report["embedding_errors"] == [
        {"chunk_id": chunk["id"], "code": "missing_embedding_credentials", "retryable": False},
    ]


def test_invalid_input_does_not_write_outputs(tmp_path):
    source = tmp_path / "input.html"
    source.write_text("<html>hello</html>")
    output = tmp_path / "out"

    result = run_cli(source, "--out", output)

    assert result.returncode == 65
    assert json.loads(result.stdout)["error"] == "unsupported_format"
    assert not output.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows subprocess termination does not deliver SIGTERM")
def test_sigterm_interrupts_cli_while_provider_request_is_blocked(tmp_path, monkeypatch):
    source_pdf = write_source_pdf(tmp_path)
    entered = Event()
    release = Event()

    class BlockingProvider(BaseHTTPRequestHandler):
        def do_POST(self):
            entered.set()
            release.wait(timeout=15)

    monkeypatch.setenv("MINERU_API_KEY", "local-test-key")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    output = tmp_path / "out"
    config = tmp_path / "config.toml"
    with ThreadingHTTPServer(("127.0.0.1", 0), BlockingProvider) as server:
        config.write_text(f'[mineru]\napi_base_url="http://127.0.0.1:{server.server_port}"\n')
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            command = cli_command(source_pdf, "--out", output, "--config", config)
            with subprocess.Popen(
                command, cwd=PROJECT_ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            ) as process:
                try:
                    assert entered.wait(timeout=10), "CLI never reached the local provider"
                    process.terminate()
                    stdout, stderr = process.communicate(timeout=5)
                finally:
                    if process.poll() is None:
                        process.kill()
                assert process.returncode == 130, stderr
        finally:
            release.set()
            server.shutdown()
            thread.join()

    assert json.loads(stdout) == {"error": "interrupted"}
    assert not (output / "report.json").exists()


@pytest.mark.parametrize("supplied_asset", [False, True], ids=["source", "asset"])
def test_oversized_input_is_rejected_before_creating_output(tmp_path, supplied_asset):
    large = tmp_path / "large.txt"
    with large.open("wb") as stream:
        stream.truncate(200_000_001)
    source = large
    arguments = []
    if supplied_asset:
        source = tmp_path / "input.md"
        source.write_text("![plot](plot.png)")
        arguments = ["--asset", f"plot.png={large}"]
    output = tmp_path / "out"
    result = run_cli(source, "--out", output, *arguments)
    assert result.returncode == 65
    assert json.loads(result.stdout)["error"] == ("asset_too_large" if supplied_asset else "source_too_large")
    assert not output.exists()


def test_image_only_input_retains_asset_and_text_citation(tmp_path):
    source = tmp_path / "input.md"
    source.write_text("![](plot.png) ![](plot.png)")
    asset = tmp_path / "plot.png"
    Image.new("RGB", (4, 4), "blue").save(asset)
    output = tmp_path / "out"
    result = run_cli(source, "--out", output, "--asset", f"plot.png={asset}")
    assert result.returncode == 0, result.stderr
    _, [chunk], vectors = read_output(output)
    assert chunk["embedding_input"] is None
    assert vectors == []
    [citation] = chunk["citations"]
    assert citation["locations"][0]["path"] == str(source.resolve())
    [relative] = citation["assets"]
    assert (output / relative).read_bytes() == asset.read_bytes()
    assert relative in (output / "content.md").read_text()


def test_cli_and_python_read_the_same_configuration(tmp_path):
    from doc_pipeline import Config, process_file

    source = tmp_path / "input.md"
    source.write_text("# Guide\n\n" + "中文长文本测试。" * 80)
    config_path = tmp_path / "config.toml"
    config_path.write_text("[chunking]\ntarget_input_bytes=80\nmax_input_bytes=120\n")
    cli_output, python_output = tmp_path / "cli", tmp_path / "python"
    result = run_cli(source, "--out", cli_output, "--config", config_path)
    assert result.returncode == 0, result.stdout
    process_file(source, python_output, Config.from_toml(config_path))
    report, chunks, vectors = read_output(cli_output)
    python_report, python_chunks, python_vectors = read_output(python_output)
    assert (chunks, vectors) == (python_chunks, python_vectors)
    volatile = {"run_id", "started_at", "finished_at", "elapsed_seconds", "stage_seconds"}
    assert {key: value for key, value in report["execution"].items() if key not in volatile} == {
        key: value for key, value in python_report["execution"].items() if key not in volatile
    }
    assert {key: value for key, value in report.items() if key != "execution"} == {
        key: value for key, value in python_report.items() if key != "execution"
    }
    assert report["chunking"] == {"target_input_bytes": 80, "max_input_bytes": 120}
    assert len(chunks) > 1
    assert all(len(chunk["embedding_input"].encode("utf-8")) <= 120 for chunk in chunks)


@pytest.mark.parametrize(("settings", "field"), [
    ('request_timeout_seconds=0\n[mineru]\napi_key="private-key"\n', "request_timeout_seconds"),
    ('[mineru]\napi_key="private-key"\ntimeout_seconds=0\n', "mineru.timeout_seconds"),
])
def test_invalid_configuration_names_field_without_printing_credentials(tmp_path, settings, field):
    config = tmp_path / "config.toml"
    config.write_text(settings)
    source = tmp_path / "input.md"
    source.write_text("Hello")
    output = tmp_path / "out"
    result = run_cli(source, "--out", output, "--config", config)
    assert result.returncode == 65
    assert json.loads(result.stdout)["details"][0]["field"] == field
    assert "private-key" not in result.stdout + result.stderr
    assert not output.exists()


@pytest.mark.parametrize(("timeout", "bad_input", "provider_failure", "code"), [
    (False, False, None, 0),
    (True, False, None, 75),
    (True, True, None, 65),
    (False, False, (-60010, "mineru_processing_failed", True), 75),
    (False, False, ("-60018", "mineru_daily_quota_exceeded", False), 65),
    (False, True, (-60010, "mineru_processing_failed", True), 65),
])
def test_cli_mixed_batch_exit_codes_and_signed_urls(
    tmp_path, monkeypatch, timeout, bad_input, provider_failure, code,
):
    source_pdf = write_source_pdf(tmp_path)
    monkeypatch.delenv("MINERU_API_KEY", raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    archive = document_archive(pdf_bytes())
    local = tmp_path / "note.txt"
    local.write_text("Local document.")
    output = tmp_path / "out"
    config = tmp_path / "config.toml"
    data_ids = []
    uploads = []
    requests = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send_bytes(self, body, status=200):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def provider_response(self, **data):
            self.send_bytes(json.dumps({"code": 0, "data": data}).encode())

        def do_POST(self):
            requests.append(("POST", self.path))
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            data_ids.extend(item["data_id"] for item in payload["files"])
            self.provider_response(batch_id="batch", file_urls=[base + "/put?signature=upload-secret"])

        def do_PUT(self):
            requests.append(("PUT", self.path))
            uploads.append(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_bytes(b"")

        def do_GET(self):
            requests.append(("GET", self.path))
            if self.path == "/api/v4/extract-results/batch/batch":
                if provider_failure is not None:
                    self.send_bytes(json.dumps({
                        "code": provider_failure[0], "msg": "provider-secret", "trace_id": "trace-secret",
                    }).encode())
                else:
                    self.provider_response(extract_result=[] if timeout else [{
                        "data_id": data_ids[0], "state": "done",
                        "full_zip_url": base + "/zip?signature=download-secret",
                    }])
            else:
                self.send_bytes(archive)

    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        base = f"http://127.0.0.1:{server.server_port}"
        deadline = 0.5 if timeout else 5
        config.write_text(f'[mineru]\napi_base_url="{base}"\napi_key="cli-secret"\ntimeout_seconds={deadline}\n')
        background = Thread(target=server.serve_forever, daemon=True)
        background.start()
        try:
            inputs = [source_pdf, local]
            if bad_input:
                inputs.append(tmp_path / "bad.html")
            result = run_cli(*inputs, "--out", output, "--config", config)
        finally:
            server.shutdown()
            background.join()

    assert result.returncode == code, result.stderr
    assert uploads == [source_pdf.read_bytes()]
    run = json.loads(result.stdout)
    run_json = (output / "run.json").read_text()
    assert run == json.loads(run_json)
    assert run["files"][0]["status"] == ("failed" if timeout or provider_failure else "complete")
    assert run["files"][1]["status"] == "complete"
    if bad_input:
        assert run["files"][2]["error"]["retryable"] is False
    if provider_failure is not None:
        _, tag, retryable = provider_failure
        error = {"stage": "remote_acquisition", "code": tag, "retryable": retryable}
        assert run["files"][0]["error"] == error
        events = [json.loads(line.removeprefix("document_finished ")) for line in result.stderr.splitlines()
                  if line.startswith("document_finished ")]
        assert [event["error"] for event in events if event["index"] == 0] == [error]
        assert requests == [("POST", "/api/v4/file-urls/batch"), ("PUT", "/put?signature=upload-secret"),
                            ("GET", "/api/v4/extract-results/batch/batch")]
    assert "document_finished" in result.stderr
    assert all(private not in result.stdout + result.stderr + run_json for private in (
        "cli-secret", "upload-secret", "download-secret", "provider-secret", "trace-secret", "Local document.",
    ))


def test_cli_rejects_assets_for_multiple_inputs(tmp_path):
    result = run_cli("one.md", "two.md", "--out", tmp_path / "out", "--asset", "a.png=local.png")
    assert result.returncode == 2
    assert "--asset requires exactly one input" in result.stderr
    assert not (tmp_path / "out").exists()


def test_cli_batch_keeps_parse_partial_distinct_from_embedding_errors(tmp_path, monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    partial, complete = tmp_path / "partial.md", tmp_path / "complete.txt"
    partial.write_text("<div>Opaque content.</div>\n")
    complete.write_text("Local document.")
    config = tmp_path / "config.toml"
    config.write_text('[embedding]\napi_base_url="https://embed.test/v1"\nmodel="example"\n')

    result = run_cli(partial, complete, "--out", tmp_path / "without-embedding")
    assert result.returncode == 0
    run = json.loads(result.stdout)
    assert [(item["status"], item["parse_status"]) for item in run["files"]] == [
        ("partial", "partial"), ("complete", "complete"),
    ]
    result = run_cli(partial, complete, "--out", tmp_path / "with-embedding", "--config", config)
    assert result.returncode == 75
    run = json.loads(result.stdout)
    assert [(item["status"], item["parse_status"]) for item in run["files"]] == [
        ("partial", "partial"), ("partial", "complete"),
    ]
    assert run["embedding_error_count"] == 1
