# Output format

`process_file`, `process_files`, and the CLI write Markdown, chunks, optional vectors, and a processing report.

## Output files

| Path | Contents |
| --- | --- |
| `content.md` | Readable document rendering. |
| `chunks.jsonl` | One compact chunk record per line. |
| `embeddings.jsonl` | Successful embedding vectors, empty when embedding is disabled. |
| `report.json` | Document checkpoint with source, stage, result, and issue details. |
| `assets/` | Referenced media, when present. Paths are relative to the document output directory. |
| `source/` | Captured source file for a URL input, when available. |

JSON and JSONL use UTF-8. Each JSONL record ends with LF. Empty JSONL files contain zero bytes. Batch output also includes `run.json`, with per-file outputs in ordinal directories such as `0001/`.

The pipeline captures input bytes and downloads MinerU archives in private working directories. For a text URL, it publishes the downloaded file in `source/`. For a MinerU URL, it publishes the selected `_origin` member as `source/origin.<extension>`. A converted member can have a different extension from the input URL. If the archive has no usable origin, the pipeline publishes the parsed output and records the source issue in `report.json`.

The pipeline locks the output directory and keeps its inode, including on a mounted directory. It prepares the document files in private staging, installs the files, and then updates `report.json` to `completed`. Read the document files as published output only when `report.json` has `run.state = "completed"`. Output locking uses POSIX filesystem operations on macOS and Linux, including the Docker image.

## `chunks.jsonl`

Every row contains these seven fields in the order shown, starting with `id`.

| Field | Meaning |
| --- | --- |
| `id` | Zero-based chunk ordinal within this document output. |
| `body` | Text selected from canonical blocks. |
| `heading_path` | Heading text from outermost to innermost section. |
| `embedding_input` | Exact prepared text, including retained context, or `null`. |
| `assets` | Unique output-relative media paths in first body-use order, such as `assets/<sha256>.png`. Empty when the body has no archived media. |
| `citations` | Compact reading and source positions. |
| `metadata` | Source identity, content fingerprint, and parse classification. |

A body can be empty when it represents media. `embedding_input` can be present when embedding is disabled. Oversized prepared text remains available and receives an embedding error if submitted for embedding.

### Citations

Every row contains all nine citation fields.

| Field | Meaning |
| --- | --- |
| `source_name` | Ingestion name or the caller-supplied display name. |
| `source_uri` | By default, the resolved local file URI or the input URL without its query and fragment. The caller can supply a canonical URI in `SourceIdentity`. |
| `pages` | Sorted original PDF page positions as `{"number": 1, "label": null}` objects, or `null`. |
| `slides` | Sorted original slide numbers, or `null`. |
| `sheets` | Original sheets as `{"name": "Sheet1", "cells": null}` objects, or `null`. |
| `source_lines` | Original decoded-text line ranges for the body, or `null`. |
| `objects` | Body figures, tables, equations, algorithms, and code blocks in selection order. |
| `content_ranges` | One or more enclosing reading windows in `content.md`. |
| `location_status` | `known` when original locations cover all body selections at the available granularity, `partial` for some, or `unavailable` for none. |

Page and slide numbers start at one. Both ends of each line range are inclusive and start at one. The pipeline counts CRLF, CR, and LF as line breaks.

An object has `kind`, `labels`, `anchors`, `body_range`, and `part`. `labels` contains observed display labels. `anchors` contains machine labels such as TeX labels. `body_range` uses zero-based Unicode code points with an exclusive end. A split table has a `part` of the form `{"table_rows": [start, end]}`. That range uses the normalized table grid, a zero-based start, and an exclusive end. Other objects have `part: null`.

Source positioning describes the chunk body. Context included only in `embedding_input` does not expand the body citations.

### Metadata

| Field | Meaning |
| --- | --- |
| `document_id` | Logical document ID supplied by the caller. The default UUID is derived from the resolved file URI or from the request URL with its query and without its fragment. Renewing a signed URL can change the default ID. |
| `document_version` | Caller-supplied source revision, or `null`. |
| `source_sha256` | SHA-256 of the captured source bytes. For a MinerU URL, this hashes the archived origin and can change across runs. The value is `null` if MinerU parses the document without returning a usable origin. |
| `chunk_sha256` | Fingerprint of the body, heading path, embedding input, and body media bytes. |
| `source_type` | Lowercase input extension, such as `pdf` or `markdown`. |
| `content_status` | `parsed` for interpreted body selections, `unparsed` for opaque body selections, or `partial` when both occur. |

To pass a URL from Python, use `UrlInput(url="https://example.org/document.md")` with `process_file` or `process_files`. For text URLs, provide any Markdown image files through `UrlInput.assets` or the CLI's single-input `--asset` option. The pipeline does not fetch linked assets or look for them on the local filesystem.

If your source system tracks document IDs and revisions, supply `SourceIdentity` to `process_file` or set it on an `InputFile` or `UrlInput`.

### Original-source positions

PDF page positions come from verified MinerU content and layout matches. When MinerU returns the submitted PDF unchanged, positions refer to the original directly. For a rewritten PDF, page correspondence requires equal page counts, equal decoded content streams for that page, and equal PDFium renders at 72 and 144 DPI with annotations and form widgets enabled. The comparison bounds render size and leaves optional-content configurations unresolved. Page labels come from the original PDF's valid page-label tree. Redundant numeric labels become `null`.

PPTX positioning requires a byte-identical origin in the provider response, equal slide and group counts, and a unique match of nested text to the slide at the same ordinal. XLSX positioning requires a byte-identical origin and equal sheet and group counts. A single sheet or a uniquely matching sheet title supplies the sheet name. Cell coordinates stay `null`. Unverified original positions remain unavailable.

For MinerU URLs, source positions refer to the file saved in `source/` when its format supports the requested position and the existing checks pass. For example, a PDF converted from a JPG input does not supply source PDF page numbers.

### Chunk fingerprint

The chunk fingerprint uses SHA-256 of a JSON object with exactly `body`, `heading_path`, `embedding_input`, and `content_assets`. `content_assets` holds the SHA-256 of each resolved body media use in selection order, including repetitions. A fingerprint describes only the available media bytes.

Hash computation uses sorted keys, compact separators, UTF-8, literal Unicode, and no trailing newline. It preserves string contents. This canonical ordering applies only to the hash input. The exported JSONL follows the field order above. Source identity, citation positions, and chunk ordinal are outside the chunk fingerprint.

### Generated example

Run this Python example in a directory where `example-output` is absent or empty.

```python
import json
from pathlib import Path

from doc_pipeline import SourceIdentity, process_file

source = Path("example.txt")
source.write_bytes(b"Hello world.\n")
output = Path("example-output")
identity = SourceIdentity(document_id="examples/hello", version="v1")
report = process_file(source, output, identity=identity)
row = json.loads((output / "chunks.jsonl").read_text(encoding="utf-8").splitlines()[0])
print(json.dumps(row, ensure_ascii=False, indent=2))
```

The `source_uri` below illustrates the absolute file URI. Its directory depends on where you run the example.

```json
{
  "id": 0,
  "body": "Hello world.",
  "heading_path": [],
  "embedding_input": "Hello world.",
  "assets": [],
  "citations": {
    "source_name": "example.txt",
    "source_uri": "file:///absolute/path/to/example.txt",
    "pages": null,
    "slides": null,
    "sheets": null,
    "source_lines": [[1, 1]],
    "objects": [],
    "content_ranges": [[1, 1]],
    "location_status": "known"
  },
  "metadata": {
    "document_id": "examples/hello",
    "document_version": "v1",
    "source_sha256": "6472bf692aaf270d5f9dc40c5ecab8f826ecc92425c8bac4d1ea69bcbbddaea4",
    "chunk_sha256": "dc409ea839099c4c6ee3b7bf50c89f73cb3c2aa5a9f9aede83228af2a570cc2c",
    "source_type": "txt",
    "content_status": "parsed"
  }
}
```

## Read surrounding context

`citations.content_ranges` identifies inclusive, one-based line windows in `content.md`. Read those windows, expand them for surrounding context, or read the entire file. Adjacent chunk IDs identify neighboring chunks within the same output. Resolve each `assets` entry against that output directory.

For source positions in a URL run, open the file in `source/` if available. For a local file, use `citations.source_uri` with `pages`, `slides`, `sheets`, or `source_lines`. The caller owns access to that location and preservation of the source revision.

## `embeddings.jsonl`

Each successful record contains `chunk_id`, `input_sha256`, `profile`, and `vector`. Match `chunk_id` to `id` in the same output directory's `chunks.jsonl`. `input_sha256` hashes the exact UTF-8 embedding input. The effective profile includes the model, actual and requested dimensions, encoding format, and a digest of the configured service URL. The vector is an array of finite numbers. The file is empty when embedding is disabled or no vectors succeed.

## `report.json`

The single-file API returns the same object that it writes to `report.json` on completion. Report schema version 2 uses the same keys across formats.

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `2`. |
| `pipeline` | Package `version` and build `revision`. Set `DOC_PIPELINE_REVISION` to supply a revision. The value is `null` when the variable is unset or empty. |
| `run` | Run `id`, zero-based input `index`, UTC `started_at`, `elapsed_seconds`, and lifecycle `state`. A single-file call uses index `0`. |
| `source` | Input `name`, `uri`, `document_id`, `document_version`, `format`, `sha256`, and `bytes`. URI and identity follow the chunk defaults above. Digest and size come from the captured source and are `null` when it is unavailable. An unspecified document version is `"n/a"`. |
| `stages` | Execution records for the seven stages below. |
| `result` | Three distributions over the published chunks. |
| `issues` | Grouped diagnostics, limitations, recovery actions, and failures. |

`0` and `[]` represent a known zero count or empty list. `"n/a"` means the field does not apply to the run. `null` means a value is unknown, has yet to be observed, or depends on publication.

### Run and stage timing

`run.state` is `running`, `completed`, `failed`, or `interrupted`. `completed` means the pipeline published the document files. The result distributions and issues describe partial parsing, missing source locations, and embedding errors independently.

Each stage has a `state`, `seconds`, and `reason`. The states are `not_started`, `running`, `succeeded`, `failed`, `skipped`, `not_applicable`, `not_reached`, and `interrupted`. `reason` explains a non-execution path, such as `disabled` or `upstream_failed`. Failures have details in `issues`. Stage time is `"n/a"` for a stage that does not run.

| Stage | Work and additional fields |
| --- | --- |
| `intake` | Validates the input and captures local files. `encoding` is the configured text encoding for text formats and `"n/a"` for MinerU formats. |
| `acquire` | Downloads text URLs or submits inputs to MinerU and downloads its archives. `service` describes MinerU activity and is `"n/a"` for text formats. |
| `parse` | Parses local input or decodes the provider archive. |
| `render` | Renders Markdown and records source block mappings. |
| `chunk` | Builds chunks. `settings` contains `target_input_bytes` and `max_input_bytes`. |
| `embed` | Generates vectors. `profile` contains `model`, requested `dimensions`, and `endpoint_sha256`. Provider-selected dimensions use `"n/a"`. `usage` is described below. A disabled stage uses `"n/a"` for both fields. |
| `write` | Writes and installs the document files. |

Run elapsed time uses a monotonic clock and includes time between stages. The terminal snapshot includes completed file installation and excludes the write of the snapshot itself. MinerU acquisition time covers submission through archive download or failure. It can include shared waits and download queuing. Acquisition intervals can overlap across documents. Use the batch's own elapsed time for the batch duration.

### Service observations

`stages.acquire.service` contains an endpoint digest in `endpoint_sha256`, the service's `batch_id` and `data_id`, and the following objects.

| Field | Meaning |
| --- | --- |
| `requested` | Requested `model_version`, `enable_formula`, `enable_table`, `language`, and `is_ocr`. |
| `observed` | `backend`, `version`, and `ocr_enabled` as declared by the validated archive. The reported version describes the archive producer. |
| `progress` | The last accepted `total_pages` and `extracted_pages` counts. These are provider progress observations. Use `result.locations` for chunk citation coverage. |

Unobserved values are `null`. Embedding usage records actual HTTP `requests` and `responses`, including retries, splits, and error responses. Each of `prompt_tokens` and `total_tokens` contains `observed` and `reported_responses`. `observed` sums valid counts. It is `0` when the stage sent zero requests and `null` when requests were sent but no response supplied that token counter. `reported_responses` gives the coverage of each sum.

### Result distributions

| Field | Counters |
| --- | --- |
| `result.chunks` | `parsed`, `partial`, and `unparsed`, from `metadata.content_status`. |
| `result.locations` | `known`, `partial`, and `unavailable`, from `citations.location_status`. |
| `result.embedding` | `succeeded`, `failed`, and `ineligible`. A chunk with a null `embedding_input` is ineligible. |

Each distribution partitions the published chunks. Sum `result.chunks` for the chunk count and read `result.embedding.succeeded` for the vector count. An empty published document has all-zero counters. Before publication, each applicable distribution is `null`. Disabled embedding has `result.embedding = "n/a"`.

### Issues

The pipeline groups issues by `stage`, `code`, `reason`, `action`, and `retryable`, in order of first occurrence.

| Field | Meaning |
| --- | --- |
| `stage`, `code` | Stage name and machine-readable issue code. |
| `reason` | Specific cause, including sanitized service details when available. `"n/a"` means the issue has no finer reason. `null` means a reason is unavailable. |
| `action` | `retained_opaque`, `unverified`, `unarchived`, `omitted`, `inferred`, `retried`, `split`, `failed`, or `noted`. |
| `retryable` | Boolean for a classified error, `null` when retryability is unknown, or `"n/a"` for a diagnostic. |
| `occurrences` | Number of occurrences in the group. |
| `targets` | Locations or assets affected by the issue. Each target contains `unit` and `values`. |
| `unlocated` | Number of local occurrences without a reliable target. |

Targets with unit `chunk`, `content_line`, `source_line`, or `source_page` use merged inclusive intervals. Chunk indices start at zero. Line and page numbers start at one. An `asset` target lists reference strings. URL credentials, query parameters, and fragments are removed. Inline data URIs have no asset target. A `document` target has `values = "n/a"`. A target can cover several occurrences, and one occurrence can span several chunks. Before publication, block issues use verified original source lines or pages where available. A failed report never points to an unpublished chunk or Markdown line. Occurrences without a durable target remain in `unlocated`.

## `run.json`

`process_files` returns the same object it writes to `run.json`, including when called with a single input. The CLI selects batch mode when it receives multiple inputs.

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `2`. |
| `run` | Batch `id`, UTC `started_at`, `elapsed_seconds`, and lifecycle `state`. `completed` means the coordinator finished the batch and recorded every file. Individual files can fail. |
| `settings` | `local_workers` and `mineru_batch_size`. |
| `mineru_requests` | HTTP `requests` and `responses` for each of `submit`, `poll`, `upload`, and `download`. Shared calls are counted once. `"n/a"` means MinerU was not used. |
| `files` | Document entries in input order. |

Each file entry contains a zero-based `index`, an output-relative `report` path such as `0001/report.json`, and `unreported`. When a document report is available, including for a failed file, `unreported` is `"n/a"`. If a document checkpoint cannot be created or updated, `report` is `null`. An older `running` checkpoint may still exist on disk. `unreported` carries `source_uri`, `stage`, `code`, `reason`, and `retryable`. Each referenced document has the batch's `run.id` and its own `run.index` and timing.

## Status and failure semantics

After obtaining the output lock, the pipeline writes atomic snapshots to `report.json` and, for a batch, to `run.json`. A caught fatal document error leaves a `failed` report and discards its staged files. A caught interruption updates the affected snapshots to `interrupted` where possible. A caught publication failure rolls back the installed document files. Previously completed siblings remain available.

A process killed with SIGKILL can leave a `running` snapshot and temporary files. A `running` snapshot records the last checkpoint, which may outlast the process. Report files can be absent if the pipeline cannot acquire the directory, write the first snapshot, or reach the first checkpoint. Each snapshot is replaced atomically. Publication as a whole consists of multiple filesystem operations.

The Python single-file API raises on fatal errors. The batch API records caught file errors in the individual reports or in `files[].unreported`. Expected embedding errors still allow publication of the parsed content and successful vectors. The pipeline requires a new or empty output directory for each call, including retries.

The CLI uses text in a terminal and JSON when stdout is redirected. Use `--format text` or `--format json` to choose. In JSON mode, stdout contains the returned report for a completed run or an error object if the call raises a handled exception. Text errors and progress go to stderr. Progress appears by default when stderr is a terminal. Use `--progress` or `--no-progress` to override the default.

| Exit code | Meaning |
| --- | --- |
| `0` | Processing finished without fatal file errors or embedding errors. Partial parsing alone returns `0`. |
| `65` | Non-retryable fatal error, or invalid configuration or input. In a batch, any non-retryable fatal file error takes precedence. |
| `75` | Retryable fatal error or any embedding error, including embedding errors whose own `retryable` field is false. |
| `130` | Interrupted execution, including SIGINT and SIGTERM. |
| `2` | Invalid CLI arguments. The argument parser prints usage to stderr. |

The [chunk contracts](../src/doc_pipeline/contracts/chunk.py) define the serialized chunk fields. The [architecture page](architecture.md) describes how parsing, chunking, and publication fit together.
