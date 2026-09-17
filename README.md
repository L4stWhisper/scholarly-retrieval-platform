# Scholarly Retrieval Platform

面向研究者与 AI Agent 的多源论文检索工具。它把多个学术数据源返回的论文元数据、引用关系和
相关论文结果规范化、聚合并保守去重，同时保留每个结论来自哪个数据源。

项目提供四类核心检索：

- 关键词检索：按研究主题寻找候选论文；
- 高级检索：增加作者、年份、题名、摘要、venue、学科、类型、开放获取、最低引用数和排序条件；
- 引用关系检索：查看一篇论文的 References（它引用谁）和 Citations（谁引用它）；
- 相关性检索：使用自然语言或正负种子论文寻找相关工作。

同一套 Python Library 同时提供 CLI、HTTP API 和 MCP 三种平行入口。Agent Skill 是调用策略，
不包含另一套检索逻辑。

## 为什么使用本项目

- 多源互补：综合图谱、预印本、会议、生命科学、高能物理和 DOI 注册元数据可以组合使用；
- 去重但不丢证据：优先按 DOI、arXiv、PMID/PMCID 等强标识合并，标题相似不会被轻率自动合并；
- 引用方向明确：所有规范边统一为 `citing -> cited`；
- 失败可见：区分 `complete`、`partial`、`empty`、`failed`、`throttled` 和 `skipped`；
- Agent 友好：默认返回有界结构化 JSON，并披露来源能力、过滤位置、截断和 provenance；
- 可追溯：可选 SQLite 保存规范结果、原始响应、请求尝试、身份决策和引文证据；
- 接口一致：CLI、API、MCP 都调用同一个 `ScholarService`，不会产生三套业务行为。

当前不会自动下载论文 PDF。结果可能包含 `pdf_url`；用户或 Agent 应根据许可和任务需要显式决定
是否获取全文。CLI/Library 可以处理用户已经合法获得的本地 PDF，并可选使用 GROBID、OCRmyPDF
提取 References。

## 安装

需要 Python 3.11 或更高版本。建议在项目专用虚拟环境中安装：

```powershell
cd D:\github_project\scholarly_retrieval_platform
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[api,mcp]"
```

不同安装命令的含义：

| 命令 | 安装内容 |
|---|---|
| `python -m pip install -e "."` | 核心 Library 与 `scholar` CLI |
| `python -m pip install -e ".[api]"` | 核心 + FastAPI/uvicorn |
| `python -m pip install -e ".[mcp]"` | 核心 + MCP Python SDK |
| `python -m pip install -e ".[api,mcp]"` | 推荐的完整 Agent 接口 |
| `python -m pip install -e ".[dev,api,mcp,distributed]"` | 开发、测试与 Redis worker |
| `python -m pip install -e ".[ocr]"` | Python OCR 依赖；仍需本机 OCR/GROBID 运行时 |

`-e` 表示 editable install：包安装到当前 Python 环境，源码仍指向本仓库。Claude Code/Codex
不必安装在该环境中，但它们启动的 `scholar-mcp` 必须能从 PATH 找到这个环境中的命令。排错时运行：

```powershell
python -c "import sys; print(sys.executable)"
Get-Command scholar
Get-Command scholar-mcp
```

也可以不依赖 console script：

```powershell
python -m scholarly_retrieval --help
python -m scholarly_retrieval search "citation graph" --limit 3
```

正确模块名是 `scholarly_retrieval`，不是 `scholar`。

## 配置

复制配置模板；真实密钥不要提交到 Git：

```powershell
Copy-Item .env.example .env
scholar doctor
scholar providers
```

CLI、API 和 MCP 默认读取当前工作目录的 `.env`。从其他目录启动时设置：

```powershell
$env:SCHOLAR_ENV_FILE = "D:\github_project\scholarly_retrieval_platform\.env"
```

多数来源无需 key。常用可选配置：

| 变量 | 用途 |
|---|---|
| `OPENALEX_API_KEY` / `OPENALEX_MAILTO` | OpenAlex 正式配额与联系邮箱 |
| `SEMANTIC_SCHOLAR_API_KEY` | 提高 Semantic Scholar 稳定性/配额 |
| `SERPAPI_API_KEY` | 启用可选 Google Scholar/SerpApi Provider |
| `ADS_API_TOKEN` | 启用可选 NASA ADS Provider；在 ADS 账户设置中生成 token |
| `OPENCITATIONS_ACCESS_TOKEN` | OpenCitations 推荐 token |
| `SCHOLAR_DB_PATH` | 启用 SQLite 审计、缓存和结果持久化 |
| `SCHOLAR_RUN_LIVE_TESTS=1` | 显式启用真实公网测试 |

`scholar doctor` 只报告凭证是否存在，不显示凭证值。

Semantic Scholar 未配置 key 时使用共享匿名配额，持续 429 即使指数退避也未必恢复。此时检查
`provider_reports[].context.attempt_count` 和 `retry_delays` 可确认实际重试；申请 key 才能获得独立的
初始 1 request/second 配额。重复实验建议同时设置 `SCHOLAR_DB_PATH`，以复用成功响应缓存。

## 五分钟完成四种检索

### 1. 关键词检索

```powershell
scholar search "retrieval augmented generation" --limit 5 --format table
```

关键词检索会从每个来源拉取最多 `limit × 3`（上限 100）的候选，完成跨源去重后，用来源名次 RRF、
标题/摘要词项覆盖和多源一致性做软融合，再截取最终 `limit`。软融合只调整顺序，不按缺词硬删除候选；
做系统综述时应提高 `--limit` 并拆分同义词/缩写查询，不能把单次 Top-N 当作完整召回。

### 2. 高级检索

```powershell
scholar search "large language model agents" `
  --author "Wang" --year-from 2023 --year-to 2026 `
  --open-access --sort newest --limit 10
```

高级条件会在上游 Provider 支持时原生下推，否则在有界召回后本地执行。使用 `query-plan` 可在
不访问网络的情况下查看实际计划：

```powershell
scholar query-plan "citation graph" --source openalex,crossref,openreview
```

### 3. 引用关系检索

```powershell
# seed -> 它引用的论文
scholar references "10.1038/s41586-021-03819-2" --limit 10

# 后来的论文 -> seed
scholar citations "10.1038/s41586-021-03819-2" --limit 10

# 同一论文存在多个 Google Scholar ID 时，核验各 ID 后检索，统一聚合去重
scholar citations "google_scholar:5554083676653175677,10581113726319067053" `
  --source google_scholar_serpapi --limit 100
```

`references` 和 `citations` 提供两种阅读模式，均输出本次返回的全部去重论文，不截断标题：

- `--format compact`（默认）：本次去重后数量、每篇论文的完整名称、论文链接、各来源及其链接。
- `--format detailed`：在精简模式基础上增加年份、作者、期刊/会议及可用 PDF 链接。

按去重后的论文逐篇组织信息，同一篇的多个来源集中列出。缺失链接标为“未提供链接”，不伪造。
两种模式均不显示原始记录数、去重指标、限流或失败原因；诊断证据仍保存在查询运行记录中。
`--limit` 控制实际检索上限，显示的数量是本次检索结果数，不代表全网引用总数。
程序调用仍可显式使用 `--format json` 导出完整结果；旧 `audit` 参数兼容精简模式，
`table` 保留为旧版兼容格式。

关系检索会先把 OpenAlex、Semantic Scholar 等来源专有 seed ID 转译为 DOI/arXiv 等可移植标识，再让
其他来源解析。Google 多 cluster 中某一簇失效时，可用簇的结果仍会返回，整体状态为 `partial`，
详细结果见 `provider_reports[].context.cluster_outcomes`。

### 4. 相关论文检索

```powershell
scholar related --text "reliable citation graph retrieval for research agents" --limit 10
scholar related --positive "10.1038/s41586-021-03819-2" --limit 10
```

### 多跳引文扩展

```powershell
scholar graph expand "10.1038/s41586-021-03819-2" `
  --direction both --depth 1 --frontier-cap 30 --top-k 30 --per-node-limit 10
```

先从 `depth=1` 和小 limit 开始，避免公共 API、运行时间和 Agent 上下文无界增长。

## 数据来源

### arXiv 论文的多源被引聚合

arXiv 页面的 ADS、Google Scholar、Semantic Scholar 是外部索引入口，各自的引用覆盖可能不同。
CLI 会调用配置的数据源，再按论文身份聚合去重；一个来源的计数不是全网总数。

```powershell
# 无需填写 Semantic Scholar key 即可尝试；公开访问仍可能限流
scholar citations "https://arxiv.org/abs/2603.25723" --source openalex,semantic_scholar,google_scholar_serpapi --limit 100

# 在 .env 配置 ADS_API_TOKEN 后，加入 NASA ADS
scholar citations "https://arxiv.org/abs/2603.25723" --source openalex,semantic_scholar,google_scholar_serpapi,ads --limit 100
```

NASA ADS token 在 [ADS 账户设置](https://ui.adsabs.harvard.edu/user/settings/token) 生成，
填入项目 `.env` 的 `ADS_API_TOKEN=`。未配置时不会注册 `ads`。
Google Scholar 解析 seed 后继续检索标题和强标识符，沿匹配记录的 All Versions 发现更多 cluster。
ID 查询为空时也可借助其他来源的 seed 元数据。候选须通过标识符或标题、作者、年份核验；
分别保存 `versions.cluster_id` 与 `cited_by.cites_id`，不假设两者相等，也不写死示例论文 ID。
不同 cites ID 的论文记录全部交给统一聚合层去重，保留各自的获取证据。

被引抓取使用多轮一致性召回：每轮从第一页开始，只跟随 `serpapi_pagination.next`；
每页最多 10 条，`filter=0`，每次设置 `no_cache=true` 并绕过项目本地 HTTP 缓存。
连续两轮完整分页得到相同结果 ID 集合、且不存在已知总数缺口时停止，默认最多 4 轮。
可在 `.env` 设置 `SCHOLAR_GOOGLE_CITATION_ROUNDS=6` 增加预算（至少 2）。
后续请求失败仍保留已获取记录；达到预算、集合不稳定或数量缺口会在机器结果中标记 `partial`。
这不保证枚举 Google 内部全部 cluster，也不保证拿到网页标称的全部被引；上游可能持续漏返。
相比单轮查询会增加 SerpApi 配额消耗，首次执行建议选择少量 seed。
显式 `google_scholar:ID1,ID2` 是用户指定的被引列表提示：无法通过 All Versions 核验的
ID 仍会尝试抓取，但保留 `caller_supplied_not_verified` 标记，不伪装成自动发现的同一论文身份。
请只传入已人工确认属于同一 seed 的 ID；自动搜索得到但身份不匹配的候选不会加入。

`--limit` 通常是每个来源的上限；Google 多 cites ID 时是**每个 cites ID 的上限**，
不会因第一个列表达到 limit 而跳过其他列表。多源去重后的总数可能超过 limit。
来源报告与匹配证据可通过 `--format json` 检查，精简阅读模式不展示调试信息。

维护者可以运行官方分页对照诊断（消耗 SerpApi 额度；默认最多 20 次 search 请求）：

```powershell
python tools/diagnose_scholar_pagination.py --cites 5554083676653175677 --rounds 2 --max-pages 5 --live
```

该命令绕过本地缓存，对照官方 next 与项目参数重建，输出逐页请求条件、search_id 和论文 ID，
不输出 API key。`--mode official --filter default` 可仅测官方默认过滤。
普通用户继续使用 `scholar citations`；诊断结果及已确认的上游边界见测试验收文档。

Registry 默认包含 12 个开放来源；配置 `SERPAPI_API_KEY` 和 `ADS_API_TOKEN` 后分别启用
Google Scholar/SerpApi 和 NASA ADS，最多 14 个来源。

| Provider 名 | Search | Resolve | References | Citations | 默认 key |
|---|---:|---:|---:|---:|---:|
| `openalex` | 是 | 多种 ID | list | list | 否，生产推荐 key |
| `openaire` | 是 | DOI/arXiv/PMID/OpenAIRE | none | none | 否 |
| `semantic_scholar` | 是 | 多种 ID | list | list | 否，推荐 key |
| `crossref` | 是 | DOI | deposited list | count | 否 |
| `datacite` | 是 | DOI | none | count | 否 |
| `dblp` | 是 | DOI/DBLP key | none | none | 否 |
| `acl_anthology` | 是 | ACL ID/URL/DOI | none | none | 否 |
| `arxiv` | 是 | arXiv ID | none | none | 否 |
| `openreview` | 是 | OpenReview ID/URL/DOI 查询 | none | none | 否 |
| `europe_pmc` | 是 | DOI/PMID/PMCID | list | list | 否 |
| `inspire` | 是 | DOI/arXiv/recid | list | list | 否 |
| `opencitations` | none | DOI/PMID/OMID | list | list | 否，推荐 token |
| `google_scholar_serpapi` | 是 | cluster/查询 | none | list | 是 |
| `ads` | 是 | DOI/arXiv/bibcode | list | list | 是 |

`list` 表示能返回可遍历论文列表，`count` 只表示来源提供计数，`none` 表示不支持。系统不会把
count 伪装成引用边。ACL Anthology 没有公开 REST 搜索 API，因此先用 DBLP 官方 API 发现有界候选，
再用 ACL 官方 MODS XML 校验，并在 `retrieval_context` 披露该路径。

按领域选源示例：

```powershell
scholar search "BERT pre-training" --source acl_anthology,dblp,arxiv --limit 10
scholar search "vision transformer" --source openreview,openalex --limit 10
scholar search "gauge gravity duality" --source inspire,openalex,arxiv --limit 10
scholar search "cancer immunotherapy" --source europe_pmc,openalex,crossref --limit 10
```

## 如何理解返回结果与模型上下文

MCP/CLI 工具调用的有界结果通常会进入 Agent 当前上下文；API 和 Python Library 是否进入上下文由
调用者决定。SQLite 中的历史结果和 Provider 原始响应不会自动进入模型上下文。

Agent 应重点读取：

- `papers`：聚合去重后的规范论文；
- `identifiers`：DOI、arXiv、PMID、ACL/OpenReview 等标识及来源；
- `source_records`：哪些 Provider 命中过该记录；
- `field_claims`：不同来源对字段给出的原值；
- `provider_reports`：每个来源的状态、数量、过滤位置、截断和 cursor；
- `assertions` / `edges`：底层引文声明与聚合后的 `citing -> cited` 边；
- `identity_decisions`：合并、冲突或待人工判断的证据；
- `fingerprint`：排除获取时间后的稳定结果指纹。

推荐先返回 5～10 篇基本信息，选定少数论文后再 `resolve`、查引用或处理全文。不要一次请求几百篇
摘要或大规模多跳图。

## CLI

CLI 是一次命令、一次结果，不监听端口。完整参数以 `scholar COMMAND --help` 为准。

| 命令 | 作用 |
|---|---|
| `providers` / `doctor` / `query-plan` | 来源、配置和执行计划诊断 |
| `search` / `resolve` / `related` | 论文发现与身份解析 |
| `references` / `citations` / `graph expand` | 引文关系与局部图 |
| `extract-references` / `link-references` | 本地正文书目抽取与实体链接 |
| `export` | 输出 JSONL、CSV、RIS、BibTeX |
| `maintenance` / `citation-evidence` | SQLite 清理与引文证据回放 |
| `review list/decide/revert` | 身份灰区人工决策与回滚 |
| `evaluate*` / `validate-reference-smoke` | 检索、实体、关系和 Reference 评测 |

`search`、`resolve` 和 `related` 默认输出 JSON，也可用 `--format table` 供人工浏览；`references` 和
`citations` 默认输出 `compact`，可用 `--format detailed` 阅读更多论文字段。程序调用应显式使用 `--format json`，并同时检查进程
退出码、领域 `status` 和 `provider_reports`。

## Python Library

```python
import asyncio

from scholarly_retrieval import ScholarService, SearchQuery


async def main() -> None:
    service = ScholarService()
    try:
        result = await service.search(
            SearchQuery(text="citation graph", year_from=2020, limit=5),
            sources=["openalex", "crossref"],
        )
        print(result.model_dump_json(indent=2))
    finally:
        await service.close()


asyncio.run(main())
```

## HTTP API

安装并启动常驻服务：

```powershell
python -m pip install -e ".[api]"
scholar-api
```

默认地址为 `http://127.0.0.1:8000`：

- 健康检查：`GET /health`
- OpenAPI UI：`GET /docs`
- 机器规范：`GET /openapi.json`
- 核心路由：`/v1/providers`、`/v1/search`、`/v1/resolve`、`/v1/related`、
  `/v1/references`、`/v1/citations`、`/v1/graph/expand`
- Reference：`/v1/references/extract`、`/v1/references/link`
- 异步图任务：`POST /v1/jobs/expand`、`GET/DELETE /v1/jobs/{job_id}`

```powershell
$body = @{ text = "citation graph"; year_from = 2020; limit = 5 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/search `
  -ContentType application/json -Body $body
```

设置 `SCHOLAR_API_KEYS` 后，请求必须带 `X-API-Key` 或 Bearer token。远程部署必须使用 TLS、反向代理、
请求体限制和配额；不要将默认无鉴权服务暴露到公网。

## MCP

MCP 让 Claude Code、Codex 等 Agent 使用结构化工具。它暴露：

`providers`、`search`、`resolve`、`related`、`references`、`citations`、`expand`、
`extract_references`、`link_references`。

### 本地 stdio（推荐）

Agent 客户端负责启动 `scholar-mcp`，不需要手动常驻服务器：

```json
{
  "mcpServers": {
    "scholarly-retrieval": {
      "command": "scholar-mcp"
    }
  }
}
```

仓库根目录已提供 `.mcp.json`。直接在终端运行 `scholar-mcp` 会等待 MCP 握手，看起来没有输出是
正常现象。

### Streamable HTTP（可选）

```powershell
$env:SCHOLAR_MCP_TRANSPORT = "streamable-http"
$env:SCHOLAR_MCP_HOST = "127.0.0.1"
$env:SCHOLAR_MCP_PORT = "8001"
scholar-mcp
```

端点为 `http://127.0.0.1:8001/mcp`。远程使用时配置 `SCHOLAR_MCP_API_KEYS`、TLS、issuer/resource
URL 和调用配额。

## Claude Code 配置

从项目根目录注册并检查：

```powershell
claude mcp add --scope project scholarly-retrieval -- scholar-mcp
claude mcp get scholarly-retrieval
```

预期为 `Connected`。非交互最小测试：

```powershell
claude -p "必须使用 scholarly-retrieval MCP：先调用 providers，再只用 openalex 搜索 AlphaFold protein structure prediction，limit=3；报告 status、provider_reports 和标题。不要使用 Bash 或网页搜索。" `
  --allowedTools "mcp__scholarly-retrieval__providers,mcp__scholarly-retrieval__search" `
  --permission-mode dontAsk --max-budget-usd 1 --output-format json `
  --no-session-persistence
```

`--allowedTools` 中的 server 名必须与 `.mcp.json` 完全一致；本项目名称含连字符，不能写成
`mcp__scholarly_retrieval__...`。交互模式也可以在 Claude Code 首次询问时逐项批准工具。

安装 Skill 到 Claude Code 项目：

```powershell
New-Item -ItemType Directory -Force .claude\skills\scholarly-research | Out-Null
Copy-Item -Recurse -Force skills\scholarly-research\* .claude\skills\scholarly-research
```

重新启动 Claude Code 后可使用 `/scholarly-research`。Skill 不保存密钥。

## Codex 配置

```powershell
codex mcp add scholarly-retrieval -- scholar-mcp
codex mcp get scholarly-retrieval
```

项目级配置也可以写入 `.codex/config.toml`：

```toml
[mcp_servers.scholarly-retrieval]
command = "scholar-mcp"
```

Codex 项目 Skill 安装路径是 `.agents/skills`：

```powershell
New-Item -ItemType Directory -Force .agents\skills\scholarly-research | Out-Null
Copy-Item -Recurse -Force skills\scholarly-research\* .agents\skills\scholarly-research
```

## 本地 PDF 与 Reference 提取

系统不自动下载 PDF。对已经合法取得的文件：

```powershell
# JATS、TEI、LaTeX、BibTeX 文本
scholar extract-references article.xml --input-format jats

# born-digital PDF，需要用户运行 GROBID
scholar extract-references article.pdf --input-format pdf `
  --grobid-url http://127.0.0.1:8070

# 扫描/混合 PDF，先本地 OCRmyPDF，再进入 GROBID
scholar extract-references scan.pdf --input-format pdf --ocr `
  --ocr-language eng+chi_sim --grobid-url http://127.0.0.1:8070
```

PDF 输入/输出上限为 100 MiB。生产环境应把 PDF 当作不可信输入，在隔离、限时、限 CPU/内存的
worker 中处理，不得绕过付费墙、验证码或访问控制。

## 数据保存

默认不持久化，也不下载 PDF。设置：

```dotenv
SCHOLAR_DB_PATH=.scholarly-retrieval/state.sqlite3
```

后会保存：

- `query_runs`：请求、规范化结果、状态和 fingerprint；
- `raw_responses` / `http_cache`：脱敏 URL、原始响应和缓存；
- `provider_attempts` / `cursor_checkpoints`：重试与分页恢复；
- `identity_events`：人工 merge/split/defer/revert；
- `citation_assertions` / `visible_citation_edges`：底层引文声明与可见边；
- `citation_edge_status_events`：证据升级历史。

SQLite 适合单机单库。运行中可能出现 `-wal` 和 `-shm` 文件，热备份不能只复制主文件。清理先预览：

```powershell
scholar maintenance --retention-days 30 --max-raw-responses 10000
scholar maintenance --retention-days 30 --max-raw-responses 10000 --apply
```

## 部署

```powershell
docker build -t scholarly-retrieval .
docker run --rm -p 8000:8000 -v scholar-data:/data `
  -e SCHOLAR_API_KEYS=replace-with-a-secret scholarly-retrieval
```

可选 `compose.distributed.yaml` 使用 Redis 共享 API 配额、图任务队列、结果和取消状态。它不共享
SQLite 长期证据库；多副本长期审计仍需要未来的服务型数据库。OCR 环境使用 `compose.ocr.yaml`，
需要可工作的 Docker/Podman engine 和至少约 4 GiB 内存。

## 常见问题

### Bash 无法进入 Windows 路径

Git Bash 中不要使用裸 `C:\Users\...`：

```bash
cd /d/github_project/scholarly_retrieval_platform
```

### `No module named scholar`

模块名不是 `scholar`。使用已安装命令或正确模块：

```powershell
scholar search "query"
python -m scholarly_retrieval search "query"
```

### Agent 找不到 `scholar-mcp`

Agent 启动的进程没有继承安装环境的 PATH。可在 MCP 配置中使用该环境 Python 的绝对路径：

```json
{
  "command": "D:\\path\\to\\python.exe",
  "args": ["-m", "scholarly_retrieval.mcp_server"]
}
```

### 某些来源失败但仍有结果

这是正常的多源 `partial`。检查 `provider_reports` 中的 `failed`、`throttled`、`skipped` 和
`truncated`，不要把部分覆盖描述成完整覆盖。

## 开发与测试

```powershell
python -m ruff check src tests
python -m compileall -q src tests
python -m pytest -q
```

真实公网测试需要显式设置 `SCHOLAR_RUN_LIVE_TESTS=1`。完整架构、模块、设计决策和数据表见
[技术实现与可溯源文档](docs/technical-implementation.md)；测试矩阵、真实案例和 Claude Code
验收见[测试验收文档](docs/test-acceptance.md)。

仓库中的两个测试资料目录职责不同：

- `benchmarks/` 保存机器可读、版本化的稳定验收案例，自动化测试会读取它；当前内容是公开真实数据的
  Reference pipeline smoke 快照，不是性能跑分，也不是人工标注 gold；
- `manual-tests/` 保存人工选择论文后得到的检查结果，供人复核真实检索效果；第三方论文 PDF 只在本地
  使用并由 `.gitignore` 排除，仓库保存论文链接、许可信息和可发布的结果文件。

## 文档

本项目只维护三份面向人的主文档，避免内容漂移：

1. 本 `README.md`：安装、配置和使用说明；
2. [技术实现与可溯源文档](docs/technical-implementation.md)：架构、模块、算法、存储与设计决策；
3. [测试验收文档](docs/test-acceptance.md)：自动化、真实接口与 Agent smoke 验收。

`skills/scholarly-research/SKILL.md`、`.mcp.json` 和 `.github` 模板是运行/自动化配置，不作为重复的
项目说明文档维护。

## 许可证

本项目以 [MIT License](LICENSE) 开源。论文元数据、摘要、全文链接和其他上游内容仍分别受对应
Provider 条款及原始作品许可证约束；本项目的软件许可证不会改变第三方数据或论文的授权范围。
