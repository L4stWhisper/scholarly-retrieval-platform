# 统一论文检索系统方案

这是一个统一的论文检索系统，支持关键词检索、高级检索、引文网络检索和语义检索，并把不同来源返回的论文整理成一组可追溯、可去重、可继续扩展的结果。

四种方式解决的问题不同：

1. 关键词检索回答“哪些论文提到了这个主题”；
2. 高级检索在关键词之上增加作者、年份、期刊、学科等条件；
3. 引文网络检索从一篇已知论文出发，沿 References 和 Citations 找到前后的相关工作；
4. 语义检索寻找措辞不同、内容相近的论文。

这是产品入口的划分，不是四个互斥的方法学类别。方法论文通常把关键词与高级检索归入主题检索（topical searching）或书目数据库检索（bibliographic database searching）；高级检索更准确地说是字段化检索（fielded search）。沿 References 和 Citations 查找论文称为引文检索（citation searching），也常称 citation chasing/tracking。语义检索与相似论文推荐则可能使用文本向量、引文图或混合信号。

高级检索仍属于传统检索，所以本文把前两种放在第一部分。后面依次讨论引文网络和语义检索，最后说明 CLI、Skill、API 和 MCP 如何分工。

## 一、传统检索

### 1. 为什么需要聚合多个来源

本方案面向用户仍称“关键词检索”，但实现上应覆盖完整的主题检索：自由文本、MeSH/Emtree 等受控词表、布尔表达式、短语与邻近运算、字段限定、过滤和排序。数据库与检索平台也不是同一个概念，例如 MEDLINE 是数据库，PubMed、Ovid 等是不同访问平台；provider manifest 应分别记录二者。

没有一个数据库在所有学科中都最全。计算机研究常先出现在 arXiv、OpenReview、ACL Anthology 或 DBLP；生物医学更依赖 PubMed、PMC 和 Europe PMC；天文学有 ADS，高能物理有 INSPIRE；经济学和社会科学还有 RePEc、SSRN。Web of Science、Scopus、Dimensions 和 Lens 覆盖广，但接入能力取决于机构订阅和 API 许可。

Google Scholar 覆盖很广，却没有面向开发者的官方公共 API。工程上可以接入 SerpApi 等第三方服务，或在自动接口失效时借用户浏览器访问。普通 Google Search 也应保留，它能找到项目页、技术报告、机构网页和数据库尚未收录的新论文，但返回结果必须经过学术内容识别。

因此，统一检索不能只选一个“最全数据库”。系统应根据查询领域选择三到六个来源并行搜索，再统一字段、解析论文身份、去重和排序。

| 来源 | 主要用途 | 局限或接入条件 |
|---|---|---|
| arXiv | 预印本、版本和源码 | 不是正式发表记录；并非每篇都有 LaTeX 源码 |
| Semantic Scholar | 跨领域搜索、论文 ID、引文图和推荐 | Reference/Citation 会漏项或误解析；连续请求容易 429 |
| OpenAlex | 开放元数据、引文图、外部 ID 和语义检索 | 新论文和引文关系可能滞后；References 只含成功匹配到 OpenAlex Work 的条目 |
| Crossref | DOI、出版商提交的元数据和部分 References | 成员提交 Reference 是可选的，不是完整全文或完整引文库 |
| Google Scholar via SerpApi | 广覆盖搜索、versions、related pages 和 citing papers | 第三方付费采集服务，不提供原始 Reference list |
| Google Search | 项目页、技术报告、机构网页和遗漏结果 | 会混入非学术内容，只能作为补漏通道 |
| DBLP、ACL Anthology、OpenReview | 计算机会议、作者和 BibTeX | 学科范围窄，但在各自范围内通常更规范 |
| PubMed、PMC、Europe PMC | 生物医学检索和部分结构化全文 | 领域专用；只有部分文章能合法取得 JATS/XML 全文 |
| ADS、INSPIRE | 天文学和高能物理 | 只按领域启用 |
| RePEc、SSRN | 经济学、社会科学和工作论文 | DOI 覆盖较弱，工作论文与期刊版的对应更复杂 |
| OpenAIRE、OpenCitations | 开放研究图、关系和来源证据 | 对新论文和部分领域覆盖有限，不能单独给出完整引文集合 |
| AMiner | 标题搜索、元数据、References 和作者/topics 推荐 | 需要 Token；当前公开接口只给被引数量，未证明能枚举 citing papers |
| Web of Science、Scopus、Dimensions、Lens | 高级字段检索、引文网络和分析 | 需要用户或机构授权，缓存与再分发受合同约束 |

AI 查询可以从 arXiv、Semantic Scholar、OpenAlex、DBLP 和 Google Scholar 开始；生物医学查询则优先 PubMed、Europe PMC、Semantic Scholar 和 Crossref。Google Search 是补漏渠道，不参与默认的论文质量判断。provider 的选择应由领域路由和用户配置共同决定，不必每次请求所有来源。

### 2. CLI 如何完成一次聚合检索

```text
用户查询
  -> 判断检索类型和大致领域
  -> 选择数据源并编译查询
  -> 并发请求、分页、限速和缓存
  -> 统一字段并保留原始来源记录
  -> 识别同一论文记录和同一工作的不同版本
  -> 合并、去重、排序
  -> 输出结果、来源、冲突、失败和截断状态
```

每个 provider 只负责两件事：把统一查询编译成该平台的请求，再把响应转换成统一记录。连接器自己处理分页、配额和错误，但不单独决定两条记录是否属于同一工作。这个判断放在共享的论文身份层中。

```bash
scholar search "agent harness" --sources arxiv,s2,openalex,google-scholar
scholar search "protein folding" --field biology --since 2024
scholar resolve "10.1145/..."
```

默认输出 JSON 或 JSONL，`--format table` 用于人工阅读。每次运行还要保存 query plan、实际成功和失败的 provider、分页范围、原始数、去重数、结果 fingerprint 和查询时间。某个来源失败应显示为 partial result，不能伪装成零结果。

### 3. 字段怎样统一

统一记录至少保存标题、摘要、作者、年份、载体、论文类型和语言；DOI、arXiv、PMID、PMCID、S2、OpenAlex、DBLP、ADS 等外部标识；预印本、会议版、期刊版和具体修订版本；PDF、JATS/XML、LaTeX、HTML 和开放获取地址；各来源自己的引用数、排序位置、请求状态和数据时间；每个字段及每条关系来自哪里。

合并字段不能采用“最后一个覆盖前一个”，也不应命中重复后只保留第一条。DOI 可优先参考 Crossref 或出版商，计算机会议名称可优先 DBLP，arXiv 版本以 arXiv 为准。若标题、作者或日期冲突，先保留各来源原值和来源，再生成当前 canonical view。

引用数尤其不能覆盖或相加。系统可以保留 `google_scholar=36`、`semantic_scholar=36` 这样的 count claims，但联合引用数必须从每一篇 citing paper 重新计算。

provider 不能只声明一个 `enabled`，而要报告 `keyword_search`、`advanced_search`、`resolve_id`、`references=list|count|link|none`、`citations=list|count|link|none`、`related`、`fulltext`、分页和再分发权限。这样 AMiner 的 Reference list、WoS Starter 的 Citation count、WoS Expanded 的 citing items 和 Google Search 的网页结果不会被混为一谈。

### 4. 一篇论文怎样去重

#### 4.1 先把两个问题分开

“去重”至少包含两个不同判断：两条数据库记录是否描述同一个具体发布版本；arXiv、会议论文和期刊论文是否属于同一研究工作。前者是书目记录消歧，后者是 work-family grouping。conference paper 和 journal extension 可以属于同一工作族，却不应被当成同一个发布版本。

内部采用四层模型：

```text
WorkFamily
  -> Manifestation / Release
       -> Version
            -> Artifact (PDF / XML / LaTeX / HTML)
```

`WorkFamily` 使用内部 UUID。DOI、arXiv ID、PMID 和各数据库 ID 都是带来源的 `IdentifierClaim`。关系至少区分 `duplicate_record_of`、`version_of`、`preprint_of`、`conference_version_of`、`extended_version_of`、`correction_of` 和 `translation_of`。

这一模型借鉴 Fatcat 的 `Work -> Release -> File`，但增加明确的 Version 层。OpenAlex 的 Work+locations、Semantic Scholar 的单层 paper、OpenCitations 的 OMID 都是外部证据，不能代替内部 WorkFamily。

#### 4.2 CLI 与 Skill 的职责边界

CLI 的确定性代码负责：

1. 规范化 DOI URL、大小写、arXiv `vN`、PMID/PMCID、Unicode、作者名和日期精度，同时保留原值；
2. 用相同 DOI、完整 arXiv version ID、PMID、PDF hash 或 provider 明示关系建立 must-link；
3. 用互斥强 ID、类型冲突、数字冲突、correction/reply/appendix 等信号建立 cannot-link；
4. 通过标题倒排、字符 n-gram、作者和年份窗口、摘要向量、Reference/Citation 指纹生成候选；
5. 计算标题、作者、摘要、年份、venue、页码、外部 ID、全文和图特征；
6. 自动合并证据明确的重复记录，保存 provenance、特征快照、算法版本和可回滚 merge log；
7. 把中间置信区间的少量候选生成 `review_bundle.json`。

Skill 中的 LLM 只审查疑难 work-family 候选：标题大改、作者增删、arXiv 与会议或期刊版没有桥接 ID、conference paper 与 journal extension 的边界。Skill 输出：

```json
{
  "decision": "merge | split | defer",
  "relation": "preprint_of | extended_version_of | duplicate_record_of | ...",
  "confidence": "high | medium | low",
  "evidence_for": [],
  "evidence_against": [],
  "missing_evidence": [],
  "rationale": ""
}
```

LLM 不直接修改 canonical entity。CLI 校验输出后，以事务和追加日志写入；高风险合并、显式来源冲突或会明显改变引用数的决定可以要求人工确认。原始 provider records 永不删除，拆分和回滚通过追加反向事件并重新投影 cluster 完成。

#### 4.3 可以采用的算法路线

第一版可采用 Crossref Marple 风格的可解释打分：搜索候选，再比较标题、作者和年份，同时要求最低分和最高分之间有足够 margin。数据量增大后可参考 S2APLER 的 title blocking、pairwise scoring、must/cannot-link、average-linkage clustering 和 incremental assignment。

摘要对改题很重要，PreprintMatch 也说明标题、摘要和作者联合优于只看标题。fuzzycat 的 `EXACT / STRONG / DIFFERENT / AMBIGUOUS` 状态和 reason code 适合用作判定接口。论文中的阈值和准确率只能作为起点，必须在自己的跨学科数据上重测。

自动合并应偏向高 precision。误合并会污染两篇工作的 References、Citations 和引用数；漏合并暂时只表现为重复，更容易修复。聚类还要检查 cluster coherence，不能仅凭 A≈B、B≈C 就强行合并 A 与 C。

### 5. 高级检索怎么做

高级检索与关键词检索共用一条处理链，只是在查询中增加标题、摘要、作者、机构、期刊或会议、时间、学科、论文类型、开放获取状态、最低引用数和精确 ID 等条件。

CLI 先把查询表示成中立对象，再由 connector 编译成 OpenAlex filter、PubMed query、WoS/Scopus 字段语法。连接器不能默默丢条件。某个来源不支持作者过滤时，应说明该条件是在本地执行，或根本没有执行。

WoS Starter 与 Expanded 应拆成不同 capability profile，因为前者主要给元数据、计数和跳转链接，后者才提供 cited references、citing items 和 related records。系统还应有在线联邦模式和可复现批量模式；后者使用 OpenAlex、S2、arXiv、xRxiv 或 OAG 快照并保存数据版本。

### 6. 可以复用哪些开源项目

没有发现一个项目同时完成跨学科检索、字段 provenance、跨版本解析、原文 Reference 验证和多源 Citation union。

| 项目 | 最值得复用的部分 | 仍缺什么 |
|---|---|---|
| [paper-search-mcp](https://github.com/openags/paper-search-mcp) | 大量 provider、并发 fan-out、全文下载回退；CLI、Skill、MCP 共用实现 | 命中重复后首条胜出；References 基本未填，Citation Graph 仍是 TODO |
| [nature-academic-search](https://github.com/wp-a/nature-academic-search) | 真正并发多源；保存 `source_records`、字段冲突、分来源引用数、运行 manifest；引文边合并 `observed_by` | 没有 WorkFamily/版本层和原文验证；S2 引文分页不完整 |
| [Paperoni](https://github.com/mila-iqia/paperoni) | workset、逐步 refinement、字段质量合并、人工 validate | 目标是维护发表集合，不是通用引文 union |
| [paper-search-pro](https://github.com/O0000-code/paper-search-pro) | 确定性 Python 与 Skill 判断的分工、学科字段富化 | 单一 canonical key 仍无法处理 DOI-only journal 与 arXiv-only preprint |
| [scholar-megasearch](https://github.com/TaewoooPark/scholar-megasearch) | 学科分桶、原始结果落盘和多波次 snowball | provider 调用和 schema 转换过多依赖 Agent；标题 key 易过合并 |
| [Academix](https://github.com/xingyulu23/Academix) | 多种 ID 路由、统一模型、DBLP BibTeX 优先和 provider fallback | 默认是 OpenAlex 优先的回退，不是并行多源聚合 |
| [litdb](https://github.com/jkitchin/litdb) | SQLite 项目库、关键词+向量+引文递归、CLI-first 和 MCP adapter | 引文主要来自 OpenAlex，没有跨源身份知识图谱 |
| [paperscraper](https://github.com/jannisborn/paperscraper) | 学科数据 dump、本地批量检索和全文获取 | 标题级去重，Citation 多为 count |
| [Fatcat/refcat](https://github.com/internetarchive/fatcat) | Work/Release/File、可审计合并；多源 Reference 匹配并保留 unmatched | refcat 公开说明偏旧快照，不是实时服务；跨版本 grouping 也未完全解决 |
| [Academic Review Tool](https://github.com/alan-turing-institute/academic_review_tool) | Crossref、WoS Starter、Scopus、ORCID 等接口，字段检索与 citation/weblink crawling | 不是跨版本实体图，也没有完整多源 citation union |
| [PaperQA](https://github.com/Future-House/paper-qa) | 检索后的全文证据、带引文问答和成熟的 CLI/开发文档 | 属于下游 RAG/Agent，不负责构造完整 Reference/Citation 图 |

最接近我们的组合是：借 `paper-search-mcp` 的连接器和下载层，借 `nature-academic-search` 的 provenance、失败契约和边 assertion，借 Paperoni 的审核工作流，再自行实现 WorkFamily/Manifestation/Version 和可回滚实体层。

### 7. 学术知识图谱给出的设计线索

AMiner、OpenAlex、OpenAIRE、OpenCitations、Semantic Scholar、Fatcat 和 Wikidata 都做了不同程度的聚合，但没有一个公开图完整保留我们需要的版本层次与逐边证据。

- OpenAIRE 值得借鉴 PID authority。IdentifierClaim 要同时保存 scheme、值、provider、authority 和置信度。它还把关系方向、语义、provenance、trust 和 validation 分开保存。
- OpenCitations 把书目实体和 citation entity 分开。相同 citation 可折叠成一条 visible edge，同时保留多个来源 assertion。
- Semantic Scholar 的 S2APLER 是大规模论文聚类参考，但 S2 paperId/CorpusId 只能作高权重证据。
- Fatcat 的 Work/Release/File、redirect 和 editgroup 适合身份与审计层；refcat 说明 Reference 匹配要保留 raw、matched、unmatched 和 provenance。
- Wikidata 适合用 DOI、arXiv、PMID、OpenAlex、ADS、Dimensions 等属性做 ID bridge；Scholia 是查询展示层，不是实时多源聚合器。
- AMiner 当前是商业 API；OAG 3.2 是重量级离线图。OAG 可用于批量索引，但公共数据缺少逐边上游 provenance，也没有完整版本层。

知识图谱在第一版主要是一种数据建模方法，并不要求立即部署 Neo4j 或 RDF。SQLite/PostgreSQL 加稳定 UUID、claims、assertions 和事件日志已经能表达这些语义。

## 二、引文网络检索

引文网络检索从一篇或一组种子论文出发，沿 References 找更早的被引工作，沿 Citations 找后来引用它们的工作。两者都是论文之间的边，但获取方法不同。

按 TARCiS 和 PRISMA-S 的方法术语，检查种子论文的 cited references 是后向引文检索（backward citation searching）；查找 citing references/works 是前向引文检索（forward citation searching）；两边都查是双向引文检索。只有把新发现论文继续作为种子迭代扩展时，才更适合称为 iterative citation searching 或 snowballing。CLI 仍使用不易混淆的 `references` 和 `citations`，因为不同 API 对 forward/backward 的图方向解释可能相反。

珍珠生长法（pearl growing）的范围更宽。它可以从已知相关论文继续扩展题名词、主题词、作者、References 和 Citations，不是后向引文检索的同义词。系统以后可以把它实现为组合 workflow，但底层仍调用主题检索与引文检索两个明确模块。

### 1. References：先判断原文是否引用，再解析它指向谁

Reference 模块要区分书目列表中的条目、正文实际指向该条目的 citation anchor，以及条目最终解析到的论文实体。即使取得结构化 XML，也不能自动把整个 reference list 当成正文已引用的 References。

我们用 [arXiv:2603.25723](https://arxiv.org/abs/2603.25723) 做了小测试：

- LaTeX 正文实际使用 69 个唯一 citation keys；
- 源码中的 `.bib` 共 182 条，其中 113 条没有被正文引用；
- Semantic Scholar 返回 85 条 Reference；按标题保守匹配后命中 66/69，漏 3 条，同时多出 19 条；
- 多出的内容中出现 `Graph`、`Pipelines`、`OpenAI` 等解析碎片；
- OpenAlex 对这篇论文返回 0 条 Reference；
- GROBID 从 PDF 抽出 66 个 `biblStruct`；63 个正文 callout 中只有 34 个带 bibliography target。

这个样本说明，返回数量更大不等于更完整。数据库会漏链和误链，PDF 也会漏抽和错连。因此 References 要聚合，但不能盲目求并集。

### 2. Reference 的四条获取路径

#### 路径一：JATS/XML、TEI 或结构化全文

有合法结构化全文时优先走这条路。JATS 的 `<xref ref-type="bibr">` 可以指向 `<ref>`，TEI 也能从正文指针连到 `<listBibl>/<biblStruct>`。它们比 PDF 少一层版面恢复误差，还能保留 citation context。

优点是结构和正文锚点清楚，适合生成高置信 Reference edge。局限首先是权限和覆盖：PMC 只覆盖部分生命科学论文；出版商没有统一的“输入 DOI 就返回 XML”接口。以 Elsevier Full Text API 为例，API key 只是必要条件，受版权保护的全文还需要用户或机构 entitlement、OAuth/session 或 institution token。Token 证明访问资格，不是绕过付费墙的通行证。

结构化全文也不能只数 `<ref>`。JATS 允许 suggested references 或 additional reading，仍应优先依据正文 bibr anchor。若只有书目列表而没有正文锚点，证据等级应低一级。

#### 路径二：arXiv 或其他预印本的 LaTeX 源码

对有源码的指定版本，最终渲染出的 bibliography 通常最接近作者原稿。必须请求准确的 `vN`，因为标题、作者、正文和 References 都可能随版本变化。

优先读取与主 TeX 对应的 `.bbl`；没有 `.bbl` 时，在隔离环境编译并读取最终 `.bbl`、`.aux` 或 `.bcf`，也可用 LaTeXML 抽取 citation context。编译失败后才展开多文件并扫描 `\cite`，此时结果标为 inferred。整个 `.bib` 不能当作 Reference list。

优点是能恢复具体版本实际使用的引用和上下文。局限是只覆盖提供可编译源码的预印本，复杂宏、多根文件、条件编译和 TeX 环境漂移都会造成失败。

#### 路径三：用户已有合法 PDF

没有更好的结构化来源，但用户已经取得 PDF 时，以本地 GROBID 为主。它能输出 TEI、Reference、raw citation、正文 callout 和坐标，并可调用 Crossref 或 biblio-glutton 做 consolidation。

优点是覆盖任何已取得的 born-digital PDF。局限是多栏、断行、扫描件、脚注、非拉丁文字和非标准版式都会降低准确率。AnyStyle 可做轻量 fallback 或差异检测；CERMINE 适合作旧基线；Science Parse 和 ParsCit 系列维护状态较差，不建议作为新系统主依赖。

多个 PDF parser 的结果不能直接求并集。系统应先按条目顺序、页面坐标、正文 callout、DOI 和字符串相似度对齐，把差异保存成候选。

#### 路径四：没有全文时聚合数据库

Semantic Scholar、OpenAlex、Crossref、OpenCitations、Europe PMC、AMiner、Lens 和领域数据库都可以提供 Reference candidates。Crossref 的 reference 来自 depositor metadata，可能只有 raw string；OpenCitations 主要覆盖可识别的开放 citation；Europe PMC 适合生物医学；AMiner 的 `cited` 字段经官方说明实际是 References。

这条路线没有全文时仍能工作，多源互补也会提高召回；但无法保证等于原文，provider 还会有版本归组、错链、滞后和限流问题。数据库独有边只能标为 `provider_asserted` 或 `candidate`。后续拿到原文后再升级为 verified。

```text
合法 JATS/TEI 且有正文锚点
  -> verified edge，数据库补论文身份
否则，有指定版本的可编译源码
  -> 最终 bibliography/citation anchor 验证，数据库补身份
否则，用户已有合法 PDF
  -> GROBID 提取，matcher 补身份，保留解析置信度
否则
  -> 多数据库 union，只生成 provider-only candidates
```

### 3. 把 Reference 字符串解析成论文实体

Reference extraction 先回答“原文是否存在这条边”，citation matching 再回答“它指向哪篇论文”。匹配优先使用 DOI、PMID、arXiv 等强标识；随后并行从 Crossref、OpenAlex、Semantic Scholar 等取得 top-k 候选，再比较标题、作者、年份、venue、卷期页和上下文。

biblio-glutton 可把 raw reference 匹配到 DOI、PMID 等结构化记录，适合机构级本地部署；代价是较大的 Crossref/PubMed 索引和持续更新。普通用户可先用远程 API 与磁盘缓存。低置信条目要保留 raw string 和 unresolved 状态，不能盲取 top 1。

这一步也遵循同一 CLI/Skill 边界：CLI 生成候选、分数和 provenance；Skill 只审查改题、作者变化或版本不清的候选，输出 `match / no-match / defer`，最后由 CLI 可回滚地写入。

### 4. Citations：多来源求并集

Citation 是开放集合。新的论文会继续引用目标论文，任何单库也可能尚未收录。因此必须取得 citing papers，而不是相加各家的 citation count。

对 `2603.25723` 的同一次测试如下：

| 来源 | 原始返回 | 来源内去重后 |
|---|---:|---:|
| Google Scholar via SerpApi | 36 | 36 |
| Semantic Scholar | 36 | 35 |
| OpenAlex | 1 | 1 |
| OpenAIRE、DataCite、OpenCitations | 0 | 0 |

Google Scholar 和 Semantic Scholar 虽然都显示 36，返回的却不是同一批论文。初步去重后，交集为 20，Google Scholar 独有 16，Semantic Scholar 独有 15，联合约 51 个研究工作。Google Scholar 单独只覆盖两者联合集的约 70.6%。这不是逐篇全文验证的绝对真值，但足以说明“挑一个最全来源”在这个样本上不可行。

默认可用 Semantic Scholar 与 Google Scholar via SerpApi，OpenAlex、OpenCitations、OpenAIRE 和 Crossref Cited-by 作开放补充；按学科加入 ADS、INSPIRE、Europe PMC。Lens、WoS Expanded、Scopus 和 Dimensions 作为用户自带权限的商业连接器。

- Lens 能同时返回 References 和 scholarly citations，还连接 patent citations，但 Token 需申请；
- WoS Expanded 有 cited references、citing items 和 related records，Starter 主要提供元数据、计数和跳转链接；
- Dimensions 有 `reference_ids` 和反向查询，但许可可能限制 bulk、dashboard 和 derivative product；
- AMiner 当前只证明能返回 References 和 Citation count，不能列作完整 citing-paper provider；
- Serper 当前只证明是 Google Web API，未核实 Google Scholar Cited-by endpoint，不能和 SerpApi Scholar 互换。

### 5. Citation 怎样去重

各来源的 citing records 进入与传统检索相同的身份管线。先解决同一 manifestation 的重复记录，再判断预印本、会议版和期刊版是否属于同一 WorkFamily。每一条原始关系都保存成 assertion：

```text
CitationAssertion(
  citing_manifestation,
  cited_manifestation_or_work,
  provider,
  provider_record_id,
  observed_at,
  evidence_type,
  verification_status
)
```

同一规范边被多个 provider 观察到时，界面显示一条边，底层保留每份 assertion。输出同时报告各来源原始数、来源内去重数、来源独有增量、manifestation-level union、WorkFamily-level union、自动合并数、LLM 审查数、冲突和待确认数。

用户提供的 [Google Scholar、Semantic Scholar、NASA ADS 聚合样例](https://github.com/zjsxply/zjsxply.github.io/blob/main/.github/workflows/update-scholar-citations.yml)已经实现分页、ID 规范化、来源独有增量和部分修订版合并，是第一版可直接参考的原型。它最终仍采用 `arXiv > DOI > title > URL` 的单一优先 key，所以 arXiv-only 与 DOI-only 的同一工作可能无法相遇。我们的实现应把全部标识挂在同一 entity 上，再走候选和 work-family 审查。

### 6. 从引文边扩展局部网络

References 和 Citations 稳定后，可以从一篇或一组 seed 扩展网络。必须显式限制方向、深度、每层候选数、时间和论文类型，否则网络很快爆炸。

```bash
scholar references arxiv:2603.25723 --mode balanced
scholar citations arxiv:2603.25723 --sources s2,google-scholar,openalex
scholar graph expand arxiv:2603.25723 --direction both --depth 2 \
  --frontier-cap 200 --top-k 100 --stop-rule budget
```

每条候选要保存发现路径、层级、得分、分页和截断状态。多 seed 命中数、Personalized PageRank、co-citation 和 bibliographic coupling 可以作为第一批可解释排序。BibliZap 的公开评测显示，多层追踪能提高召回，却可能一次增加数千候选；因此 depth、frontier cap、Top-K 和 stop rule 是核心检索参数。

### 7. 相似项目怎样影响技术路线

#### Local Citation Network

[Local Citation Network](https://github.com/LocalCitationNetwork/LocalCitationNetwork.github.io) 与我们的局部引文网络目标很接近。源码实现了 OpenAlex、Semantic Scholar、Crossref 和旧 OpenCitations adapter，把响应统一为 article DTO，再生成 seed、cited、citing、Top Cited、Top Citing、co-cited 和 co-citing 网络，并支持 JSON、CSV、RIS 和本地缓存。

但它是 provider 单选，不是一次请求多源 union。`callAPI` 根据一个 API 字符串走互斥分支，每张图也只记录一个来源。去重仅按当前 provider ID，没有跨源 WorkFamily。

可以借鉴 source -> seed -> cited/citing 的构图方式，OpenAlex 每 50 IDs 分块与 cursor 分页，S2 References/Citations 分页，Top Cited/Top Citing、co-citation、provider 失败 placeholder 和多格式导出。不能照搬单 provider 身份键、扁平 article、缺少原文验证、S2 batch 的 500 IDs/9999 citations 边界、429 后固定等待两分钟、以及 `localStorage` 持久化。

#### CitationChaser

[CitationChaser](https://github.com/nealhaddaway/citationchaser) 的 R 包和 [Shiny 应用](https://estech.shinyapps.io/citationchaser/) 面向系统综述中的 forward/backward citation chasing。它接受多个 seeds，先取得 Lens 边 ID，再批量补邻居元数据，最后导出 RIS。

它本身只调用 Lens，并按 Lens ID 去重。Lens 的底层虽然聚合 PubMed、Crossref、CORE 等来源，客户端却没有多源 provenance 和跨版本解析。它适合借鉴批量 seed、先边后节点、分桶分页和 RIS 工作流，不能作为我们的聚合和身份层。目标论文在线试跑未成功；换成熟 DOI 时能得到 65 References 和 205 Citations，也说明它更适合作为可选 Lens 前端，而不是完整性真值。

#### 其他引文工具

| 项目 | 可借鉴之处 | 与本项目的差距 |
|---|---|---|
| [Citation Gecko](https://github.com/CitationGecko/gecko-react) | Crossref References + OpenCitations Citations 汇入同一 paper/edge store，多 seed 排序 | 两来源固定分工，不是同类边 union；匹配仍靠 MAG ID、DOI、精确标题+作者 |
| [BibliZap](https://github.com/BibliZap/BibliZap) | Lens 多层递归、路径频次、缓存和候选预算 | 仍是 Lens 单源，不能解决多源身份与证据 |
| [Inciteful](https://inciteful.xyz) | PageRank、co-citation、bibliographic coupling、两论文连接路径 | 后端未开源，排序结果不是完整引文集合 |
| [Paperfetcher](https://github.com/paperfetcher/paperfetcher) | DOI set、Crossref/COCI 和 RIS snowballing | provider 是替换而非 union，缺少无 DOI 文献 |
| Litmaps、ResearchRabbit | collection、人工扩图、监控和反馈闭环 | 没有可依赖的公开聚合 API 或可审计去重规则 |
| [Connected Papers](https://www.connectedpapers.com/about) | 共引与文献耦合的相似论文图 | 不是 Citation tree，也不是完整引文 provider |
| [CiteSource](https://github.com/ESHackathon/CiteSource) | 多来源导入、候选去重、人工复核和 merge log | 假定重复项同一期刊，不处理 preprint/journal WorkFamily |
| [LitStudy](https://github.com/NLeSC/litstudy) | 多 provider 的统一 Document 接口、显式集合并集和多 seed 图分析 | 标识模型仍不足以表达跨版本 WorkFamily |
| [Snowballing](https://github.com/JoaoFelipe/snowballing) | Google Scholar 浏览器辅助、PDF Reference 降级和筛选 provenance | 不是稳定的多源服务，浏览器/PDF 路径只能作兜底 |

这些项目分别解决批量导出、人工扩图和图排序，没有一个完成“多源可审计 union + 原文 Reference 验证 + WorkFamily 去重”。我们的方案不是重复制作一张 citation graph，而是补上它们普遍缺少的身份与证据层。

## 三、语义检索与相关论文发现

相关论文发现可以从一段自然语言、一篇论文或一组正负样本出发。这里要分清三类能力：文本向量检索、种子论文推荐、引文图相似。产品层可以统一成 `scholar related`，但每条结果必须保留 `retrieval_method`。

### 1. 可接入的能力

| 服务或项目 | 已核实能力 | 在系统中的位置 |
|---|---|---|
| [OpenAlex Semantic Search](https://help.openalex.org/api/semantic-search/) | GTE Large EN、1024 维，题名+摘要向量；最多 2000 字符、50 条结果、1 RPS | 自然语言或摘要的默认文本语义召回 |
| [Semantic Scholar Recommendations](https://api.semanticscholar.org/api-docs/recommendations) | 一篇或多篇正负样本推荐，最多 500 条 | seed-paper 和用户反馈驱动推荐 |
| Semantic Scholar SPECTER2 | 对已知 paper IDs 返回 embedding | 候选重排和用户私有论文库，不是自然语言全库搜索 API |
| [Exa](https://exa.ai/docs/reference/search) | neural/auto Web 搜索及 publication 类别 | Web 语义补漏，结果仍需实体解析 |
| Web of Science Related Records | 依据共同被引文献找相关记录 | 授权用户的 bibliographic coupling，不是文本向量搜索 |
| Connected Papers、Litmaps、Inciteful | 共引、文献耦合和引文图发现 | 参考算法和交互，本地可复现 |
| ResearchRabbit | collection 和用户反馈驱动的持续推荐 | 参考反馈闭环；没有已核实的公共 API |
| Open Knowledge Maps / Head Start | 对检索结果做主题聚类和知识地图 | 参考结果概览，不是引文边 provider |

本次用 `2603.25723` 的摘要调用 OpenAlex Semantic Search，成功返回 50 条，结果集中在 agent、harness 和 reliability，但也有主题漂移。Semantic Scholar Recommendations 对同一 arXiv ID 也成功返回相关工作；随后请求 SPECTER2 embedding 再次遇到 429。

Exa 是通用 Web 搜索，不是学术知识图谱，返回的 publication URL 和 citation 字段只能作为候选。当前没有足够证据证明 Nature 提供面向第三方的通用相似论文 API，因此不把 Nature 列作正式 provider。

### 2. 推荐的聚合方法

`scholar related` 并行运行四个召回器：OpenAlex `search.semantic` 接受研究问题或摘要；S2 Recommendations 接受论文 IDs 和正负样本；CLI 在聚合后的引文图上计算 bibliographic coupling、co-citation、PageRank 或 link prediction；Exa publication 搜索 Web 范围遗漏结果。

候选先经过 WorkFamily/Manifestation/Version 去重，再融合排序。第一版可以用 Reciprocal Rank Fusion，避免直接相加不可比的分数。SPECTER2 用于已有向量候选的重排。输出要说明论文为何出现，例如 `OpenAlex semantic`、`S2 positive seeds`、`shares 18 references`。

不建议第一版自建全球论文向量库。论文数据持续更新，全量 embedding、ANN 索引和增量同步都很重。用户已下载或收藏的私有集合可以建立本地向量索引；全球召回先依赖托管服务和开放图。

Skill 负责把自然语言转换成召回计划、选择正负样本和解释少量临界结果。API 请求、图指标、RRF、缓存、去重和 provenance 仍由 CLI 完成。

## 四、实现形式

### 1. CLI 是核心

检索、分页、限速、字段合并、候选生成、确定性去重、缓存和导出都由 library 和 CLI 完成。Python 适合第一版，因为已有论文 API、GROBID client、文本匹配和数据处理生态。

```text
scholar search
scholar resolve
scholar references
scholar citations
scholar related
scholar graph expand
scholar review
scholar export
scholar providers
scholar doctor
```

library 与 CLI 分开，HTTP API、桌面界面、Skill 和 MCP 都调用同一套 library。SQLite 足以支持本地单用户模式；多人共享缓存和长任务时再换 PostgreSQL 和 worker queue。

持久层至少保存 provider 原始记录和字段 claims；WorkFamily、Manifestation、Version、Artifact；IdentifierClaims；Reference/Citation assertions 和 evidence；merge/split/defer 决策和 cluster version；query plan、cursor、重试、截断和缓存状态。

### 2. Skill 处理安装、路由和疑难判断

Skill 负责发现、安装、更新和检查 CLI；判断关键词、高级、引文还是语义检索；选择领域 provider 和查询深度；解释哪些来源缺失；审查 CLI 生成的疑难版本和 Reference matching bundles。

安装和更新体验可参考 [Agent Access](https://github.com/r266-tech/agent-access) 的 `list / info / doctor / install / update` 思路。Skill 不负责逐页请求、正则解析、全库聚类或直接改实体库。

### 3. API 和 MCP 是适配层

需要共享缓存或长任务时，在 library 上增加 HTTP API。MCP 可以支持 `search`、`references`、`citations` 和 `related`，但只是同一能力的 Agent 入口。MCP 不拥有另一套 schema 和去重逻辑。

### 4. 429、凭证与用户浏览器兜底

限流和权限是正常运行状态。每个 provider 需要独立 token bucket、并发预算、`Retry-After`、指数退避、jitter、最大重试、熔断、逐页落盘和断点续传。固定睡眠一秒或两分钟都不够稳。

凭证顺序是公开 API、用户自己的 API key、机构或商业 Token，最后才是用户浏览器。浏览器兜底使用用户现有登录态和合法权限，适合 Google Scholar、出版商页面或机构订阅；它不是绕过 CAPTCHA、付费墙或访问控制。商业 provider 还要保存 access tier 和 redistribution policy。

## 五、检索系统怎样评测

这套系统不能用“返回了多少篇”衡量。评测至少要回答六个问题：该找的论文找回多少，用户要筛多少；多接一个来源带来多少独有且正确的增量；相关论文排得是否靠前；论文版本有没有合对；References 是否从原文抽全并链接到正确工作；系统在限流和中断下能否完整、可重复地运行。

### 1. CoCites 的 75% 应怎样理解

用户给出的博客数字来自 Janssens 等人的 [CoCites 验证研究](https://doi.org/10.1186/s12874-020-0907-5)。研究用 250 篇已发表系统综述最终纳入的 4,761 篇论文作回溯性 gold set，从每篇 review 的已知纳入论文中选择两篇当时被引最高的论文作种子。

组合方法先筛共被引论文，再做双向引文检索，得到的每篇 review 检出率中位数是 75.0%，IQR 50.0% 到 90.1%，中位候选数 873。这个 75% 不是“找到全网 75% 相关文章”，也不是任意种子和任意学科的通用召回率。种子来自事后已知 gold，并按高被引挑选；两篇种子本身还被计入检出率；数据依赖 Web of Science，样本主要处在生命科学系统综述语境。原 review 的中位筛选量是 794，组合检索也并非总能减少工作量。

这项研究给我们的真正启示是：评测引文检索必须同时报告 recall 和 screening burden，并固定历史时间截点，不能使用检索完成以后才出现的引用边。

### 2. Gold 数据与切分

应维护三套互补真值：

1. 系统综述回溯集。保存 topic、原检索日期、最终纳入论文和原始筛选量，用于测论文发现能力与筛选成本。这里的 gold 只代表该 review 纳入的论文，不是全领域所有相关论文。
2. 逐论文引文真值集。以特定版本的 LaTeX 最终 bibliography、人工校正的 JATS/TEI 或跨学科 PDF 标注为依据，保存正文 callout、书目条目和目标 Work。
3. 实体解析集。收集多个 provider 中同一工作的预印本、会议版、期刊版和修订版，以及标题相似但不同工作的 hard negatives，分别标注 `same manifestation`、`same work/version-of` 和 `different work`。

训练、调参和测试按 WorkFamily 或 topic 分组。同一工作的 arXiv 与期刊版不能分落训练和测试。历史回放要求检索索引、citation graph、embedding、seed 和 provider 数据都遵守同一个 cutoff。跨学科结果除总体值外，还要报告分领域分布、最差领域和置信区间。

### 3. 传统检索、语义检索和推荐

主题检索和系统综述首先看 Recall，也叫 sensitivity；同时报告 Precision 和 `NNR = screened / relevant found`。排序结果使用 Recall@k、MRR、MAP 和 nDCG@k。只有具备多级相关性标注时才使用 nDCG。若评估达到 95% recall 时节省的筛选工作，可以报告 WSS@95，但必须把目标 recall 写进指标名。

自然语言文本检索、seed-paper recommendation 和 graph similarity 不是同一个任务：

- 文本检索可用 LitSearch、TREC-COVID、NFCorpus 等 benchmark；
- 种子推荐可用 RELISH、CSFCube 和 SciRepEval 的 Cite/CoCite/CoRead；
- 图相似应做时间切分，用 cutoff 之后新增的引用或共引作弱标签，并另配人工相关性判断。

每套数据都运行单一 provider、最佳单源、所有来源 union、RRF 融合、融合后重排。另做 leave-one-source-out，报告 oracle recall、fusion loss 和各 provider 的 unique relevant gain。这样才能知道问题出在召回、融合排序还是某个来源。

### 4. 引文检索与多源增量

对 References 与 Citations 分方向报告 edge precision、recall 和 F1，并按一跳、两跳和固定候选预算分别计算。topic 层另报 Recall@k、NNR、每增加一层的 marginal relevant gain，以及每个 provider 增加的相关工作和噪声。

多源比较包括 provider 两两 Jaccard、overlap coefficient、独有相关结果、加入一个来源后的 marginal recall gain，以及 raw records、Manifestations 和 WorkFamilies 三种数量。CitationChaser 和 SpiderCite 都依赖 Lens，不能被当成两个独立底层来源。

共引（co-citation）指两篇论文被后来论文共同引用；文献耦合（bibliographic coupling）指两篇论文共享 References。它们是相似度信号，不是直接 citation edges，评测时应归入 related-paper ranking。

### 5. 论文去重与版本识别

实体评测拆成三层：

- blocking/candidate recall，检查所有真实匹配是否进入候选，并报告 candidate reduction ratio；
- pairwise precision、recall、F1，另列 false merge 和 false split；
- cluster-level B-cubed precision/recall/F1，必要时补 CEAF-e 和 exact-cluster accuracy。

还要给 `same manifestation / same work or version / different work` 的 relation confusion matrix，并分别报告无 DOI、标题大改、作者增删、跨语言和 conference-to-journal 子集。自动 merge 阈值应由 precision 的置信区间约束，而不是从其他论文照抄。

LLM Skill 只在 CLI 判为灰区的 review bundles 上评 coverage、precision、defer rate、人工接受率和每例成本。强 DOI matches 不应混入制造虚高分数。最后还要测去重对 Citation edge 和 union count 的 distortion，因为一次误合并可能使检索指标看似改善，实际却把两个工作压在了一起。

### 6. Reference 解析与 linking

不能用一个“Reference F1”覆盖整个流水线。应分别测：

1. Reference section detection 和 entry segmentation，包括 exact/soft entry F1、split/merge 和 count MAE；
2. title、authors、year、venue、pages、DOI 等字段解析，区分 oracle entry 与端到端输入；
3. 正文 citation anchor 检测，以及 anchor-to-bibliography linking；
4. bibliography item 到 Manifestation/WorkFamily 的 entity linking，包括 recall@K、NIL/unresolved 和 precision-coverage curve；
5. 最终 Reference edge 的 micro/macro precision、recall、F1，同时报告 Manifestation-strict 与 WorkFamily-relaxed 两套分数。

LaTeX、JATS/XML、born-digital PDF 和扫描/OCR PDF 分层报告。GROBID 或 biblio-glutton 在已正确切分且有 DOI 的样本上取得高分，不代表整条系统恢复了同样比例的 Reference edges。必须另跑从原始文档到最终 Work edge 的端到端测试。

### 7. 可靠性、成本与可复现性

可靠性和数据覆盖分开评。核心指标包括首次与最终成功率、发生 429 的 operation 比例、retry amplification、p95 恢复时间、分页枚举完整性、checkpoint resume equality、缓存避免的请求数、同一 fixture 重放的确定性、字段与边的 provenance coverage、延迟和实际 API 成本。

真实 API 测试按各 provider 官方额度的 50%、80% 和接近 100% 归一化。过载与 `Retry-After` 主要用 fault injection，避免冲击公共服务。一次聚合任务输出 `complete / partial / degraded / failed`；成功但无数据的 `empty` 必须与 throttled、分页未完成区分。

最终报告不压成一个“综合分”。至少并列展示 retrieval recall、screening burden、ranking、entity quality、Reference quality、provider reliability 和成本。

## Notes

- 429、配额、接口变化和凭证过期需要作为 provider contract tests 长期维护，不能只在发布前手工试一次。
- 不同领域使用 arXiv、bioRxiv/medRxiv、SSRN、RePEc、ADS、INSPIRE、PubMed 等数据库的习惯不同。后续应按十几到二十个大类学科测试研究者真实的主题、字段化、Reference、Citation 和语义检索路径，逐步补齐领域适配；AI 样本不能代表全部领域。
- README 要按成熟开源项目维护：一句话定位、适用场景、安装、三分钟 quickstart、provider/capability matrix、真实输入输出、去重和 Reference/Citation 的含义、429 与 `doctor`、架构、贡献指南、路线图和 changelog。可参考 Agent Access 的 CLI 安装思路、[PaperQA](https://github.com/Future-House/paper-qa) 的示例与开发说明、[Zotero](https://github.com/zotero/zotero) 和 [GROBID](https://github.com/grobidOrg/grobid) 的长期项目文档；不必为了模仿 star 数堆徽章。
- 项目发布时要准备 GitHub launch、X/Twitter 和小红书文案。宣传重点不是“接了很多 API”，而是“四种论文检索统一入口、多源 Reference/Citation 聚合、预印本/会议/期刊跨版本去重、CLI-first 且可被 Skill 和 Agent 直接复用”。短文案可以是：`一个 CLI，统一主题、字段化、引文网络和语义检索。它从多个学术数据库取回论文级结果，验证 References、合并 Citations，并识别预印本、会议版和期刊版之间的关系。每个结果都能追溯来源，也能直接交给脚本或 Agent 使用。`

## 参考资料与推荐阅读

### 开源项目与实现

- [paper-search-mcp](https://github.com/openags/paper-search-mcp)
- [nature-academic-search](https://github.com/wp-a/nature-academic-search)
- [Paperoni](https://github.com/mila-iqia/paperoni)
- [paper-search-pro](https://github.com/O0000-code/paper-search-pro)
- [Academix](https://github.com/xingyulu23/Academix)
- [litdb](https://github.com/jkitchin/litdb)
- [Local Citation Network](https://github.com/LocalCitationNetwork/LocalCitationNetwork.github.io)
- [CitationChaser](https://github.com/nealhaddaway/citationchaser)
- [Citation Gecko](https://github.com/CitationGecko/gecko-react)
- [BibliZap](https://github.com/BibliZap/BibliZap)
- [Inciteful](https://inciteful.xyz)
- [Fatcat/refcat](https://github.com/internetarchive/fatcat)
- [Scholar、Semantic Scholar、ADS Citation 聚合样例](https://github.com/zjsxply/zjsxply.github.io/blob/main/.github/workflows/update-scholar-citations.yml)
- [GROBID](https://github.com/grobidOrg/grobid)
- [biblio-glutton](https://github.com/kermitt2/biblio-glutton)
- [AnyStyle](https://github.com/inukshuk/anystyle)
- [Crossref Marple](https://gitlab.com/crossref/marple/)
- [S2APLER](https://github.com/allenai/S2APLER)
- [PreprintMatch](https://github.com/PeterEckmann1/preprint-match)
- [fuzzycat](https://gitlab.com/internetarchive/fuzzycat)

### 数据库、图谱与 API

- [Semantic Scholar API](https://www.semanticscholar.org/product/api)
- [OpenAlex](https://help.openalex.org/)
- [OpenAIRE Research Graph](https://graph.openaire.eu/docs/)
- [OpenCitations Meta/Index](https://opencitations.net/)
- [AMiner Data API](https://open.aminer.cn/docs)
- [Wikidata/Scholia](https://scholia.toolforge.org/)
- [Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)
- [Europe PMC REST API](https://europepmc.org/RestfulWebService)
- [SerpApi Google Scholar API](https://serpapi.com/google-scholar-api)
- [Web of Science API Expanded](https://developer.clarivate.com/apis/wos)
- [Scopus APIs](https://dev.elsevier.com/scopus.html)
- [Dimensions DSL](https://docs.dimensions.ai/dsl/)
- [Lens Scholarly API](https://docs.api.lens.org/)
- [OpenAlex Semantic Search](https://help.openalex.org/api/semantic-search/)
- [Semantic Scholar Recommendations API](https://api.semanticscholar.org/api-docs/recommendations)
- [Exa Search API](https://exa.ai/docs/reference/search)
- [JATS ref-list 与 xref](https://jats.nlm.nih.gov/archiving/tag-library/1.3/element/ref-list.html)
- [PMC developer services](https://pmc.ncbi.nlm.nih.gov/tools/developers/)
- [arXiv bulk source](https://info.arxiv.org/help/bulk_data_s3.html)

### 推荐阅读：检索术语与方法

- [TARCiS statement](https://doi.org/10.1136/bmj-2023-078384)：引文检索术语、实施和报告的首选指南。
- [PRISMA-S](https://doi.org/10.1186/s13643-020-01542-z)：规范报告数据库、平台、检索式、cited/citing references 和检索日期。
- [Cochrane Handbook, Chapter 4](https://training.cochrane.org/handbook/current/chapter-04)：把数据库检索、引文索引和补充检索放入系统综述的完整流程。
- [Cooper et al. 2018](https://doi.org/10.1186/s12874-018-0545-3)：梳理系统综述中数据库检索与 supplementary search methods 的边界。
- [Wright et al. 2014](https://doi.org/10.1186/1471-2288-14-73)：用 sensitivity、precision 和 NNR 评估 citation searching 的案例。
- [Wohlin 2014](https://doi.org/10.1145/2601248.2601268)：软件工程领域迭代 backward/forward snowballing 的经典方法。
- [Kessler 1963](https://doi.org/10.1002/asi.5090140103)：bibliographic coupling 的经典定义。
- [Small 1973](https://doi.org/10.1002/asi.4630240406)：co-citation 的经典定义。

### 推荐阅读：系统、实体与引文图

- [The Semantic Scholar Open Data Platform](https://arxiv.org/abs/2301.10140)：理解多源摄取、论文去重、书目抽取与 citation linking 的生产流水线。
- [SPECTER](https://doi.org/10.18653/v1/2020.acl-main.207) 与 [SPECTER2](https://github.com/allenai/SPECTER2)：citation-informed scientific document representation，但不能把相似度误当引用事实。
- [OpenCitations Meta](https://doi.org/10.1162/qss_a_00292)：书目实体、外部 ID、引用实体和 provenance 分层设计。
- [PreprintMatch](https://doi.org/10.1371/journal.pone.0281659)：标题、摘要和作者联合进行 preprint-publication matching 的实证研究。
- [ASySD](https://doi.org/10.1186/s12915-023-01686-z)：来自真实系统综述多库结果的去重方法与人工 gold。
- [Gusenbauer et al. 2024](https://doi.org/10.1002/jrsm.1729)：59 个 citation indices 的跨学科覆盖比较。
- [Hirt et al. 2023](https://doi.org/10.1002/jrsm.1635)：citation tracking 工具与方法的范围综述。
- [CitationChaser paper](https://doi.org/10.1002/jrsm.1563)：批量 forward/backward citation chasing 工具与透明报告。

### 推荐阅读：评测

- [CoCites validation study](https://doi.org/10.1186/s12874-020-0907-5)：250 篇 review 的回溯性评测，展示 recall 与筛选负担必须同时看。
- [Cohen et al. 2006](https://doi.org/10.1197/jamia.M1929)：系统综述自动筛选和 Work Saved over Sampling 的经典来源。
- [Retrieval evaluation with incomplete information](https://doi.org/10.1145/1008992.1009000)：gold 不完整时 bpref 等评测方法的依据。
- [LitSearch](https://arxiv.org/abs/2407.18940)：自然语言科学文献检索 benchmark。
- [SciRepEval](https://arxiv.org/abs/2211.13308)：科学文献表示与 search、cite、co-cite、co-read 等任务的统一评测。
- [BEIR](https://arxiv.org/abs/2104.08663)：跨领域信息检索 benchmark，用于检查模型是否只在单一数据集上有效。
- [CSFCube](https://arxiv.org/abs/2103.12906)：按 background、method 和 result 分面的论文相似性评测。
- [GROBID end-to-end evaluation](https://grobid.readthedocs.io/en/latest/End-to-end-evaluation/)：Reference 分割、字段解析和端到端评测实现。
