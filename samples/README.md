# Document parsing samples

Use these samples to try the pipeline and check parser regressions. The corpus includes Chinese and English text, tables, images, and formulas. Some extension aliases and image encodings share content.

Run the examples from the repository root after [installing the project](../README.md#quick-start). Use a new or empty output directory for each run.

## Run a sample from a URL

Pass a URL from [`urls.txt`](urls.txt) as a positional argument. Each line points to one input file at a fixed Git commit. This plain-text sample runs with the default configuration.

```sh
uv run doc-pipeline "$(grep '/txt_001_rfc_rfc2119_key_words\.txt$' samples/urls.txt)" \
	--out out/sample-txt
```

Open `out/sample-txt/content.md` to read the result. The output directory also contains `chunks.jsonl`, `report.json`, and the downloaded file under `source/`.

PDF, Office, and standalone image inputs require a [MinerU API key](../README.md#configure-mineru). MinerU fetches those URLs directly. If MinerU rejects a GitHub Raw link, download the file and pass its local path.

## Run a sample with its assets

Download and extract the [sample archive](https://github.com/ShaoxinLi/Multimodal-Doc-Pipeline/archive/9174b2a3788620e9dc9615834da8015cf458e43e.tar.gz). Copy the format folders from the archive's `samples/` directory into the repository's `samples/` directory.

Keep each structured text document beside its `assets/` folder so that relative image paths resolve. Pass the document file to the CLI.

```sh
uv run doc-pipeline \
	samples/md/md_011_microsoft_genai_intro/md_011_microsoft_genai_intro.md \
	--out out/sample-genai
```

For a Markdown URL, supply any companion image through `--asset REFERENCE=PATH`. The URL list contains the primary documents. Use the archive to get their companion files.

## Choose a format

The URL list contains 240 inputs across 20 extensions, with 12 inputs per extension. List the extensions and counts with this command.

```sh
awk -F. '{print $NF}' samples/urls.txt | sort | uniq -c
```
