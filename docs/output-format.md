# Output format

`process_file`, `process_files`, and the CLI write Markdown, chunks, optional vectors, and a processing report.

## Output files

| Path | Contents |
| --- | --- |
| `content.md` | Readable document rendering. |
| `chunks.jsonl` | One compact chunk record per line. |
| `embeddings.jsonl` | Successful embedding vectors, empty when embedding is disabled. |
| `report.json` | Per-file processing status, counts, diagnostics, and timings. |
| `assets/` | Referenced media, when present. Paths are relative to the document output directory. |

JSON and JSONL use UTF-8. Each JSONL record ends with LF. Empty JSONL files contain zero bytes. Batch output also includes `run.json`, with per-file outputs in ordinal directories such as `0001/`.

The pipeline uses private input captures during processing to keep source hashes and parsed bytes consistent. It does not retain the source document or MinerU response archive in the output. Original-source access belongs to the caller through `citations.source_uri`.

Each document is prepared in private staging before publication. The pipeline preserves an existing output directory, including a mounted directory, and installs `report.json` last. Output locking uses POSIX filesystem operations on macOS and Linux, including the Docker image.

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
| `source_uri` | Resolved original input file URI by default. The caller can supply an external URI that identifies this exact source version. |
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
| `document_id` | Stable logical document ID supplied by the caller. The default UUID is derived from the resolved input path and changes on rename. |
| `document_version` | Caller-supplied source revision, or `null`. |
| `source_sha256` | SHA-256 of the captured input bytes. |
| `chunk_sha256` | Fingerprint of the body, heading path, embedding input, and body media bytes. |
| `source_type` | Lowercase input extension, such as `pdf` or `markdown`. |
| `content_status` | `parsed` for interpreted body selections, `unparsed` for opaque body selections, or `partial` when both occur. |

If your source system tracks document IDs and revisions, supply `SourceIdentity` to `process_file` or set it on each batch `InputFile`.

### Original-source positions

PDF page positions come from verified MinerU content and layout matches. When MinerU returns the submitted PDF unchanged, positions refer to the original directly. For a rewritten PDF, page correspondence requires equal page counts, equal decoded content streams for that page, and equal PDFium renders at 72 and 144 DPI with annotations and form widgets enabled. The comparison bounds render size and leaves optional-content configurations unresolved. Page labels come from the original PDF's valid page-label tree. Redundant numeric labels become `null`.

PPTX positioning requires a byte-identical origin in the provider response, equal slide and group counts, and a unique match of nested text to the slide at the same ordinal. XLSX positioning requires a byte-identical origin and equal sheet and group counts. A single sheet or a uniquely matching sheet title supplies the sheet name. Cell coordinates stay `null`. Unverified original positions remain unavailable.

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

For the original document, use `citations.source_uri` with `pages`, `slides`, `sheets`, or `source_lines`. The caller owns access to that location and preservation of the source revision. `metadata.source_sha256` identifies the input bytes used for this run.

## `embeddings.jsonl`

Each successful record contains `chunk_id`, `input_sha256`, `profile`, and `vector`. Match `chunk_id` to `id` in the same output directory's `chunks.jsonl`. `input_sha256` hashes the exact UTF-8 embedding input. The effective profile includes the model, actual and requested dimensions, encoding format, and a digest of the configured service URL. The vector is an array of finite numbers. The file is empty when embedding is disabled or no vectors succeed.

## `report.json`

The single-file API returns the same object that it writes to `report.json`.

| Field | Meaning |
| --- | --- |
| `source` | Resolved intake path and snapshot byte digest for the run report. |
| `parse_status` | `complete` or `partial`. Partial means that the parsed document contains opaque content. |
| `chunk_count`, `embedding_count` | Number of exported chunks and successful vectors. |
| `chunking` | Effective `target_input_bytes` and `max_input_bytes`. |
| `embedding` | Configured `model` and `dimensions`, or `null` when disabled. `dimensions` stays `null` if unspecified. |
| `embedding_errors` | Per-chunk errors with `chunk_id`, `code`, and `retryable`. |
| `diagnostics` | Parser and asset diagnostic strings. A nonempty array does not necessarily mean partial parsing. |
| `execution` | Execution record described below. |

### Execution record

| Field | Meaning |
| --- | --- |
| `run_id` | Identifier shared by all files in a batch, or assigned to a single-file call. |
| `started_at`, `finished_at`, `elapsed_seconds` | Per-file timing. |
| `stage_seconds` | Timings for observed stages, such as `intake`, `remote_acquisition`, `parse`, `render`, `chunk`, `write`, and `embedding`. Unreached stages are absent. |
| `status` | `complete`, `partial`, or `failed`, as defined below. |
| `parse_status` | Parse result, or `null` if parsing did not return. |
| `chunk_count`, `embedding_count`, `embedding_error_count` | Counts observed before the record was finalized. |
| `mineru` | Optional provider observation with `data_id`, `batch_id`, `total_pages`, `extracted_pages`, and `options`. Unknown observations are `null`. |
| `embedding_usage` | Optional token-usage observations, described below. |
| `error` | Present for a fatal file error in the batch record. Contains `stage`, `code`, and `retryable`. |

`mineru.options` contains `model_version`, `enable_formula`, `enable_table`, `language`, and `is_ocr`. Provider page counts describe processing progress, not verified citation coverage.

`embedding_usage` contains `responses`, `prompt_tokens`, and `total_tokens`. Each token field contains `observed_tokens` and `responses_with_usage`. `observed_tokens` is `null` when no response supplied that counter. Otherwise it sums the reported values, which can cover fewer than `responses` responses. Usage observations do not guarantee that vectors passed validation.

The execution snapshot is taken before the final report write. Its timing does not include that write. Per-file elapsed time can include waiting between stages, so it need not equal the sum of stage timings.

## `run.json`

`process_files` returns the same aggregate object that it writes to `run.json`, including when called with a single input. The CLI selects batch mode only when given multiple inputs.

| Field | Meaning |
| --- | --- |
| `run_id`, `started_at`, `finished_at`, `elapsed_seconds` | Batch identity and timing. |
| `status` | `failed` if every file failed, `partial` if any file failed or was partial, otherwise `complete`. |
| `file_count` | Number of inputs. |
| `complete_file_count`, `partial_file_count`, `failed_file_count` | Counts by per-file execution status. |
| `chunk_count`, `embedding_count`, `embedding_error_count` | Sums of the per-file counters. A failed file's counters can reflect work completed before failure. |
| `embedding_usage` | Aggregate usage observations. Present when embedding is configured. |
| `files` | Per-file entries in input order. |

Each `files` entry contains `index`, `input`, `report`, and the execution-record fields directly, without an `execution` wrapper. `input` is the submitted `Path` converted to a string, without resolving it to an absolute path. `report` is a path relative to the batch root, such as `0001/report.json`, or `null` for a failed file.

## Status and failure semantics

The three status concepts are independent.

| Value | What it describes |
| --- | --- |
| `parse_status` | Whether the parser preserved any content as opaque rather than interpreting it. |
| Per-file `status` | `failed` for a fatal error. Otherwise `partial` for partial parsing or any embedding error. Otherwise `complete`. |
| Citation `location_status` | Coverage of verified original-source locations for the chunk body. |

Embedding disabled, an empty document, or missing citation coordinates can still produce `status: "complete"`. A partial document can still contain usable chunks and vectors.

A fatal single-file error raises an exception instead of returning a report. A caught batch file error is recorded in `run.json`. A fatal error before publication leaves no completed document output. Expected embedding errors still produce content and a report, with successful vectors when available. Interruption can prevent the final batch report.

The CLI prints a summary to stdout in a terminal and JSON when stdout is redirected. Use `--format text` or `--format json` to choose the format. Text-mode errors go to stderr. Progress also goes to stderr and appears by default when stderr is a terminal. Use `--progress` or `--no-progress` to override the default.

| Exit code | Meaning |
| --- | --- |
| `0` | Processing finished without fatal file errors or embedding errors. Partial parsing alone still returns `0`. |
| `65` | Non-retryable fatal error, or invalid configuration or input. In a batch, any non-retryable fatal file error takes precedence. |
| `75` | Retryable fatal error or any embedding error, including embedding errors whose own `retryable` field is false. |
| `130` | Interrupted execution, including SIGINT and SIGTERM. |
| `2` | Invalid CLI arguments reported by the argument parser. The argument parser prints usage to stderr. |

`retryable` describes an individual error. It does not mean that a run can resume in its existing output directory. The pipeline requires an empty or nonexistent output directory for each call.

The [chunk contracts](../src/doc_pipeline/contracts/chunk.py) define the serialized fields. The [architecture page](architecture.md) describes how parsing, chunking, and publication fit together.
