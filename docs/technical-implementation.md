# 技术实现与可溯源文档

本文是 Scholarly Retrieval Platform 的唯一技术说明，记录当前架构、模块、数据契约、算法、外部
来源、可靠性、安全边界、部署方式和关键设计决策。用户操作统一放在根目录 `README.md`，测试证据
统一放在 `docs/test-acceptance.md`。

基线版本：`0.1.0`；文档核对日期：2026-09-03。

## 1. 目标与边界

### 多源 citation seed 解析与 NASA ADS

Provider 新增可选 `resolve_seed(Paper)` 与 `traversal_identifier(Paper, fallback)` 钩子。
关系检索先做输入 ID 解析、可移植 ID 回退；仅在空结果时尝试元数据核验，
429、鉴权及其他服务故障不触发额外标题检索。Google Scholar 对官方明确的零结果响应返回空批次；
标题候选以强标识符或规范化标题＋作者姓氏＋相容年份核验，冲突标识符排除，
Google 即使直接解析成功也继续发现别名：标题/强标识符检索、已核验记录的 All Versions，
以有界队列遍历。`versions.cluster_id` 和 `cited_by.cites_id` 分别保存于 SourceRecord 的
retrieval_context；只把明确返回的 cites ID 用作被引列表入口。显式输入的多个 cluster 也逐个
核验，不盲目将调用者给出的 ID 声明为同一论文。显式 numeric ID 同时是调用者指定的
cites 列表提示：无法核验时仍可遍历，但 SourceRecord 标记 caller_supplied_not_verified，
不创建已核验的标识符 claim；自动候选不能走此例外。这里没有针对某篇论文的特殊常量。

SerpApi citation 使用多轮一致性召回（2026-09-16 替代固定偏移探测）：

1. 每个 cites ID 独立运行，默认最多 4 轮，每轮从 start=0 开始。
2. 只跟随 `serpapi_pagination.next`，不从 total_results 推算页数，也不自行推算下一页偏移。
   下一页必须是官方 HTTPS search.json、保持 seed/query/engine 且偏移递增；忽略 URL 中的密钥，
   保留本地授权。每轮最多 ceil(limit/10)+2 页，防止异常分页无界消耗配额。
3. 所有轮次均 `no_cache=true`，ReliableHttpClient 同时 use_cache=False；请求仍写审计记录。
4. 同一 cites ID 内反复观察到的相同 result_id 合并为一条抓取记录，累积轮次/页偏移证据。
   不同 cites ID 的记录不提前去重，全部交给 ScholarService 的身份解析与多源去重。
5. 连续两轮完整结果 ID 集合相等、且累计返回量不少于历页最大报告总数，才结束为 stable。
   轮数/结果预算、失败、非法 next、已知缺口均返回 partial/truncated 并保留已成功的论文。

`citation_rounds`（至少 2，未传入时读取 SCHOLAR_GOOGLE_CITATION_ROUNDS，默认 4）、
`discovery_pages`（默认 12）可在 Provider 构造时调整；
`limit` 对 Google 按 cites ID 应用，而不是裁剪所有列表拼接结果。
`provider_reports[].context.cluster_outcomes` 保存每个 cites ID 的 rounds/pages/stop_reason；
多列表重叠未知，因此不将其总数相加冒充去重总数。seed 的 SourceRecord 保存发现预算/错误，
发现范围仅为可见且经核验的候选，不保证穷尽 Google 内部索引。
每页附带 search_metadata.id，可用官方 Search Archive API 比较同一请求的 JSON 与 HTML。
独立 CLI 诊断工具 tools/diagnose_scholar_pagination.py 不经过聚合/缓存，保留 next 查询参数并
与项目分页构造对照。2026-09-16 的真实对照发现两者实际参数一致，但原始 HTML 也出现缺页；
因此当前多轮机制属于有界召回补偿，不是稳定快照或全量保证。具体证据见测试验收文档。
官方依据：[Google Scholar API](https://serpapi.com/google-scholar-api) 的 cluster、cites、
serpapi_pagination、no_cache 参数。绕过缓存的请求消耗正常查询额度，需控制 seed 数量。

NASA ADS 使用官方 `/v1/search/query`，通过 `citations(query)` 与 `references(query)`
区分被引和参考文献方向，按 start/rows 分页并保留 numFound 和截断状态。
bibcode 是来源记录 ID，DOI/arXiv claims 进入既有身份去重流程，source_url 保留 ADS 页面。
官方接口与授权依据：[ADS API](https://github.com/adsabs/adsabs-dev-api)、
[引用运算符](https://adsabs.github.io/help/search/citations-and-references)。

CLI 关系检索的阅读层提供 `compact` 与 `detailed`：从服务层已去重的
`papers` 全量渲染，以完整论文标题组织来源及 `source_url`。详细模式补充年份、
作者、venue 和 PDF；不在显示层按相同标题再次合并，避免误合并不同论文。
内部来源报告、身份决策及错误上下文仍进入原有持久化和 JSON 数据契约。
终端数量仅表示本次返回的去重结果，受请求 limit 和上游覆盖影响。

系统目标是给研究者和 Agent 提供可安装、可审计的论文检索内核：

1. 统一关键词、高级条件、引用关系和相关性检索；
2. 聚合多个覆盖范围、能力、限流和许可不同的 Provider；
3. 用强标识保守去重，保留字段冲突和来源；
4. 用同一 Library 支撑 CLI、HTTP API 和 MCP；
5. 对失败、截断、分页、引文方向和证据等级做机器可读披露；
6. 可选保存检索运行、原始响应、身份决策和引文证据。

当前非目标：

- 不保证对任一学科或 Provider 的全库召回；
- 不抓取 Google Scholar、ACL 搜索结果页或绕过验证码/付费墙；
- 不自动下载和保存论文 PDF；
- 不把不同 Provider 的 citation count 相加；
- 不把标题相似直接当成同一论文；
- 不把一次公网 smoke 当成科学准确率结论；
- Redis 多副本模式不等同于已完成共享长期证据数据库。

## 2. 总体架构

```text
Researcher / Script           Claude Code / Codex            Remote client
         |                            |                            |
        CLI                     Skill -> MCP                  HTTP API
         |                            |                            |
         +----------------------------+----------------------------+
                                      |
                               ScholarService
                                      |
          +---------------------------+---------------------------+
          |                           |                           |
   Query/Result Models         Provider Registry          Identity / Graph
          |                           |                           |
          +------------------- Provider adapters ----------------+
                                      |
                       Reliable HTTP + optional SQLite
                                      |
                         Public/licensed scholarly APIs
```

核心原则是 library-first：`ScholarService` 是唯一业务编排层；CLI、API 和 MCP 只完成输入校验、传输、
鉴权和序列化。Skill 只指导 Agent 选工具、控制预算和解释证据。

一次搜索的数据流：

```text
SearchQuery
   -> capability/filter execution plan
   -> bounded concurrent provider calls
   -> provider payload normalization
   -> cross-source identity resolution
   -> local exact filters and deterministic sort
   -> SearchResult + reports + evidence + fingerprint
   -> optional query-run/raw-response persistence
```

## 3. 仓库和模块职责

### 3.1 核心包

| 文件 | 责任 |
|---|---|
| `models.py` | Provider-neutral Pydantic 输入、论文、实体、证据、图和结果模型 |
| `service.py` | search/resolve/related/references/citations/expand/link 的唯一编排层 |
| `providers/base.py` | `ScholarlyProvider` 抽象接口、能力清单、预期 Provider 错误 |
| `providers/registry.py` | 默认 Provider 工厂、顺序和可选凭证 gating |
| `normalization.py` | DOI、arXiv、OpenAlex、OpenReview、ACL Anthology 标识规范化 |
| `identity.py` | 强标识 must-link、冲突 cannot-link、灰区 review 和字段合并 |
| `entities.py` | Paper 到 Manifestation、Version、WorkFamily、Artifact 的投影 |
| `query_language.py` | Provider-neutral AND/OR/NOT/phrase 字段表达式本地求值 |
| `reliability.py` | HTTP 缓存、并发/节奏、Retry-After、退避、熔断和 URL 脱敏 |
| `storage.py` | SQLite schema、迁移、raw/cache/run/checkpoint/identity/citation 持久化 |
| `fingerprint.py` | 排除获取时间等易变字段后的确定性结果指纹 |
| `reference_extraction.py` | JATS、TEI、LaTeX、BibTeX、PDF/GROBID、OCRmyPDF 提取 |
| `reference_matching.py` | DOI 或题名/作者/venue/年份候选评分与阈值决策 |
| `reference_smoke.py` | 公共 JATS 功能 smoke 与多源 linking 验证 |
| `benchmark.py` | 版本化检索 gold、指标与 bootstrap 区间 |
| `reference_benchmark.py` | 分层 Reference 提取/候选/link/edge 评测 |
| `evaluation.py` | retrieval、edge、cluster、relation 等指标 |
| `exporters.py` | JSONL、CSV、RIS、BibTeX 导出 |
| `distributed.py` | Redis 图任务、共享状态、取消和固定窗口配额 |

### 3.2 接口与运行模块

| 文件 | 责任 |
|---|---|
| `cli.py` / `__main__.py` | Typer CLI、JSON/table 输出、文件输入和用户错误 |
| `api.py` | FastAPI、OpenAPI、鉴权、请求体限制、同步/异步图路由 |
| `mcp_server.py` | FastMCP stdio/Streamable HTTP、工具 schema、bearer verifier、配额 |
| `worker.py` | Redis 队列图扩展 worker 和取消检查 |
| `config.py` | `.env` 加载与不泄露密钥值的诊断 |

### 3.3 部署与 Agent 工件

| 路径 | 责任 |
|---|---|
| `pyproject.toml` | 构建元数据、依赖 extras、console scripts、pytest/ruff 配置 |
| `.mcp.json` | Claude Code 项目级 stdio MCP 配置 |
| `skills/scholarly-research/SKILL.md` | Agent 检索路由、预算和证据解释规则 |
| `Dockerfile` | 普通 HTTP API 镜像 |
| `Dockerfile.ocr` / `compose.ocr.yaml` | OCRmyPDF + Tesseract + GROBID 可复现实验环境 |
| `compose.distributed.yaml` | Nginx、API replicas、workers、Redis 的可选部署切片 |
| `.github/workflows/ci.yml` | Python 版本矩阵静态与离线测试 |
| `benchmarks/` | 自动化测试读取的版本化真实数据 smoke 案例；不是性能跑分或人工 gold |
| `manual-tests/` | 人工选择论文的可复核结果与来源清单；第三方 PDF 不进入 Git |

## 4. 核心领域模型

### 4.1 Paper 与来源声明

`Paper` 是当前规范视图，不是对世界的唯一事实。主要字段：

- 书目：`title`、`abstract`、`authors`、`publication_date/year`、`venue`、`work_type`；
- 检索：`fields_of_study`、`language`、`open_access`；
- 访问：`landing_page_url`、`pdf_url`；
- 身份：`identifiers[]`，每条有 scheme、value、authority、confidence、provenance；
- 计数：`citation_counts[]`，每个 Provider 独立；
- 来源：`source_records[]`，含 Provider record ID、URL、rank、method、context 和时间；
- 冲突证据：`field_claims` 保存各来源原值，`field_provenance` 保存规范字段依据。

标识 scheme 当前包括 DOI、arXiv、PMID、PMCID、OpenAlex、Semantic Scholar、Crossref、Google
Scholar、DBLP、OpenCitations OMID、OpenAIRE、INSPIRE、OpenReview 和 ACL Anthology。

### 4.2 运行状态

| 状态 | 语义 |
|---|---|
| `complete` | 至少一个适用分支完成并有可用结果 |
| `partial` | 有可用结果，但至少一个适用分支失败或覆盖不完整 |
| `empty` | 已成功执行但没有结果 |
| `failed` | Provider/映射/输入以外的分支失败 |
| `throttled` | 上游明确限流 |
| `skipped` | Provider 能力声明不支持该操作 |
| `degraded` | 保留给可用但降级的执行状态 |

`ProviderReport` 保存 operation、raw retrieved count、unresolved count、total、truncated、next cursor、
错误码、filter execution 和图层上下文。调用者不能只根据 HTTP 200 或 CLI exit code 判断完整性。

### 4.3 实体层

系统保留四个视角：

- Manifestation：某个可观察发表/预印本记录；
- Version：可显式关联的版本；
- WorkFamily：同一研究工作的版本族；
- Artifact：landing page 或 PDF URL。

没有 provider 或人工证据时，单例 grouping 表示“未知关系”，不是“已经验证没有其他版本”。

## 5. 四种检索和图功能

### 5.1 关键词与高级检索

`SearchQuery` 支持 text、递归 expression、year range、author、OA、title、abstract、venue、field、
work types、minimum citations、sort 和 1～100 limit。

每个过滤条件必须报告执行位置：

- `provider`：编译到上游 API；
- `local`：有界召回后在规范 Paper 上执行；
- `unsupported`：无法可靠执行。

所有关键词查询都会把 Provider 候选窗口放大到请求 limit 的三倍，上限仍为 100；高级字段继续在
规范层二次校验。`sort=relevance` 使用来源名次 RRF、标题/摘要/venue/field 的 IDF 词项覆盖、原短语
命中和多源一致性做确定性软融合。融合不按缺词硬删除候选，以避免用 precision 换掉 recall；但有界
Top-N 仍不保证全库 recall。arXiv 将多词文本编译成顺序无关的 AND 词项，而不是要求整句精确短语。

### 5.2 Resolve

`resolve(identifier)` 是身份解析，不是把 ID 当普通关键词。所有选择的、声明 `resolve_id` 的
Provider 尝试精确解析，随后进入同一个身份层。References/Citations 查询也先 resolve seed；如果输入
是某来源专有 ID，服务会从已解析 seed 选择 DOI、arXiv、PMID 等可移植标识，再让起初返回 empty 的
list-capable Provider 重试解析。限流/运行时失败不会用别名放大重试。实际遍历使用该 Provider 成功
解析 seed 时采用的标识，而不是把 OpenAlex ID 原样传给 Semantic Scholar。

### 5.3 References 与 Citations

关系能力为 `list`、`count` 或 `none`：

- References：seed 引用的工作，边为 `seed -> referenced`；
- Citations：引用 seed 的工作，边为 `citing -> seed`；
- 所有可见边统一保持 `citing -> cited`；
- count-only 来源只增加计数 claim，不生成论文或边；
- 无强标识 deposited reference 进入 `UnresolvedReference`，不会被静默丢弃。
- Google Scholar 各 cites ID 独立多轮分页，跨 ID 重复记录保留到统一身份聚合层；单列表失败时
  保留其他列表并返回 `partial` 和逐列表诊断。

每条 `CitationAssertion` 保存 Provider、source record、evidence type 和 verification status。多来源
观察到同一端点时折叠为稳定 `VisibleCitationEdge`，但保留全部 assertions。

证据类型：

- `provider_graph`：数据库图关系；
- `depositor_metadata`：出版方提交书目；
- `fulltext_anchor`：正文引用锚点。

数据库断言初始为 `provider_asserted`；正文 anchor 可单向升级到 `verified`，较弱证据不能降级已验证边。

### 5.4 Related

Related 接受自然语言、正种子和负种子：

- OpenAlex 使用 semantic search；
- Semantic Scholar 使用 Recommendations 多正/负样本 POST；
- 候选先做跨源身份解析，再用 Reciprocal Rank Fusion；
- 每个 RRF 分项保存 provider、method、原始 rank/score 和 seed context；
- 负种子先 resolve，再对规范实体做硬排除。

RRF 分数只用于当前候选融合，不是校准概率。

### 5.5 有预算的局部引文图

`GraphExpansionQuery` 支持最多 20 个 seed、references/citations/both、depth 1～3、frontier cap、
Top-K、per-node limit、年份/类型候选过滤和运行时间预算。

算法使用确定性 BFS：每层完整获取、身份合并、过滤和截断后保存 checkpoint。被过滤或预算丢弃的
节点、路径和 assertion 不形成悬空边。输出包含：

- 每条 seed 到目标的 discovery path；
- truncation reasons 和 filter exclusions；
- provider assertions 与规范可见边；
- 本次有界图内的共引、文献耦合；
- damping=0.85、50 次迭代的确定性 PageRank；
- 完整层 checkpoint/resume。

这些排名和相似度只描述本次预算图，不代表全库统计。

## 6. 身份合并与去重

自动 must-link 的强标识是 DOI、arXiv、PMID 和 PMCID。相同 Provider record ID 也可 exact-link。

规则顺序：

1. 应用仍有效的人工 merge/split/defer 事件；
2. 相同 record ID 合并；
3. 共享强标识且没有强标识冲突时合并；
4. 强标识冲突生成 cannot-link；
5. 题名、作者、年份相似只生成 review candidate，不自动合并；
6. 聚类合并前检查传递性冲突，防止桥接记录把矛盾 DOI/PMID 合入同一簇。

合并不是“首条胜出”：identifiers、source records、counts、field claims 和 provenance 求并集；规范字段
保持确定性优先级。人工事件可以持久化、回滚并在以后运行中重放。

## 7. Provider 实现与能力

| Provider | 领域/角色 | Search | Resolve | Ref | Cite | 接入事实 |
|---|---|---:|---:|---:|---:|---|
| OpenAlex | 综合开放图谱 | 是 | 是 | list | list | cursor、referenced works、`cites:`、semantic |
| OpenAIRE | 开放仓储/项目/长尾 | 是 | 是 | none | none | Graph API v3、cursor/page |
| Semantic Scholar | 综合图与推荐 | 是 | 是 | list | list | graph/recommendations、key 可选但推荐 |
| Crossref | DOI 注册元数据 | 是 | DOI | list | count | deposited ref 不保证完整；citing 仅 count |
| DataCite | 数据集/软件 DOI | 是 | DOI | none | count | CC0 元数据为主 |
| DBLP | 计算机科学书目 | 是 | DOI/key | none | none | 搜索 JSON、单记录 XML |
| ACL Anthology | NLP 权威书目 | 是 | ID/URL/DOI | none | none | DBLP 候选 + ACL 官方 MODS XML 校验 |
| arXiv | 预印本 | 是 | arXiv ID | none | none | Atom、版本、分类、DOI/PDF bridge |
| OpenReview | ML 投稿/版本 | 是 | ID/URL/DOI 查询 | none | none | API v2，只取 forum notes |
| Europe PMC | 生命科学 | 是 | DOI/PMID/PMCID | list | list | cursor/page、JATS 全文能力 |
| INSPIRE | 高能物理 | 是 | DOI/arXiv/recid | list | list | embedded refs、`refersto` citations |
| OpenCitations | 开放引文图 | none | DOI/PMID/OMID | list | list | Index v2 + Meta，token 推荐 |
| Google Scholar/SerpApi | 可选网页索引 | 是 | cluster/query | none | list | 仅用户 key；绝不直抓 Scholar |

主要官方接口依据：

- [OpenAlex API](https://docs.openalex.org/)
- [Semantic Scholar Academic Graph API](https://api.semanticscholar.org/api-docs/)
- [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)
- [DataCite REST API](https://support.datacite.org/docs/rest-api)
- [DBLP Search API](https://dblp.org/faq/How+to+use+the+dblp+search+API.html)
- [ACL Anthology data access](https://aclanthology.org/faq/api/)
- [arXiv API](https://info.arxiv.org/help/api/)
- [OpenReview API v2](https://docs.openreview.net/reference/api-v2/openapi-definition)
- [Europe PMC REST](https://europepmc.org/RestfulWebService)
- [INSPIRE REST API](https://github.com/inspirehep/rest-api-doc)
- [OpenCitations API](https://api.opencitations.net/)
- [OpenAIRE Graph API](https://graph.openaire.eu/docs/apis/graph-api/overview/)

ACL Anthology 官网全文搜索依赖 Google Custom Search，没有第三方 REST 搜索 API。本实现不抓取网页，
用 DBLP 发现最多 100 个候选 ACL ID，再请求 `/{id}.xml`。DBLP 总数不是 ACL 总数，因此
`total_available=null`，候选池不足时返回 `truncated=true`。

OpenReview API v2 的 content 字段可能以 `{value: ...}` 包装。适配器解包 title、abstract、authors、
venue、keywords、DOI/arXiv/PDF/license，并固定 `source=forum` 排除 review/comment。

## 8. Reference 提取、OCR 与 Linking

结构化提取器支持：

- JATS XML：正文 `xref ref-type=bibr` 到 bibliography；
- TEI/GROBID：正文 `ref type=bibr` 到 `listBibl`；
- LaTeX：`\cite` 到 `\bibitem`；
- BibTeX：外部书目字段；
- PDF：用户明确指定 GROBID URL 后 multipart 上传；
- 扫描 PDF：显式 `--ocr`，本地 OCRmyPDF 后复用 GROBID/TEI。

每次正文引用可保存最多 1000 字符的 context、anchor、section 和 locator。默认只返回正文实际引用的
条目；bibliography-only 需要显式包括。XML 拒绝 DTD/ENTITY，PDF 和 OCR 输出限制为 100 MiB；外部
程序使用 argv + `shell=False`，OCR 语言字符白名单防止命令注入。

Linking 优先 DOI。没有 DOI 时用题名召回，并结合作者、venue、年份 ±1 评分。只有唯一候选超过
自动阈值且与第二名有足够 margin 才解析；ambiguous/unresolved 保留诊断。只有 resolved 且存在
verified body anchor 才生成 verified citation edge。

系统当前只处理调用方提供的本地 PDF，不实现 PDF 下载器。未来若增加，应是显式、OA 优先的独立
artifact 获取模块，并实现 URL/重定向/SSRF、大小、类型、哈希、许可和隔离处理，不应塞进 search。

## 9. 可靠性、分页与错误隔离

`ReliableHttpClient` 的关键契约：

- 只对无副作用查询进行缓存/重放；GET 和明确的只读 JSON POST 分开；
- 默认最多 3 次，重试 429、500、502、503、504 和 transport errors；
- Semantic Scholar 专用策略最多 5 次、约 2/4/8/16 秒指数退避并限制为 1 个并发、至少间隔 1.1 秒；
- 优先解析 `Retry-After`，其独立安全上限为 120 秒，不再被指数退避的 10/30 秒上限错误截短；
- 最终 HTTP 响应把 `attempt_count` 和实际 `retry_delays` 写入 Provider report context，未启用 SQLite 时
  也能判断退避是否真的执行；
- 每个 Provider 独立 semaphore、最小间隔、缓存 TTL 和内存熔断；
- 参数排序生成稳定 cache key；持久 URL 对 key/token 等参数脱敏；
- Authorization 不持久化，错误消息不回显带凭证 URL；
- Provider payload 的 AttributeError/KeyError/TypeError/ValueError 隔离到单分支报告。

`cursor` 是 Provider 的不透明 continuation token，调用方不得解析或自行拼接。OpenAlex 使用 opaque
cursor，Semantic Scholar 常使用 next offset，Europe PMC 使用 cursor/page。关系翻页 checkpoint
保存 Provider、operation、规范 seed、limit、token 和已完成页；正常完成后删除。

## 10. SQLite 数据与证据组织

未设置 `SCHOLAR_DB_PATH` 时，结果只返回调用方，进程结束后消失；不会保存 PDF。设置路径后自动创建
SQLite，并启用 foreign keys、WAL、busy timeout 和 `synchronous=NORMAL`。

| 表 | 内容 | 清理策略 |
|---|---|---|
| `schema_meta` | schema 版本 | 长期 |
| `query_runs` | 请求、完整结果 JSON、状态、fingerprint、时间 | 可按保留期清理 |
| `raw_responses` | 脱敏 URL、状态、白名单 header、body/hash | 可按日期/容量清理 |
| `http_cache` | 未过期成功响应 | TTL 清理 |
| `provider_attempts` | attempt、结果、错误、retry delay | 可按保留期清理 |
| `cursor_checkpoints` | 分页或完整 BFS 层恢复状态 | 成功删除、旧项清理 |
| `identity_events` | merge/split/defer/revert | 保留 |
| `citation_assertions` | 每个来源的原始边断言 | 保留、幂等 |
| `visible_citation_edges` | 聚合规范边和状态 | 保留、幂等 |
| `citation_edge_assertions` | 边与 assertion 多对多关系 | 保留 |
| `citation_edge_status_events` | provider_asserted -> verified 历史 | 保留、去重 |

SQLite 是运行快照和单机证据库，不是全局论文知识库：没有独立全局 papers/authors/venues 表，同一论文
可能存在于多个 run JSON，不适合跨历史大规模图查询。运行时 `-wal`/`-shm` 属于数据库状态；热备份
需 clean shutdown/checkpoint 或 SQLite backup API。`scholar maintenance` 默认只预览清理范围，只有显式
增加 `--apply` 才会删除符合保留策略的运行记录、原始响应和缓存。

## 11. CLI、API、MCP 与 Skill 契约

CLI、API、MCP 的输入最终构造同一 Pydantic 模型，输出同一结果模型。新增字段应先进入 Library，
再由薄适配层自动暴露；禁止在接口层复制去重、分页或图逻辑。

HTTP API：

- lifespan 中创建/关闭一个 service；
- 输入 schema 错误 422，未知来源等用户错误 400，seed 不存在 404；
- Provider partial/failure 是 200 内领域状态；
- 可配置 API key/Bearer、每主体固定窗口配额、4 MiB 默认 body limit；
- 同步图与可提交/查询/取消的异步图任务共存。

MCP：

- 使用官方 Python SDK FastMCP；
- stdio 是本地默认，Streamable HTTP 显式启用；
- 工具 `Context` 不进入公开 input schema；
- HTTP transport 可配置静态 bearer verifier、scope、per-subject quota；
- MCP 工具结果会成为 Agent 工具上下文，但数据库 raw/history 不自动进入。

Skill：

- 只描述何时调用 search/resolve/references/citations/related/expand/extract/link；
- 建议初始小 limit，检查 status/reports/truncated；
- 不保存 key，不自己抓取网页，不在模型侧重新去重；
- 不把 count、RRF 或 singleton entity 解释成不存在的确定性结论。

## 12. API 安全与多副本部署

安全边界：

- 默认服务只监听 loopback；公网必须 TLS + reverse proxy；
- API/MCP key 只来自环境或 secret mount；
- raw key 在 Redis rate-limit key 中先哈希；
- 默认 API/MCP 请求体上限 4 MiB，代理层需设置一致硬限制；
- PDF 是不可信输入，OCR/GROBID worker 应限制网络、CPU、内存、时间和挂载；
- Provider terms、文章 license 和用户授权决定能否保存/再分发，技术可访问不等于法律授权。

单节点使用 SQLite。多副本 profile 使用 Redis 共享：

- fixed-window API quotas；
- expansion payload/status/result/cancel；
- worker queue；
- `SCHOLAR_JOB_TTL_SECONDS` 控制 job 生命周期，AOF 保存 Redis 状态。

当前 Redis list 在 worker `BLPOP` 后崩溃不会自动 reclaim；客户端可以重交确定性 expansion。多个
容器不得同时读写挂载同一 SQLite。需要共享长期 evidence 时应迁移 PostgreSQL-compatible store；
Redis coordination 不能替代它。

## 13. 评测契约

检索指标包括 Precision、Recall、MRR、MAP、nDCG、no-new-relevant rate、work saved at target recall 和
bootstrap mean interval。Ranked IDs 先去重，分母显式。

实体评测同时报告：

- B-cubed；
- pairwise cluster；
- CEAF-e（Hungarian 全局最优对齐）；
- exact-cluster；
- false merge/split。

版本关系评测固定同一 pair universe，输出 same-manifestation/version-of/different-work confusion matrix、
逐类 P/R/F1、accuracy 和 macro-F1。

Reference gold 分层评估字段提取、候选生成、链接决策和 verified edge；无人工 gold 时的公共 JATS
smoke 只证明功能路径，不估计无 DOI、扫描 PDF 或跨领域准确率。

## 14. 开源复用与接口研究

项目没有直接复制下列第三方源码，而是吸收可验证的架构思想和官方接口：

| 项目 | 采用的思想 | 没有照搬的部分 |
|---|---|---|
| [paper-search-mcp](https://github.com/openags/paper-search-mcp) | Library/CLI/MCP 共用、每源连接器、free-first | 扁平 Paper 和首条胜出去重 |
| nature-academic-search | source records、字段冲突、逐源 count、partial | 不足以表达版本/正文证据 |
| [Paperoni](https://github.com/mila-iqia/paperoni) | workset refinement、人工 validate | 不是通用实时 citation union |
| [litdb](https://github.com/jkitchin/litdb) | CLI-first、SQLite、MCP | 单表不能表达逐边 provenance |
| [Local Citation Network](https://github.com/LocalCitationNetwork/LocalCitationNetwork.github.io) | seed/cited/citing、分页、图排名 | 单 Provider/扁平 ID 去重 |
| [Fatcat](https://github.com/internetarchive/fatcat) | Work/Release/File、审计 merge | 快照服务边界不同 |
| [S2APLER](https://github.com/allenai/S2APLER) | blocking、pair scoring、must/cannot link | 阈值不能未经本项目 gold 校准照搬 |

## 15. 关键设计决策账本

原分散 ADR 已合并为本节；编号保留用于提交、Issue 和测试追溯。

| 决策 | 日期 | 结论 |
|---|---|---|
| ADR-0001 | 2026-08-31 | Library-first、provenance-first、薄适配层 |
| ADR-0002 | 2026-08-31 | list/count/none capability routing；未解析书目显式保留 |
| ADR-0003 | 2026-08-31 | SQLite 审计缓存；幂等请求有界 retry/pacing/cursor checkpoint |
| ADR-0004 | 2026-09-01 | FastAPI/MCP 不复制业务逻辑；stdio 默认，公网显式鉴权 |
| ADR-0005 | 2026-09-01 | 多 seed、深度/节点/时间有界的确定性 BFS 引文图 |
| ADR-0006 | 2026-09-01 | Related 使用原生语义/推荐 + RRF；导出；正文 anchor 证据 |
| ADR-0007 | 2026-09-02 | 字段原值 claim、稳定 citation assertion/edge、有界来源重叠 |
| ADR-0008 | 2026-09-02 | 引用上下文；DOI 优先；题名/作者/venue/年份候选 linking |
| ADR-0009 | 2026-09-02 | B-cubed/pairwise/CEAF-e/exact cluster 与关系 confusion 分开报告 |
| ADR-0010 | 2026-09-02 | 扫描 PDF 显式本地 OCRmyPDF，再进入 GROBID |
| ADR-0011 | 2026-09-02 | assertion/visible edge/status event 独立持久化、幂等单向升级 |
| ADR-0012 | 2026-09-02 | 候选分项评分；阈值 + top-2 margin；ambiguous 不造边 |
| ADR-0013 | 2026-09-02 | Reference 题名候选尽可能使用 Provider 原生 title recall |
| ADR-0014 | 2026-09-02 | OpenAlex title/type/min-citation/sort 原生下推并规范层复核 |
| ADR-0015 | 2026-09-02 | Reference gold 按 extraction/candidate/link/edge 分层评估 |
| ADR-0016 | 2026-09-02 | 无 gold 时公共 JATS smoke 与科学准确率声明严格分离 |
| ADR-0017 | 2026-09-02 | 公网四模式测试低负载、宽松但可诊断，不固定动态排名 |
| ADR-0018 | 2026-09-02 | Provider 按覆盖增量/可验证性/许可成本选，不追求 20+ 数字 |
| ADR-0019A | 2026-09-02 | 可复现 OCR 镜像 + 真正 image-only PDF opt-in 测试 |
| ADR-0019B | 2026-09-03 | OpenReview 直连；ACL 使用 DBLP 候选 + 官方 XML 校验 |
| ADR-0020 | 2026-09-02 | Redis 共享 quota/job/cancel，不冒充共享长期 evidence store |
| ADR-0021 | 2026-09-02 | v0.1 发布核心是四种检索、多源证据和 Agent 工具封装 |
| ADR-0022 | 2026-09-03 | 用户文档收敛为 README、技术实现、测试验收三份主文档 |

当以下条件发生时复查相关决策：Provider API/许可变化；强身份规则在跨领域 gold 上系统性失败；
SQLite 写并发成为瓶颈；需要共享长期证据；MCP SDK 大版本迁移；引入 PDF 获取；新的排序模型完成
校准；或者新来源能证明独有覆盖增量。

## 16. 配置变量索引

| 类别 | 变量 |
|---|---|
| 环境文件 | `SCHOLAR_ENV_FILE` |
| Provider | `OPENALEX_API_KEY`、`OPENALEX_MAILTO`、`SEMANTIC_SCHOLAR_API_KEY`、`SERPAPI_API_KEY`、`CROSSREF_MAILTO`、`ARXIV_MAILTO`、`EUROPE_PMC_EMAIL`、`OPENCITATIONS_ACCESS_TOKEN`、`OPENCITATIONS_MAX_RELATIONS` |
| 存储/匹配 | `SCHOLAR_DB_PATH`、`SCHOLAR_REFERENCE_AUTO_MATCH_THRESHOLD`、`SCHOLAR_REFERENCE_MINIMUM_MARGIN` |
| HTTP API | `SCHOLAR_API_HOST`、`SCHOLAR_API_PORT`、`SCHOLAR_API_KEYS`、`SCHOLAR_RATE_LIMIT_PER_MINUTE`、`SCHOLAR_API_MAX_REQUEST_BODY_BYTES` |
| MCP | `SCHOLAR_MCP_TRANSPORT`、`SCHOLAR_MCP_HOST`、`SCHOLAR_MCP_PORT`、`SCHOLAR_MCP_API_KEYS`、`SCHOLAR_MCP_RATE_LIMIT_PER_MINUTE`、`SCHOLAR_MCP_MAX_REQUEST_BODY_BYTES`、`SCHOLAR_MCP_ISSUER_URL`、`SCHOLAR_MCP_RESOURCE_URL` |
| Redis | `SCHOLAR_REDIS_URL`、`SCHOLAR_REDIS_PREFIX`、`SCHOLAR_JOB_TTL_SECONDS` |
| Tests | `SCHOLAR_RUN_LIVE_TESTS`、`SCHOLAR_RUN_OCR_TESTS` |

实际默认值和最新字段以 `.env.example` 与代码中的 Pydantic/OpenAPI schema 为准。

## 17. 扩展规则

新增 Provider：

1. 实现 `ScholarlyProvider`；
2. 准确声明 keyword/advanced/resolve/ref/cite/related/fulltext、filter execution、pagination、access、
   credential、terms 和 redistribution；
3. 映射 `Paper`、identifier authority、source record、field claims 和 provenance；
4. 不支持的操作声明 none/count，不能伪造空 list 能力；
5. 在 Registry 注册，不改 `ScholarService`；
6. 覆盖正常、空、404、限流/传输、payload drift、分页/截断、身份和真实低负载 smoke；
7. 更新 README 矩阵、本技术文档和测试验收文档。

新增接口字段或算法必须先定义中立模型和 Library 行为，再更新 CLI/API/MCP 契约测试。涉及实体合并、
排序阈值或引用验证的变化必须提供版本化 gold 或明确说明只有功能 smoke。

## 18. 已知限制与下一步

- 公共 API 排名、可用性和限流会变化；
- 本地过滤只作用于有界上游 Top-N，可能漏掉库中符合条件但未进入候选池的结果；
- ACL 关键词搜索不是官方全库索引；
- OpenReview 可能对部分网络触发 challenge verification；不得绕过；
- Semantic Scholar 无 key 时共享匿名配额，即使指数退避也可能持续 429；生产稳定性仍依赖申请 key；
- OCR runtime 已定义但需本机 Docker/Podman engine 才能真实验收；
- Reference linking 阈值尚缺可再分发的跨领域人工 gold 校准；
- SQLite 不是持续增长、多 Agent 共享的规范论文仓库；
- Redis queue 没有 worker lease/reclaim；
- 尚无内置 PDF 下载器和全文分块检索。

优先下一步应由实际需求驱动：显式 OA artifact 获取、ACL 官方批量数据本地索引、跨领域 gold、共享
服务型证据库，或向量/图融合排序。它们不阻塞当前 Library/CLI/API/MCP/Skill 的核心使用。
