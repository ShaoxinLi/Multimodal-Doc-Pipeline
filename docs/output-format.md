# Output format

`process_file`, `process_files`, and the CLI write artifacts. Each artifact contains the submitted bytes, parsed content, chunks, and the evidence needed to read them after the input path changes.

## Artifact files

| Path | Contents |
| --- | --- |
| `document.json` | Final manifest with the artifact ID, chunk hash recipe, producer version, source identity, parse and chunk settings, and file digests. |
| `source/original.*` | Exact input bytes. |
| `source/dependencies/` | Captured explicit asset inputs, when supplied. |
| `content.md` | Readable document rendering. |
| `blocks.jsonl` | Typed blocks in document order, each with canonical text, a Markdown mapping, and media occurrences. |
| `provenance.jsonl` | One evidence record per chunk, indexed by chunk ordinal. |
| `chunks.jsonl` | One compact chunk record per line. |
| `assets/` | Archived media referenced by blocks. Manifest resource keys resolve to files, byte hashes, and media types. |
| `native/mineru.zip` | Exact MinerU response archive for remote inputs. The manifest identifies relevant members. |
| `native/provider.pdf` | Converted PDF when the parser retains a provider PDF coordinate space. |

JSON and JSONL use UTF-8. Each JSONL record ends with LF. Empty JSONL files contain zero bytes. Artifact paths are relative to the bundle root. Reports and embeddings are separate run outputs. Batch output also includes `run.json`, with per-file artifacts in ordinal directories such as `0001/`.

The pipeline captures and hashes the main input before validation, parsing, and upload. Explicit assets are captured during intake. The local parser captures implicit assets when it discovers references. Capture happens per file.

Publication uses a private staging directory and a per-destination lease. For a new destination, the pipeline renames the completed directory into place. For an existing directory, including an output mount, it preserves that directory and installs the manifest last. A reader can open the artifact after publication and before embedding finishes. The writer and verified reader use POSIX filesystem operations on macOS and Linux, including the Docker image.

## `chunks.jsonl`

Every row contains all eight fields.

| Field | Meaning |
| --- | --- |
| `artifact_id` | SHA-256 identifier of the artifact that owns this occurrence. |
| `id` | Zero-based chunk ordinal within that artifact. |
| `body` | Text selected from canonical blocks. |
| `heading_path` | Heading text from outermost to innermost section. |
| `embedding_input` | Exact prepared text, including retained context, or `null`. |
| `assets` | Unique manifest resource keys in first body-use order. Empty when the body has no archived media. |
| `citations` | Compact reading and source positions. |
| `metadata` | Source identity, content fingerprint, and parse classification. |

A body can be empty when it represents media. `embedding_input` can be present when embedding is disabled. Oversized prepared text remains available and receives an embedding error if submitted for embedding.

### Citations

Every row contains all nine citation fields.

| Field | Meaning |
| --- | --- |
| `source_name` | Ingestion name or the caller-supplied display name. |
| `source_uri` | Artifact-relative snapshot path by default. The caller can supply an external URI that identifies this exact source version. |
| `pages` | Sorted original PDF page positions as `{"number": 1, "label": null}` objects, or `null`. |
| `slides` | Sorted original slide numbers, or `null`. |
| `sheets` | Original sheets as `{"name": "Sheet1", "cells": null}` objects, or `null`. |
| `source_lines` | Original decoded-text line ranges, or `null`. Auxiliary reference definitions remain in evidence. |
| `objects` | Body figures, tables, equations, algorithms, and code blocks in selection order. |
| `content_ranges` | One or more enclosing reading windows in `content.md`. |
| `location_status` | `known` when original locations cover all body selections at the available granularity, `partial` for some, or `unavailable` for none. |

Page and slide numbers start at one. Both ends of each line range are inclusive and start at one. The pipeline counts CRLF, CR, and LF as line breaks. Source code-point ranges in evidence refer to the decoded original, with its original line endings and any byte order mark preserved by the selected encoding.

An object has `kind`, `labels`, `anchors`, `body_range`, and `part`. `labels` contains observed display labels. `anchors` contains machine labels such as TeX labels. `body_range` uses zero-based Unicode code points with an exclusive end. A split table has a `part` of the form `{"table_rows": [start, end]}`. That range uses the normalized table grid, a zero-based start, and an exclusive end. Other objects have `part: null`.

Source positioning uses body selections. Embedding-only ancestor headings, table headers, captions, and footnotes remain in evidence under their respective roles.

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

### Original-source evidence

PDF bounding boxes come from verified MinerU content and layout matches. When MinerU returns the exact submitted PDF, the boxes refer to the original coordinate space. For a rewritten PDF, page correspondence requires equal page counts, equal decoded content streams for that page, and equal PDFium renders at 72 and 144 DPI with annotations and form widgets enabled. The manifest records the comparison basis and failures. The page comparison bounds render size and leaves PDF optional-content configurations unresolved. Corresponding rewritten pages provide page numbers. Their bounding boxes stay in the provider coordinate space. Page labels come from the original PDF's valid page-label tree. Redundant numeric labels become `null`.

PPTX positioning requires an identical retained origin, equal slide and group counts, and a unique match of nested text to the slide at the same ordinal. XLSX positioning requires an identical retained origin and equal sheet and group counts. A single sheet or a uniquely matching sheet title supplies the sheet name. Cell coordinates stay `null`. DOC, PPT, and XLS retain available provider evidence.

### Hash recipes

The chunk fingerprint uses SHA-256 of a JSON object with exactly `body`, `heading_path`, `embedding_input`, and `content_assets`. `content_assets` holds the SHA-256 of each resolved body media use in selection order, including repetitions. Unresolved media stays in evidence. A fingerprint describes only the available media bytes.

Canonical JSON uses sorted keys, compact separators, UTF-8, literal Unicode, and no trailing newline for a single value. It preserves string contents. The artifact ID is SHA-256 of canonical `document.json` with only `artifact_id` removed. Its `files` map covers full bytes of the listed files. `chunk_rows_sha256` covers canonical JSONL rows with only `artifact_id` removed from each row. The reader checks the canonical chunk bytes and each row's `artifact_id` as well.

The artifact ID covers the retained native archive. A provider rerun can change the ID even when the displayed content and chunk fingerprints agree. The embedding profile and run timing are outside the artifact identity.

### Generated example

Run this Python example in a directory where `example-output` is absent or empty.

```python
from pathlib import Path

from doc_pipeline import Artifact, SourceIdentity, process_file

source = Path("example.txt")
source.write_bytes(b"Hello world.\n")
output = Path("example-output")
identity = SourceIdentity(document_id="examples/hello", version="v1")
report = process_file(source, output, identity=identity)
artifact = Artifact.open(output, expected_id=report["artifact_id"])
print(artifact.chunk(0).model_dump_json(indent=2))
```

```json
{
  "artifact_id": "18d091a301a187d9bec35c4f990ee587ab3f91a0bff7228c8d6f3bcf4d367a7a",
  "id": 0,
  "body": "Hello world.",
  "heading_path": [],
  "embedding_input": "Hello world.",
  "assets": [],
  "citations": {
    "source_name": "example.txt",
    "source_uri": "source/original.txt",
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

## Verified reads

Open an artifact by its directory. Pass an expected ID when the caller already has a chunk reference.

```python
row = artifact.chunk(0)
nearby = artifact.neighbors(row.id, radius=2)
windows = artifact.context(row.id, before=2, after=3)
selection, blocks = artifact.evidence(row.id)
original_bytes = artifact.source()
media_bytes = [artifact.asset(key) for key in row.assets]
```

`neighbors` returns available neighboring chunks in ordinal order. `context` clips the requested expansion to the rendered document and merges adjacent windows. `content()` returns the complete verified Markdown. `evidence` returns the chunk's selected block ranges and the referenced block records. Each body fragment is the corresponding slice of a block's `canonical_text`. Join the body fragments with two LF characters to reconstruct `body`.

`Artifact.open` raises `ArtifactError` for an incomplete, corrupted, or mismatched bundle. The open reader retains verified chunks, Markdown, and evidence. Source and asset reads verify the bytes they return. An invalid ordinal raises `IndexError`. An unknown resource key raises `KeyError`.

## `embeddings.jsonl`

Each successful record contains `artifact_id`, `chunk_id`, `input_sha256`, `profile`, and `vector`. Join `artifact_id` and `chunk_id` to the chunk occurrence. `input_sha256` hashes the exact UTF-8 embedding input. The effective profile includes the model, actual and requested dimensions, encoding format, and a digest of the configured service URL. The vector is an array of finite numbers. The file is empty when embedding is disabled or no vectors succeed.

## `report.json`

The single-file API returns the same object that it writes to `report.json`.

| Field | Meaning |
| --- | --- |
| `source` | Resolved intake path and snapshot byte digest for the run report. |
| `artifact_id` | Identifier of the published artifact. |
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

A fatal single-file error raises an exception instead of returning a report. A caught batch file error is recorded in `run.json`. A published artifact remains readable if embeddings or report writing fail. Readers require a finalized, verified `document.json`. Interruption can prevent the final batch report.

The CLI prints a summary to stdout in a terminal and JSON when stdout is redirected. Use `--format text` or `--format json` to choose the format. Text-mode errors go to stderr. Progress also goes to stderr and appears by default when stderr is a terminal. Use `--progress` or `--no-progress` to override the default.

| Exit code | Meaning |
| --- | --- |
| `0` | Processing finished without fatal file errors or embedding errors. Partial parsing alone still returns `0`. |
| `65` | Non-retryable fatal error, or invalid configuration or input. In a batch, any non-retryable fatal file error takes precedence. |
| `75` | Retryable fatal error or any embedding error, including embedding errors whose own `retryable` field is false. |
| `130` | Interrupted execution, including SIGINT and SIGTERM. |
| `2` | Invalid CLI arguments reported by the argument parser. The argument parser prints usage to stderr. |

`retryable` describes an individual error. It does not mean that a run can resume in its existing output directory. The pipeline requires an empty or nonexistent output directory for each call.

The [artifact contracts](../src/doc_pipeline/contracts/artifact.py) define the serialized fields. The [architecture page](architecture.md) describes how parsing, chunking, and publication fit together.
