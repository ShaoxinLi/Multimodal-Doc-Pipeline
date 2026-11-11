# Multimodal Doc Pipeline

[English](README.md) · [简体中文](README.zh-CN.md)

将文档转换为 Markdown、结构化分块和来源引用，供检索流水线使用。可通过兼容 OpenAI 的服务生成嵌入向量，支持 CLI、Python API 和 Docker。

文本格式在本地处理，无需 API key。PDF、Office 和图像文件会上传至 [MinerU](https://github.com/opendatalab/MinerU)，需要 MinerU API key。

[快速开始](#快速开始) · [获取 Docker 镜像](#获取-docker-镜像) · [配置](#配置) · [输出](#输出) · [文档](#文档)

## 快速开始

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。克隆仓库并安装依赖。使用 Docker 时，请从[获取 Docker 镜像](#获取-docker-镜像)开始。

所有示例中的 `path/to/document` 都需替换为真实文件路径，包含文件扩展名。每次运行都需要不存在或为空的输出目录。

```sh
git clone https://github.com/ShaoxinLi/Multimodal-Doc-Pipeline.git
cd Multimodal-Doc-Pipeline
uv sync --frozen
uv run doc-pipeline path/to/document --out path/to/output
```

首次运行可选择[支持的文本格式](#支持的格式)，无需凭据。处理 PDF、Office 或图像文件前，请先[设置 MinerU key](#配置-mineru)。

命令会打印 JSON 报告，并在输出目录生成 `content.md`、`chunks.jsonl`、`embeddings.jsonl` 和 `report.json`。默认不启用嵌入，因此 `embeddings.jsonl` 为空。

查看结果：

```sh
cat path/to/output/content.md
cat path/to/output/report.json
```

检查 `parse_status` 和 `embedding_errors`。退出码为 `0` 时，解析结果仍可能不完整。

## 获取 Docker 镜像

需要 Docker。下载已发布的镜像，然后检查 CLI：

```sh
docker pull ghcr.io/shaoxinli/multimodal-doc-pipeline:latest
docker run --rm ghcr.io/shaoxinli/multimodal-doc-pipeline:latest --help
```

镜像包含 Python 和项目依赖，使用时无需在本机安装 Python。

如果需要从源码构建，在项目根目录运行：

```sh
docker build -t doc-pipeline:local .
docker run --rm doc-pipeline:local --help
```

使用本地构建时，将下方命令中的已发布镜像名称替换为 `doc-pipeline:local`。

[镜像工作流](.github/workflows/image.yml)会在发布前测试镜像。`latest` 对应默认分支，已发布的构建还带有 `sha-<short>` 标签。`v1.2.3` 等 Git 版本标签会生成 `1.2.3` 等镜像标签。

### 使用 Docker 处理文件

Shell 示例使用 POSIX 语法。将 `/absolute/path/to/documents` 替换为输入文件所在目录。将 `/in/document` 替换为该文件在容器内的路径，包含文件扩展名。

```sh
mkdir -p path/to/output
docker run --rm --user "$(id -u):$(id -g)" \
	-v "/absolute/path/to/documents:/in:ro" \
	-v "$PWD/path/to/output:/out" \
	ghcr.io/shaoxinli/multimodal-doc-pipeline:latest /in/document --out /out
```

输入目录以只读方式挂载。输出目录必须为空，且当前用户可写。如需传入 API key 和配置文件，使用下方的[完整 Docker 配置示例](#配置-docker)。

在 Docker 中查看进度和摘要时，在输入路径后加上 `--progress --format text`。默认格式见 [CLI 输出说明](docs/output-format.md#status-and-failure-semantics)。

## 支持的格式

| 输入 | 扩展名 | 处理方式 |
| --- | --- | --- |
| 文本 | `.md`, `.markdown`, `.txt`, `.rst`, `.tex`, `.latex` | 本地，无需 API key |
| PDF | `.pdf` | MinerU |
| Office | `.doc`, `.docx`, `.ppt`, `.pptx`, `.xls`, `.xlsx` | MinerU |
| 图像 | `.png`, `.jpg`, `.jpeg`, `.jp2`, `.webp`, `.gif`, `.bmp` | MinerU |

每个文件不超过 200 MB，每份 PDF 不超过 200 页。不支持 HTML 和加密 PDF。

## 配置

本地文本处理使用默认设置即可。如需修改 MinerU 选项、分块大小、并发或超时，复制 [config.example.toml](config.example.toml)：

```sh
cp config.example.toml config.toml
```

修改所需设置，然后显式传入配置文件：

```sh
uv run doc-pipeline path/to/document \
	--out path/to/output \
	--config config.toml
```

应用不会自动查找 `config.toml` 或加载 `.env`。非空环境变量中的 key 会覆盖配置文件中的值。分块大小按 UTF-8 字节计量，不是 token 数。

### 配置 MinerU

运行 CLI 前设置 key。使用默认 MinerU 设置时，无需配置文件：

```sh
export MINERU_API_KEY='your-mineru-api-key'
uv run doc-pipeline path/to/document --out path/to/output
```

也可以在 `config.toml` 的 `[mineru]` 段落中设置 `api_key`。

### 启用嵌入

取消 `config.toml` 中 `[embedding]` 段落的注释，将模型和地址替换为你的服务信息：

```toml
[embedding]
model = "your-embedding-model"
api_base_url = "https://your-provider.example/v1"
```

客户端会在地址后追加 `/embeddings`，并将分块文本发送到该服务。设置 key 后，使用配置文件运行：

```sh
export EMBEDDING_API_KEY='your-embedding-api-key'
uv run doc-pipeline path/to/document \
	--out path/to/output \
	--config config.toml
```

仅设置环境变量中的 key 不会启用嵌入，还必须配置 `[embedding]` 段落。仅在服务支持时设置 `dimensions`。

### 配置 Docker

先按上文创建 `config.toml`。要从文件传入 key，复制 [.env.example](.env.example)，再填入所需的 key：

```sh
cp .env.example .env
```

使用这两个文件运行：

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

如果 key 已通过 shell 导出，改用以下命令：

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

## 处理多个文件

将每个输入文件传给 CLI：

```sh
uv run doc-pipeline path/to/document path/to/another-document \
	--out path/to/batch-output
```

结果写入 `0001/`、`0002/` 等编号目录，根目录的 `run.json` 记录各文件的结果。单个文件失败不会阻止其他文件生成结果。

## 使用 Python API

通过 `uv run python` 在项目环境中运行 Python。处理单个文件：

```python
from pathlib import Path
from doc_pipeline import process_file

report = process_file(Path("path/to/document"), Path("path/to/output"))
print(report["parse_status"])
```

批量处理并显式加载配置：

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

`process_file` 也接受同一个 `config` 对象作为第三个参数。

两个函数都接受 `on_progress`，回调会收到包含文件和当前阶段的 `ProgressEvent`。批量处理的回调可能在工作线程中运行，请同步对共享状态的写入。

```python
report = process_file(
	Path("path/to/document"),
	Path("path/to/output"),
	on_progress=lambda event: print(event.path.name, event.stage),
)
```

## 输出

| 文件 | 内容 |
| --- | --- |
| `content.md` | 解析后的 Markdown 文档 |
| `chunks.jsonl` | 包含章节上下文、元数据和来源引用的分块 |
| `embeddings.jsonl` | 成功生成的嵌入向量，未启用嵌入时为空 |
| `report.json` | 解析状态、数量统计、诊断信息、嵌入错误和耗时 |
| `assets/` | 资源快照，仅在有资源时生成 |
| `run.json` | 批量任务中各文件的结果和报告路径 |

引用指向原始文件，仅在经过验证时包含页面坐标。字段定义和引用精度见[输出格式参考](docs/output-format.md)。

### 检查失败或不完整的结果

| 问题 | 检查方法 |
| --- | --- |
| `output_not_empty` | 选择新目录或空目录，重试时也一样。 |
| `missing_mineru_credentials` | 导出 `MINERU_API_KEY`。使用 Docker 时，通过 `-e` 或 `--env-file` 传入。 |
| 没有嵌入向量 | 检查 `[embedding]` 设置和 `report.json` 中的 `embedding_errors`。 |
| 解析结果不完整 | 使用分块前，查看 `diagnostics` 并检查 `content.md`。 |

退出码 `0` 表示处理完成，`2` 表示 CLI 参数无效，`65` 表示不可重试的失败，`75` 表示可重试的失败或任意嵌入错误，`130` 表示运行中断。详见[失败语义](docs/output-format.md#status-and-failure-semantics)。

## 开发

在项目根目录安装依赖并运行检查：

```sh
uv sync --frozen
uv run pytest -q
uv run ruff check src tests
```

测试不会调用真实的付费服务。源代码位于 [src/doc_pipeline](src/doc_pipeline)，测试位于 [tests](tests)。

## 文档

- [架构](docs/architecture.md)介绍解析、分块、引用和模块职责。
- [输出格式](docs/output-format.md)定义输出文件、报告字段和失败行为。
- [配置示例](config.example.toml)列出 MinerU、嵌入、分块、并发和超时设置。
