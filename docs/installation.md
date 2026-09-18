# 安装

需要 Python 3.11、3.12 或 3.13。项目尚未发布到 PyPI，请从源码安装。

## 使用 uv（推荐）

[uv](https://github.com/astral-sh/uv) 会自动创建 `.venv`、按 `uv.lock` 安装锁定版本并暴露 `scholar` 等命令：

```bash
git clone https://github.com/L4stWhisper/scholarly-retrieval-platform.git
cd scholarly-retrieval-platform
uv sync --extra api --extra mcp
uv run scholar --help
```

`uv sync` 默认同时安装开发依赖组（pytest、ruff）。可选依赖按需叠加：

| 命令 | 安装内容 |
|---|---|
| `uv sync` | 核心 Library、`scholar` CLI 与开发依赖 |
| `uv sync --extra api` | 加 FastAPI/uvicorn，提供 `scholar-api` |
| `uv sync --extra mcp` | 加 MCP Python SDK，提供 `scholar-mcp` |
| `uv sync --extra api --extra mcp` | 推荐的完整 Agent 接口 |
| `uv sync --extra api --extra mcp --extra distributed` | 再加 Redis worker |
| `uv sync --extra ocr` | Python OCR 依赖；仍需本机 OCRmyPDF/Tesseract/GROBID 运行时 |

`.python-version` 固定为 3.12；`uv sync --python 3.13` 可切换解释器。

如果希望不加 `uv run` 前缀直接使用命令，激活虚拟环境即可：

```bash
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
scholar --help
```

## 使用 pip

```bash
python -m venv .venv
source .venv/bin/activate          # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[api,mcp]"
```

可选依赖名与 uv 相同：`api`、`mcp`、`distributed`、`ocr`；开发依赖使用 `dev` extra，例如
`pip install -e ".[dev,api,mcp,distributed]"`。

`-e` 是 editable install：包安装到当前环境，源码仍指向本仓库。

## 验证安装

```bash
scholar providers
scholar doctor
python -c "import sys; print(sys.executable)"
```

也可以不依赖 console script：

```bash
python -m scholarly_retrieval --help
python -m scholarly_retrieval search "citation graph" --limit 3
```

正确的模块名是 `scholarly_retrieval`，不是 `scholar`。

## Agent 客户端如何找到命令

Claude Code、Codex 不必安装在同一个环境中，但它们启动的 `scholar-mcp` 必须能从 PATH 找到。
Windows 上可用 `Get-Command scholar-mcp`，其他平台用 `which scholar-mcp` 检查。找不到时在 MCP
配置中写该环境 Python 的绝对路径，见[常见问题](faq.md)。

## 升级

```bash
git pull
uv sync --extra api --extra mcp      # 或重新执行 pip install -e ...
```
