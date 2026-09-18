<p align="center">
  <h1 align="center">Scholarly Retrieval Platform</h1>
</p>

<p align="center">
  面向研究者与 AI Agent 的多源学术检索与引文图工具：一套 Python Library，CLI、HTTP API、MCP 三种入口。
</p>

<p align="center">
  <a href="https://github.com/L4stWhisper/scholarly-retrieval-platform/actions/workflows/ci.yml"><img src="https://github.com/L4stWhisper/scholarly-retrieval-platform/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue" alt="Python"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License"></a>
  <a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json" alt="uv"></a>
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff"></a>
  <a href="https://modelcontextprotocol.io/"><img src="https://img.shields.io/badge/MCP-compatible-8A2BE2" alt="MCP"></a>
  <a href="https://github.com/L4stWhisper/scholarly-retrieval-platform/stargazers"><img src="https://img.shields.io/github/stars/L4stWhisper/scholarly-retrieval-platform?style=social" alt="GitHub Stars"></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="docs/cli.md">CLI 指南</a> ·
  <a href="docs/mcp.md">接入 Claude Code / Codex</a> ·
  <a href="docs/providers.md">数据来源</a> ·
  <a href="docs/technical-implementation.md">技术实现</a>
</p>

---

## 功能

- **四种检索**：关键词检索、高级检索（作者、年份、题名、摘要、venue、学科、类型、开放获取、最低引用数、排序）、引用关系检索（References 与 Citations）、相关论文检索（自然语言或正负种子）。
- **多源互补**：OpenAlex、Semantic Scholar、Crossref、arXiv、Europe PMC、DBLP、ACL Anthology、OpenReview、INSPIRE、OpenAIRE、DataCite、OpenCitations 开箱即用；配置密钥后启用 Google Scholar（SerpApi）与 NASA ADS。
- **去重但不丢证据**：按 DOI、arXiv、PMID/PMCID 等强标识保守合并，arXiv 版本与预印本版本归为同一作品；题名相似只进入人工复核，不自动合并。每条结论保留来自哪个来源。
- **失败可见**：每个来源单独报告 `complete`、`partial`、`empty`、`failed`、`throttled`、`skipped`，并披露截断、过滤位置与重试情况；不把部分覆盖伪装成完整覆盖。
- **Agent 原生**：MCP 服务器与 Agent Skill 让 Claude Code、Codex 直接调用结构化工具，返回有界 JSON。
- **可追溯**：可选 SQLite 保存规范结果、原始响应、请求尝试、身份决策与引文证据，支持人工合并/拆分与回滚。
- **Reference 提取**：对合法获得的 JATS、TEI、LaTeX、BibTeX、PDF（GROBID，可选 OCR）抽取参考文献并链接到论文实体。
- **多跳引文图**：在深度、节点数、时间等显式预算下确定性扩展局部引文网络。

## 快速开始

### 安装

需要 Python 3.11 或更高版本。推荐使用 [uv](https://github.com/astral-sh/uv)：

```bash
git clone https://github.com/L4stWhisper/scholarly-retrieval-platform.git
cd scholarly-retrieval-platform
uv sync --extra api --extra mcp
uv run scholar --help
```

不使用 uv 时：

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[api,mcp]"
scholar --help
```

### 配置（可选）

多数来源无需密钥即可使用。建议至少申请免费的 Semantic Scholar key，否则该来源会持续限流：

```bash
cp .env.example .env      # 填入 SEMANTIC_SCHOLAR_API_KEY 等；进程环境变量始终优先于文件
uv run scholar doctor     # 只报告密钥是否存在，不显示值
```

### 最小使用

```bash
# 关键词检索
uv run scholar search "retrieval augmented generation" --limit 5 --format table

# 谁引用了这篇论文 / 这篇论文引用了谁
uv run scholar citations "https://arxiv.org/abs/2603.25723" --limit 20
uv run scholar references "10.1038/s41586-021-03819-2" --limit 20

# 相关论文
uv run scholar related --text "reliable citation graph retrieval for research agents" --limit 10
```

### 接入 Agent

```bash
claude mcp add --scope project scholarly-retrieval -- scholar-mcp
```

Claude Code、Codex 的完整配置与 Skill 安装见 [docs/mcp.md](docs/mcp.md)。

## 文档

| 主题 | 内容 |
|---|---|
| [安装](docs/installation.md) | uv / pip 安装、可选依赖、常见环境问题 |
| [配置](docs/configuration.md) | 环境变量优先级、`.env` 查找顺序、各来源密钥 |
| [CLI 指南](docs/cli.md) | 四种检索、结果阅读模式、多跳扩展、全部命令 |
| [Python Library](docs/library.md) | 在自己的代码中调用 `ScholarService` |
| [HTTP API](docs/api.md) | 常驻服务、路由、鉴权与部署边界 |
| [MCP 与 Agent](docs/mcp.md) | Claude Code、Codex、Skill、Streamable HTTP |
| [数据来源](docs/providers.md) | 来源能力表、按领域选源、Semantic Scholar 限流、Google Scholar 与 ADS 聚合 |
| [Reference 提取](docs/references-and-pdf.md) | 本地 PDF/XML 的参考文献抽取与链接 |
| [存储与部署](docs/storage-and-deployment.md) | SQLite 证据库、维护、Docker 与分布式部署 |
| [常见问题](docs/faq.md) | 限流、额度、路径与 Agent 找不到命令 |
| [开发指南](docs/development.md) | 测试、lint、提交规范、文档结构 |
| [技术实现](docs/technical-implementation.md) | 架构、模块、算法、存储与设计决策 |
| [测试验收](docs/test-acceptance.md) | 自动化、真实接口与 Agent 验收记录 |

## 开发

```bash
uv sync --extra api --extra mcp --extra distributed
uv run ruff check src tests tools
uv run pytest -q
```

提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/)（`feat`、`fix`、`docs`、`refactor`、`test`、`chore`、`ci`），详见[开发指南](docs/development.md)。

## 许可证

本项目以 [MIT License](LICENSE) 开源。论文元数据、摘要、全文链接和其他上游内容仍分别受对应来源条款及原始作品许可证约束；本项目的软件许可证不会改变第三方数据或论文的授权范围。
