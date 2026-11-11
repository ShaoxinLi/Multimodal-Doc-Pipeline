# Architecture

Multimodal Doc Pipeline turns files into readable Markdown and retrieval chunks with source citations. Optional embeddings let downstream systems index those chunks. The pipeline writes files to disk. It does not run a search service or manage a vector database.

This page explains how the implementation fits together. [Output format](output-format.md) defines the exported data. Installation and usage are in the [README](../README.md).

## One pipeline, two parsing paths

The CLI calls the same `process_file` and `process_files` functions that Python callers use. Docker packages the CLI without adding a separate processing path.

```mermaid
flowchart TD
	CLI["CLI · also used by Docker"] --> PIPE["pipeline.py · validate and coordinate"]
	API["Python API"] --> PIPE
	PIPE --> LOCAL["Local text parsers"]
	PIPE --> REMOTE["MinerU client · upload, poll, download"]
	REMOTE --> DECODE["Validate archive and decode blocks"]
	LOCAL --> CONTENT["ParsedContent · blocks, assets, source evidence"]
	DECODE --> CONTENT
	CONTENT --> RENDER["Render Markdown and block mappings"]
	RENDER --> MD["content.md"]
	CONTENT --> CHUNK["Build chunks and citations"]
	RENDER -->|mappings| CHUNK
	CHUNK --> JSONL["chunks.jsonl"]
	CHUNK --> EMBED["Optional embedding service"]
	EMBED --> VECTORS["embeddings.jsonl"]
	PIPE -. execution records .-> REPORT["report.json · run.json for batches"]
```

Text formats are parsed locally. PDF, Office, and image files are uploaded to MinerU. Both paths produce `ParsedContent`, so rendering and chunking do not depend on the input format or the provider's response schema.

The diagram shows data flow. The coordinator also owns file writes and execution records. `embeddings.jsonl` is created even when embedding is disabled, in which case it is empty.

## Structured blocks preserve meaning and evidence

[`ParsedContent`](../src/doc_pipeline/contracts/document.py) contains ordered `Block` objects, inline asset references, a parse status, and diagnostics. Each block has a typed payload, such as a heading, text, table, code, equation, or image. Source locations belong to the block rather than to its rendered text.

This shared model separates three representations with different purposes.

- The original file supplies source evidence and a SHA-256 digest.
- `content.md` supplies a readable rendering. Tables may remain HTML, and unsupported syntax may appear in fenced blocks.
- Chunks supply text for retrieval. Their text need not be a substring of the rendered Markdown.

The renderer returns both Markdown and `MarkdownMapping` records. The chunker uses those mappings and block selections to connect each chunk to the rendered document and the original source. It does not parse the Markdown again to recover citations.

Unsupported content becomes `Opaque` where the parser can preserve it. That makes `parse_status` partial. Nonempty opaque content remains in the output, but its chunk has no embedding input. A diagnostic or an unresolved citation alone does not make parsing partial.

## Chunking follows document structure

The [chunk builder](../src/doc_pipeline/chunking/builder.py) tracks headings and packs prose into chunks. It uses sentence and line boundaries where possible. Oversized tables can split by row groups, and oversized code can split by lines. Slide boundaries reset the heading context.

`body` holds the selected content. `embedding_input` can add headings, captions, table headers, or footnotes so a retrieved fragment retains context. Citations distinguish body text from added context and headings.

Size budgets count UTF-8 bytes in the embedding input, not tokens. The target guides packing. The maximum is checked before embedding, but it is not a guarantee that every exported chunk fits. An indivisible element can remain oversized and receive an `input_too_large` embedding error.

## Citations require evidence from the original file

Local parsers track character ranges in the decoded source. When parsing changes the text, the citation can identify an enclosing source region instead of an exact character match.

For remote documents, the [PDF evidence matcher](../src/doc_pipeline/parsers/multimodal/pdf_evidence.py) checks provider content against layout data and PDF page geometry. The [exporter](../src/doc_pipeline/chunking/citations.py) then retains only locations whose file reference matches the original input.

This check matters for Office files. A provider-generated PDF can help interpret the content, but its coordinates do not locate text in the original Office file. Those locations are removed from exported citations. The content remains available, with unresolved source precision instead of misleading coordinates.

Parsing completeness and citation precision therefore answer different questions. A document can parse completely while its original source locations remain unavailable.

## Execution and failure boundaries

[`pipeline.py`](../src/doc_pipeline/pipeline.py) validates inputs, selects parsers, renders content, builds chunks, writes outputs, and optionally requests embeddings. It records per-stage timings and counts throughout that work.

For batches, local documents run in a thread pool controlled by `local_workers`. Remote documents are submitted in groups controlled by `mineru.batch_size`. Local work can overlap remote acquisition. The final `run.json` preserves input order regardless of completion order.

Caught failures belong to individual files, so one failed document does not stop the remaining batch. Embedding failures are recorded per chunk and leave parsed content available. A successful embedding response with missing vectors or repeated input indices triggers one resend of the same batch. If that resend fails, processing continues with the next batch. The Python single-file API raises fatal processing errors. The batch API records caught file errors in `run.json`.

Each run requires an empty or nonexistent output directory. There is no resume or overwrite mode. Each document writes to a temporary sibling directory and publishes the whole bundle by renaming it after the report is ready. A caught file failure discards that temporary bundle. A sudden process stop can leave temporary files, and an interrupted batch may have no final `run.json`.

## Where responsibilities live

The source is grouped around the shared document model and the steps that consume it.

- [`cli.py`](../src/doc_pipeline/cli.py) handles arguments, JSON on stdout, and exit codes. [`pipeline.py`](../src/doc_pipeline/pipeline.py) coordinates processing for both public APIs.
- [`contracts/`](../src/doc_pipeline/contracts/) defines configuration, document blocks, chunks, citations, and execution records.
- [`parsers/text/`](../src/doc_pipeline/parsers/text/) handles local syntax and assets. [`parsers/multimodal/`](../src/doc_pipeline/parsers/multimodal/) handles MinerU requests, archive validation, block decoding, and PDF evidence.
- [`parsed_content/`](../src/doc_pipeline/parsed_content/) defines block text selection and Markdown rendering. [`chunking/`](../src/doc_pipeline/chunking/) builds retrieval chunks and exports citations and metadata.
- [`services/`](../src/doc_pipeline/services/) handles embeddings, HTTP helpers, hashing, and file serialization.

Parser changes meet the rest of the system at `ParsedContent`. Output changes meet downstream consumers at the [serialized contract](output-format.md), including its range and precision rules.
