# Multimodal Doc Pipeline

[English](README.md) · [简体中文](README.zh-CN.md)

Turn documents into Markdown, structured chunks, and source citations for retrieval pipelines. Add embeddings with an OpenAI-compatible service. Run through the CLI, Python API, or Docker.

Text formats run locally without an API key. PDF, Office, and image files are uploaded to [MinerU](https://github.com/opendatalab/MinerU) and require a MinerU API key.

[Quick start](#quick-start) · [Get the Docker image](#get-the-docker-image) · [Configuration](#configuration) · [Outputs](#outputs) · [Documentation](#documentation)

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/). Clone the repository and install its dependencies. For Docker, start with [Get the Docker image](#get-the-docker-image).

In every example, replace `path/to/document` with a real file path, including its extension. Use an absent or empty output directory for each run.

```sh
git clone https://github.com/ShaoxinLi/Multimodal-Doc-Pipeline.git
cd Multimodal-Doc-Pipeline
uv sync --frozen
uv run doc-pipeline path/to/document --out path/to/output
```

For a first run without credentials, use a [supported text format](#supported-formats). To process a PDF, Office file, or image, [set your MinerU key](#configure-mineru) first.

The command prints a JSON report and writes `content.md`, `chunks.jsonl`, `embeddings.jsonl`, and `report.json` to the output directory. Embeddings are disabled by default, so `embeddings.jsonl` is empty.

Inspect the result:

```sh
cat path/to/output/content.md
cat path/to/output/report.json
```

Check `parse_status` and `embedding_errors`. Exit code `0` can still mean partial parsing.

## Get the Docker image

Requires Docker. Download the published image and check the CLI:

```sh
docker pull ghcr.io/shaoxinli/multimodal-doc-pipeline:latest
docker run --rm ghcr.io/shaoxinli/multimodal-doc-pipeline:latest --help
```

The image includes Python and the project's dependencies. You do not need a local Python installation to use it.

To build from source instead, run these commands from the project root:

```sh
docker build -t doc-pipeline:local .
docker run --rm doc-pipeline:local --help
```

For a local build, use `doc-pipeline:local` in place of the published image name in the commands below.

The [image workflow](.github/workflows/image.yml) tests each image before publishing it. `latest` tracks the default branch. Published builds also have `sha-<short>` tags, and Git version tags such as `v1.2.3` produce image tags such as `1.2.3`.

### Process a file with Docker

The shell examples use POSIX syntax. Replace `/absolute/path/to/documents` with the directory containing your input. Replace `/in/document` with that file's container path, including its extension.

```sh
mkdir -p path/to/output
docker run --rm --user "$(id -u):$(id -g)" \
	-v "/absolute/path/to/documents:/in:ro" \
	-v "$PWD/path/to/output:/out" \
	ghcr.io/shaoxinli/multimodal-doc-pipeline:latest /in/document --out /out
```

The input mount is read-only. The output directory must be empty and writable by your user. To pass API keys and a configuration file, use the [complete Docker configuration examples](#configure-docker).

To show progress and a summary in Docker, add `--progress --format text` after the input path. See [CLI output](docs/output-format.md#status-and-failure-semantics) for format defaults.

## Supported formats

| Input | Extensions | Processing |
| --- | --- | --- |
| Text | `.md`, `.markdown`, `.txt`, `.rst`, `.tex`, `.latex` | Local, no API key |
| PDF | `.pdf` | MinerU |
| Office | `.doc`, `.docx`, `.ppt`, `.pptx`, `.xls`, `.xlsx` | MinerU |
| Images | `.png`, `.jpg`, `.jpeg`, `.jp2`, `.webp`, `.gif`, `.bmp` | MinerU |

Inputs are limited to 200 MB per file and 200 pages per PDF. HTML and encrypted PDFs are unsupported.

## Configuration

Default settings are enough for local text processing. To change MinerU options, chunk sizes, concurrency, or timeouts, copy [config.example.toml](config.example.toml):

```sh
cp config.example.toml config.toml
```

Edit the settings you need, then pass the file explicitly:

```sh
uv run doc-pipeline path/to/document \
	--out path/to/output \
	--config config.toml
```

The application does not discover `config.toml` or load `.env` automatically. Nonempty environment keys override keys in the configuration file. Chunk sizes use UTF-8 bytes, not tokens.

### Configure MinerU

Set your key before running the CLI. A configuration file is optional when using the default MinerU settings:

```sh
export MINERU_API_KEY='your-mineru-api-key'
uv run doc-pipeline path/to/document --out path/to/output
```

You can also set `api_key` in the `[mineru]` section of `config.toml`.

### Enable embeddings

Uncomment the `[embedding]` section in `config.toml` and replace the model and URL with your service's values:

```toml
[embedding]
model = "your-embedding-model"
api_base_url = "https://your-provider.example/v1"
```

The client appends `/embeddings` to this URL and sends chunk text to the service. Set the key and run with the configuration file:

```sh
export EMBEDDING_API_KEY='your-embedding-api-key'
uv run doc-pipeline path/to/document \
	--out path/to/output \
	--config config.toml
```

An environment key alone does not enable embeddings. The `[embedding]` section is required. Set `dimensions` only if your service supports that parameter.

### Configure Docker

First create `config.toml` as shown above. To pass keys from a file, copy [.env.example](.env.example) and fill in the keys you need:

```sh
cp .env.example .env
```

Run with both files:

```sh
mkdir -p path/to/output
docker run --rm --user "$(id -u):$(id -g)" \
	--env-file .env \
	-v "/absolute/path/to/documents:/in:ro" \
	-v "$PWD/path/to/output:/out" \
	-v "$PWD/config.toml:/config/config.toml:ro" \
	ghcr.io/shaoxinli/multimodal-doc-pipeline:latest /in/document --out /out \
	--config /config/config.toml
```

To pass keys already exported in your shell, use this command instead:

```sh
mkdir -p path/to/output
docker run --rm --user "$(id -u):$(id -g)" \
	-e MINERU_API_KEY \
	-e EMBEDDING_API_KEY \
	-v "/absolute/path/to/documents:/in:ro" \
	-v "$PWD/path/to/output:/out" \
	-v "$PWD/config.toml:/config/config.toml:ro" \
	ghcr.io/shaoxinli/multimodal-doc-pipeline:latest /in/document --out /out \
	--config /config/config.toml
```

## Process multiple files

Pass each input file to the CLI:

```sh
uv run doc-pipeline path/to/document path/to/another-document \
	--out path/to/batch-output
```

Results go into numbered directories such as `0001/` and `0002/`. The root `run.json` records each file's result. A file failure does not prevent other files from producing results.

## Use the Python API

Run Python in the project environment with `uv run python`. For a single file:

```python
from pathlib import Path
from doc_pipeline import process_file

report = process_file(Path("path/to/document"), Path("path/to/output"))
print(report["parse_status"])
```

For a batch with explicit configuration:

```python
from pathlib import Path
from doc_pipeline import Config, InputFile, process_files

config = Config.from_toml(Path("config.toml"))
report = process_files(
	[InputFile(path=Path("path/to/document")), InputFile(path=Path("path/to/another-document"))],
	Path("path/to/batch-output"),
	config,
)
print(report["status"])
```

`process_file` accepts the same `config` object as its third argument.

Pass `on_progress` to either function to receive a `ProgressEvent` with the file and current stage. Batch callbacks can run on worker threads. Synchronize writes to shared state.

```python
report = process_file(
	Path("path/to/document"),
	Path("path/to/output"),
	on_progress=lambda event: print(event.path.name, event.stage),
)
```

## Outputs

| File | Contents |
| --- | --- |
| `content.md` | Parsed document in Markdown |
| `chunks.jsonl` | Chunks with section context, metadata, and source citations |
| `embeddings.jsonl` | Successful embedding vectors, empty when embeddings are disabled |
| `report.json` | Parse status, counts, diagnostics, embedding errors, and timings |
| `assets/` | Asset snapshots, when present |
| `run.json` | Per-file results and report paths for batch runs |

Citations refer to the original file. Page coordinates appear only when verified. See the [output format reference](docs/output-format.md) for field definitions and citation precision.

### Check a failed or partial run

| Symptom | What to check |
| --- | --- |
| `output_not_empty` | Choose a new or empty directory, including when retrying. |
| `missing_mineru_credentials` | Export `MINERU_API_KEY`. In Docker, pass it with `-e` or `--env-file`. |
| No embeddings appear | Check the `[embedding]` settings and `embedding_errors` in `report.json`. |
| Parsing is partial | Read `diagnostics` and inspect `content.md` before using the chunks. |

Exit codes are `0` for completed processing, `2` for invalid CLI arguments, `65` for a non-retryable failure, `75` for a retryable failure or any embedding error, and `130` for interruption. See [failure semantics](docs/output-format.md#status-and-failure-semantics) for details.

## Development

Install dependencies and run the checks from the project root:

```sh
uv sync --frozen
uv run pytest -q
uv run ruff check src tests
```

Tests do not call live paid services. Source code is in [src/doc_pipeline](src/doc_pipeline), with tests in [tests](tests).

## Documentation

- [Architecture](docs/architecture.md) explains parsing, chunking, citations, and module ownership.
- [Output format](docs/output-format.md) defines the files, report fields, and failure behavior.
- [Configuration example](config.example.toml) lists settings for MinerU, embeddings, chunking, concurrency, and timeouts.
