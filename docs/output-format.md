# Output format

This page describes the files written by `process_file` and `process_files`. The CLI uses the same format. [Architecture](architecture.md) explains the processing model. Commands and configuration are in the [README](../README.md).

## Files and encoding

A completed single-file call writes these files into its output directory.

| File | Contents |
| --- | --- |
| `content.md` | Rendered document, including supported Markdown and HTML. |
| `chunks.jsonl` | One chunk object per line. |
| `embeddings.jsonl` | One successful embedding per line. Empty when embedding is disabled or no vectors succeed. |
| `report.json` | Document result, settings, diagnostics, embedding errors, and execution record. |
| `assets/` | Asset snapshots, when present. References are relative to the document's output directory. |

All text output uses UTF-8. JSONL files contain JSON objects separated by newlines, without an enclosing array. Empty JSONL files contain no records.

Batch output adds `run.json` at the root. Each input is assigned a directory such as `0001/` or `0002/`, in input order. A failed input may have no directory or only partial artifacts. Its `run.json` entry has `report: null`.

There is no output schema version field. The definitions below describe the current exporter, whose source is linked at the end of this page.

## Shared conventions

| Value | Convention |
| --- | --- |
| `FileRef` | `{"path": "...", "sha256": "..."}`. Original source paths are absolute. SHA-256 is a 64-character lowercase hexadecimal digest of the file bytes. |
| Character range | `{"start": 0, "end": 12}`. Zero-based, start-inclusive, end-exclusive Unicode code points. These are not byte offsets or UTF-16 code units. |
| Line range | `first_line` and `last_line` are one-based and both inclusive. |
| Array and page indices | Zero-based. This includes chunk IDs, citation indices, table row ranges, page indices, and slide indices. |
| Timestamps and durations | Timestamps use ISO 8601 in UTC. Durations are seconds measured with a monotonic clock. |

Text-source ranges refer to the original decoded text, including its original line endings and any byte order mark. Markdown ranges refer to `content.md`.

Optional citation fields are omitted when their value is `None`. Metadata fields retain JSON `null`. `embedding_input` is always present and can be `null`. Optional execution sections are omitted when unavailable.

## `chunks.jsonl`

Each line has these fields.

| Field | Meaning |
| --- | --- |
| `id` | Sequential integer starting at zero within this document. It is not a global or cross-run identifier. |
| `body` | Selected block text. It may differ from the Markdown rendering. |
| `heading_path` | Heading text from outermost to innermost section. |
| `embedding_input` | Text prepared for embedding, including retained context. `null` for chunks preserved without embeddable text. |
| `citations` | Source and Markdown mappings for the body, context, and headings. |
| `metadata` | Document identity, source reference, section, page, and object metadata. |

`embedding_input` is prepared even when embedding is disabled. A non-null value does not guarantee a successful vector. An input that exceeds `max_input_bytes` stays in the chunk file but receives an embedding error when embedding is enabled.

### Example

For `example.txt` containing `Hello world.` followed by a newline, the chunk has the following selected fields. The example omits citations and metadata for readability.

```json
{
	"id": 0,
	"body": "Hello world.",
	"heading_path": [],
	"embedding_input": "Hello world."
}
```

These commands reproduce the example from the repository root. The output directory must not already contain files.

```sh
printf 'Hello world.\n' > example.txt
uv run doc-pipeline example.txt --out example-output
```

### Citations

Each citation refers to one selected region of a parsed block.

| Field | Meaning |
| --- | --- |
| `block_id`, `kind` | Block identifier within the document and payload type, such as `text`, `table`, or `image`. |
| `role` | `body` for selected content, `context` for added non-heading context, or `heading` for section context. |
| `block_text_range` | Range within the block's canonical text, the representation used for chunk selection. |
| `body_range` | Range within `body`. Present for body citations. |
| `input_range` | Range within `embedding_input`. Present when the cited text is included there. |
| `heading_index` | Index into `heading_path`, when the citation represents a heading. |
| `markdown` | Mapping into `content.md`, with `codepoints`, `first_line`, `last_line`, and `precision`. |
| `locations` | Zero or more original-source locations, described below. |
| `precision` | Precision of the original-source mapping. Independent of `markdown.precision`. |
| `reason` | Explanation for a location limitation, when available. |
| `table_rows` | Selected table row range, when the chunk selects rows. Uses `start` and exclusive `end`. |
| `labels`, `assets` | Recognized object labels and archived asset paths associated with the selection. Empty arrays when none apply. |

Separators and generated table column names do not have source citations. A heading can remain in `heading_path` even when it was dropped from `embedding_input` to fit the size budget. Its citation then has no `input_range`.

Source precision has three values.

| `precision` | Meaning |
| --- | --- |
| `exact` | The selected text has an exact source-text mapping. |
| `enclosing` | The source region contains the selection but can include more content. |
| `unresolved` | The original-source location is not fully verified. |

`markdown.precision` is either `exact` or `enclosing`. For the text example above, source precision is `exact`, but Markdown precision is `enclosing` because the renderer escapes the period. The source range is `[0, 12)`, and the Markdown range is `[0, 13)`.

### Source locations

The `kind` field identifies the location shape.

| `kind` | Fields | Coordinate system |
| --- | --- | --- |
| `native` | `path`, `codepoints`, `first_line`, `last_line` | Character and line ranges in the original decoded text. |
| `pdf` | `path`, `page_index`, `bbox`, `page_size` | Verified coordinates on the original PDF page. |

For PDF locations, `bbox` is `[x1, y1, x2, y2]` and `page_size` is `[width, height]`. Coordinates use PDF points with the origin at the top-left of the media box. They are not pixels or normalized values. Crop offsets have already been applied. Unsupported page geometry can leave locations unresolved.

The exporter removes locations that refer to a provider-generated conversion rather than the original file. For example, coordinates from an Office-to-PDF conversion do not appear as original Office coordinates. Affected citations use `precision: "unresolved"` and `reason: "original_source_location_unverified"`.

### Metadata

| Field | Meaning |
| --- | --- |
| `document_id` | File URI derived from the absolute original source path. It identifies a path, not file contents. |
| `document_version` | Currently `null`. The content digest is in `source.sha256`. |
| `source` | Original input `FileRef`. |
| `source_type` | Lowercase input extension without the dot, such as `pdf` or `markdown`. |
| `section_path` | Same heading text as `heading_path`. |
| `block_ids` | Distinct block IDs from body citations, in first-appearance order. |
| `page_indices` | Sorted original PDF page indices from body citations, or `null`. |
| `page_coordinate_space` | `"original"` when `page_indices` is present, otherwise `null`. |
| `page_status` | Page coverage of body citations, defined below. |
| `slide_indices` | Sorted provider slide-group indices for PowerPoint body blocks when known, otherwise `null`. These are not verified page coordinates. |
| `labels` | Arrays named `tables`, `algorithms`, `equations`, `figures`, and `code`, collected from body citations. |
| `source_locations` | References into `citations`, including body, context, and heading locations. |

Each `source_locations` entry has `citation_index`, `location_index`, `coordinate_space`, and `source`. The indices address `citations[citation_index].locations[location_index]`. Current exports use `coordinate_space: "original"` and the original input `FileRef`.

| `page_status` | Meaning |
| --- | --- |
| `known` | Every body citation has a PDF location, and no body location was removed during export. |
| `partial` | Some original PDF pages are known, but coverage is incomplete or a body location was removed. |
| `nonpaged` | All body citations have native text locations, with no body locations removed. |
| `unavailable` | No original PDF page indices are available, and native text coverage is insufficient for `nonpaged`. |

The internal model also accepts `mixed` and converted coordinate spaces. The current chunk exporter does not emit them.

## `embeddings.jsonl`

Each successful record has `chunk_id` and a `vector` array of finite numbers. `chunk_id` refers to `id` in the same document's `chunks.jsonl`. Record position is not a join key because failed and ineligible chunks have no vector record.

The embedding model and configured dimensions are recorded in `report.json`. When dimensions are unspecified, the client infers vector length from the first successful response and checks later responses within the same document.

## `report.json`

The single-file API returns the same object that it writes to `report.json`.

| Field | Meaning |
| --- | --- |
| `source` | Original input `FileRef`. |
| `parse_status` | `complete` or `partial`. Partial means that the parsed document contains opaque content. |
| `chunk_count`, `embedding_count` | Number of exported chunks and successful vectors. |
| `chunking` | Effective `target_input_bytes` and `max_input_bytes`. |
| `embedding` | Configured `model`, `dimensions`, and `api_base_url`, or `null` when disabled. API keys are excluded. `dimensions` stays `null` if unspecified. |
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
| Citation `precision` | Whether the selected content has a verified original-source location. |

Embedding disabled, an empty document, or missing citation coordinates can still produce `status: "complete"`. A partial document can still contain usable chunks and vectors.

A fatal single-file error raises an exception instead of returning a report. A caught batch file error is recorded in `run.json`. Earlier assets or data files can remain after failure, so their presence alone does not establish success. Reports use atomic replacement, but the output directory as a whole does not. Interruption or a failure outside per-file processing can prevent the final batch report.

The CLI emits its result or error as JSON on stdout and logs execution events to stderr.

| Exit code | Meaning |
| --- | --- |
| `0` | Processing finished without fatal file errors or embedding errors. Partial parsing alone still returns `0`. |
| `65` | Non-retryable fatal error, or invalid configuration or input. In a batch, any non-retryable fatal file error takes precedence. |
| `75` | Retryable fatal error or any embedding error, including embedding errors whose own `retryable` field is false. |
| `130` | Interrupted execution, including SIGINT and SIGTERM. |
| `2` | Invalid CLI arguments reported by the argument parser. This path prints usage rather than a JSON result. |

`retryable` describes an individual error. It does not mean that a run can resume in its existing output directory. The pipeline requires an empty or nonexistent output directory for each call.

## Source definitions

The [chunk models](../src/doc_pipeline/contracts/chunk.py) define field shapes. The [chunk exporter](../src/doc_pipeline/chunking/citations.py) defines exported metadata and omission rules. [Pipeline orchestration](../src/doc_pipeline/pipeline.py), [execution records](../src/doc_pipeline/contracts/run.py), [embedding serialization](../src/doc_pipeline/services/embeddings.py), and the [CLI](../src/doc_pipeline/cli.py) define reports and failure behavior.
