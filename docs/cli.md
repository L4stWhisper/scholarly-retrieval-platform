# CLI 指南

CLI 是一次命令、一次结果，不监听端口。完整参数以 `scholar COMMAND --help` 为准。使用 uv 时在命令
前加 `uv run`，或先激活 `.venv`。

## 四种检索

### 1. 关键词检索

```bash
scholar search "retrieval augmented generation" --limit 5
```

关键词检索会从每个来源拉取最多 `limit × 3`（上限 100）的候选，完成跨源去重后，用来源名次 RRF、
标题/摘要词项覆盖（含轻量词干归一，IDF 平方加权让稀有主题词主导）和多源一致性做软融合，再截取
最终 `limit`。软融合只调整顺序，不按缺词硬删除候选。补充材料（`component`）、同行评审报告、
资助记录、勘误与撤稿声明这类登记记录会被排除，因为它们不是论文；被撤稿的论文仍会返回但排名降半，
显式指定 `--work-type` 时不做这些排除。

做系统综述时应提高 `--limit` 并拆分同义词/缩写查询，不能把单次 Top-N 当作完整召回。

### 2. 高级检索

```bash
scholar search "large language model agents" \
  --author "Wang" --year-from 2023 --year-to 2026 \
  --open-access --sort newest --limit 10
```

高级条件在上游来源支持时原生下推，否则在有界召回后本地执行。`query-plan` 可在不访问网络的
情况下查看实际执行计划：

```bash
scholar query-plan "citation graph" --source openalex,crossref,openreview
```

### 3. 引用关系检索

```bash
# seed -> 它引用的论文
scholar references "10.1038/s41586-021-03819-2" --limit 10

# 后来的论文 -> seed
scholar citations "10.1038/s41586-021-03819-2" --limit 10

# 同一论文存在多个 Google Scholar ID 时，核验各 ID 后检索，统一聚合去重
scholar citations "google_scholar:5554083676653175677,10581113726319067053" \
  --source google_scholar_serpapi --limit 100
```

关系检索会先把 OpenAlex、Semantic Scholar 等来源专有 seed ID 转译为 DOI/arXiv 等可移植标识，再让
其他来源解析。某个来源或某个 Google cluster 失效时，其他来源的结果仍会返回，整体状态为 `partial`。

`--limit` 通常是每个来源的上限；Google 多 cites ID 时是每个 cites ID 的上限。多源去重后的总数可能
超过 limit。所有来源的原始记录在多源检索完成后统一去重，不会对单个来源提前去重。

### 4. 相关论文检索

```bash
scholar related --text "reliable citation graph retrieval for research agents" --limit 10
scholar related --positive "10.1038/s41586-021-03819-2" --limit 10
```

## 结果阅读模式

`search`、`related`、`references` 和 `citations` 提供两种阅读模式，均输出本次返回的全部去重论文，
不截断标题；`search` 会先打印检索词，`related` 打印种子论文，`references`/`citations` 打印目标论文：

- `--format compact`（默认）：去重后数量、每篇论文的完整名称、论文链接、各来源及其链接；
- `--format detailed`：在精简模式基础上增加年份、作者、期刊/会议及可用 PDF 链接。

同一篇论文的多个来源集中列出，缺失链接标为"未提供链接"，不伪造。两种模式不显示原始记录数、去重
指标、限流或失败原因；程序调用应显式使用 `--format json`，并同时检查进程退出码、`status` 和
`provider_reports`。

`resolve` 默认输出 JSON。`--format table` 是保留的旧版单行表格，会截断标题与来源，仅用于兼容。

## 多跳引文扩展

```bash
scholar graph expand "10.1038/s41586-021-03819-2" \
  --direction both --depth 1 --frontier-cap 30 --top-k 30 --per-node-limit 10
```

先从 `depth=1` 和小 limit 开始，避免公共 API、运行时间和 Agent 上下文无界增长。

## 如何理解返回结果

`--format json` 的结果中，Agent 与程序应重点读取：

- `papers`：聚合去重后的规范论文；
- `identifiers`：DOI、arXiv、PMID、ACL/OpenReview 等标识及来源；
- `source_records`：哪些来源命中过该记录；
- `field_claims`：不同来源对字段给出的原值；
- `provider_reports`：每个来源的状态、数量、过滤位置、截断、重试与 cursor；
- `assertions` / `edges`：底层引文声明与聚合后的 `citing -> cited` 边；
- `identity_decisions`：合并、冲突或待人工判断的证据；
- `fingerprint`：排除获取时间后的稳定结果指纹。

推荐先返回 5～10 篇基本信息，选定少数论文后再 `resolve`、查引用或处理全文。

## 全部命令

| 命令 | 作用 |
|---|---|
| `providers` / `doctor` / `query-plan` | 来源、配置和执行计划诊断 |
| `search` / `resolve` / `related` | 论文发现与身份解析 |
| `references` / `citations` / `graph expand` | 引文关系与局部图 |
| `extract-references` / `link-references` | 本地正文书目抽取与实体链接，见 [Reference 提取](references-and-pdf.md) |
| `export` | 输出 JSONL、CSV、RIS、BibTeX |
| `maintenance` / `citation-evidence` | SQLite 清理与引文证据回放，见 [存储与部署](storage-and-deployment.md) |
| `review list/decide/revert` | 身份灰区人工决策与回滚 |
| `evaluate*` / `validate-reference-smoke` | 检索、实体、关系和 Reference 评测 |
