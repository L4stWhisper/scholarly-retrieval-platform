# 测试与验收

本文是 Scholarly Retrieval Platform 唯一的测试说明和验收记录。它同时回答三个问题：代码契约是否稳定、真实学术数据源是否可用、Agent 是否能通过 MCP 完成完整检索流程。

## 1. 验收原则

### 关键词检索优化的跨领域对照（2026-09-19）

用户要求优化不能只对一篇论文生效。用 `git worktree` 检出优化前提交 f0188c9，与当前代码对同一组
查询各跑一次（默认五个来源、limit 10、同一时段），逐条人工判断切题性：

| 查询 | 领域 | 优化前 | 优化后 | 变化 |
|---|---|---:|---:|---|
| graph neural network molecular property prediction | 化学/ML | 10/10 切题 | 10/10 | 排名一致 |
| CRISPR base editing off-target effects | 生物医学 | 10/10 | 10/10 | 第 8 条 Crossref 与 Europe PMC 记录合并为一篇 |
| surface code quantum error correction threshold | 物理 | 10/10 | 10/10 | 排名一致 |
| large language model agents tool use | CS | 10/10 | 10/10 | 排名一致 |
| climate change impact on crop yields | 农业/气候 | 10/10 | 10/10 | 排名一致 |
| deep learning medical image segmentation | 医学影像 | 8/10：第 2、3 位是撤稿声明与被撤稿论文 | 10/10 | 撤稿声明排除、被撤稿论文降权 |
| natural language agent harness（前次记录） | CS | 5 重复 + 5 无关 | 11/12 切题 | 去重与主题词权重 |

结论：词干归一、IDF 平方与强标识归一在判别词充足的查询上不改变排名（无回归），在含通用词或版本
重复的查询上显著改善。对照过程新发现撤稿相关记录混入前列属于全局问题，随即补充规则：
勘误、撤稿声明等登记类型默认排除；被撤稿论文保留但相关性分数减半；显式 `--work-type` 时不排除。
`test_service` 新增对应契约测试。上述均为真实公网结果，来源排名会随索引变化。

### Semantic Scholar 无 key 访问（2026-09-19）

用户复测确认：退避机制正确执行（时间戳核对三次请求间隔约 2.3 秒与 4.1 秒）但匿名池始终 429，
"有机制没解决问题"。随后按端点探测发现匿名限流按端点划分：`/paper/search` 与 `/paper/{id}` 六次
全部 429；`/paper/search/bulk`、`POST /paper/batch`、`/paper/{id}/citations|references` 与
Recommendations 均 200，交替快速请求 10 次中 bulk 出现 2 次 429、batch 0 次，重试可覆盖。

处理：无 key 时 search 走 bulk（引用数降序、本地截取 limit、相关性交给服务层词项融合），
resolve 走 batch；有 key 时保留相关性端点并在 429 后回退；熔断器改为按路径计数，否则 5 次 429
会打开整个来源的熔断而拒绝回退请求。

真实 CLI，全部 `--source semantic_scholar`、无 key：search 5 篇（DPR、RocketQA 居前）、
resolve 1 篇、citations 42 篇、references 10 篇、related 5 篇，状态均 complete，每次约 17–21 秒
（含 Python 启动与 1.1 秒最小间隔）。离线回归：provider 测试新增 5 项、可靠性测试新增 1 项。

### 关键词检索精度、Semantic Scholar 429 与配置查找顺序（2026-09-18）

- Semantic Scholar 持续 429 的根因：`.env` 中 `SEMANTIC_SCHOLAR_API_KEY` 为空，匿名流量共享全球
  配额池。直接探测 `GET /graph/v1/paper/arXiv:2603.25723` 六次全部 429 且无 `Retry-After`。
  项目的指数退避本身有效（5 次、2/4/8/16 秒并遵守 Retry-After），但退避无法换来配额。
  处理：匿名模式改为 3 次重试并在 `error_message` 附带 `throttle_hint`；配置 key 后仍为 5 次。
- 关键词检索 `natural language agent harness`（openalex,crossref,arxiv,europe_pmc，limit 10）
  修复前：前 10 条中 5 条是重复（Preprints.org `.v1/.v2/.v3` 三条、arXiv 与 OpenAlex 的同一篇
  两条），另有 3 条只匹配通用词的无关老论文与 2 条 Crossref `component` 补充材料记录。
  修复后（强标识 key 归一、词干归一、IDF 平方、排除非论文类型）：limit 12 中 11 条切题，
  重复全部合并；`dense retrieval open-domain question answering` 12 条全部切题，
  `retrieval augmented generation hallucination` 10 条全部切题。均为真实 CLI 结果，未使用 mock。
- 配置：`load_environment` 查找顺序改为显式路径、`./.env`、`~/.config/scholarly-retrieval/.env`，
  进程环境变量始终优先；`scholar doctor` 显示实际加载的文件。
- 离线回归：`test_identity` 新增 3 项、`test_service` 新增 3 项、`test_config` 新增 2 项、
  `test_semantic_scholar_provider` 新增 1 项；文档契约测试改为 README 入口 + `docs/` 指南。

### Google 陈旧快照页校验与 seed 标称总数下限（2026-09-18）

参考 zjsxply.github.io 在 2026-09-10 与 09-14 的三次提交（`bin/update_scholar_citations_serpapi.py`）。
对方做法：每个 cites ID 单独抓取、num=20、filter=0、只跟随 next 并保留 start/as_sdt/sciodt/scipsc/filter；
整体结果少于上次缓存计数时用 no_cache 重跑一遍取较多者；最后经验性写死 `as_sdt=0,27`
（注释称 as_sdt=0/省略/0,26 得 27–28 条，0,27 连续三次得 51 条）。

本项目不经过项目代码、直接向 SerpApi 探测（本机无 HTTP(S)_PROXY，请求只到 serpapi.com）。
主 ID `5554083676653175677`，num=20、filter=0、no_cache=true，各页记为 (start, 返回, 标称)：

| as_sdt | 唯一 result ID | 各页 |
| --- | ---: | --- |
| 0 | 34 | (0,20,21) (20,20,47) (40,7,47) |
| 0,27 | 38 | (0,20,28) (20,20,47) (40,7,47) |
| 省略 | 47 | (0,20,47) (20,20,47) (40,7,47) |
| 省略，第 2 次 | 47 | 全部标称 47 |
| 0,27，第 2 次 | 47 | 全部标称 47 |
| 省略，第 3 次 | 33 | (0,20,47) (20,13,33) |
| 0,27，第 3 次 | 20 | (0,20,47) (20, 无结果错误) |
| 省略，num=10 | 20 | (0,10,23) (10,10,20) |

第二 ID `10581113726319067053` 单页各请求四次，(返回, 标称)：
省略 2/4/4/6；0,27 4/5/6/6；0,5 5/6/5/错误；2005 四次全部错误。

结论：同一 cites 列表由互不一致的索引快照响应，页面标称总数在 20–47（或 2–6）之间跳动；
小快照页与大快照页重叠，按 result_id 合并后偏少。`as_sdt=0,27` 不是确定性修复（第 3 次仅 20 条），
也不采用。三个候选原因中：上游后端快照不一致是主因；代理可排除；项目代码原本没有识别陈旧页的机制。

修复（provider `_citation_pages`）：每页 20 条；每页标称总数与本次已见最大值比较，更小、或 next
承诺的页为空即判定陈旧页，在每轮 `SCHOLAR_GOOGLE_PAGE_RETRIES`（默认 3）预算内立即重拉；
所有尝试的结果都并入观察集；seed 记录的 `cited_by.total` 经 `traversal_identifier` 记为该 cites ID 的
总数下限，列表始终达不到下限时保持 partial。停止规则不变：连续两轮相同 ID 集合且无总数缺口。

真实 CLI（页级重拉已加入、标称下限加入前）：
`citations google_scholar:5554083676653175677,10581113726319067053 --source google_scholar_serpapi --limit 100 --format json`

- 主 ID：两轮各 47 个不同 ID，两轮均 consistent；陈旧页重拉分别触发 1 次（start=40 空页）和 3 次
  （start=20 标称 33），stop_reason=stable，complete。此前记录的最好结果为 46。
- 第二 ID：两轮各 2 条且标称 2，stable。这暴露了"本次运行从未见到 6"时无法察觉缺口，
  因此补充 seed 标称下限。整体 status complete，49 篇。

加入标称下限后的真实 CLI 未能执行：SerpApi 免费额度（250 次/月）在本次探测后用尽，返回 http_429。
第二 ID 的下限逻辑仅由离线测试与上面的直接探测（6 出现在约 1/4–2/4 的新鲜请求中）支持，
尚无端到端实测；额度恢复后应重跑上述命令核验第二 ID 是否达到 6。

离线回归：新增 `tests/test_scholar_snapshot_consistency.py` 7 项（陈旧首页重拉、next 承诺空页重拉、
预算耗尽保留数据并 partial、page_retries=0、环境变量、标称下限触发重拉、标称下限未达保持 partial）；
更新 `test_citation_sources.py` 的偏移断言。全量 272 passed、33 skipped；Ruff check 通过。

### 官方分页对照与原始 HTML 定位（2026-09-16）

目的：区分“本地参数/解析丢失”和“SerpApi 返回时已经缺失”。使用
`tools/diagnose_scholar_pagination.py` 从终端发起真实请求，不用 Claude/MCP、不经过本地缓存。
固定主 cites ID、num=10、hl=zh-CN、as_sdt=2005、no_cache=true，分别比较原样保留官方 next
查询参数与项目 `_next_params` 的请求构造；两轮交替执行顺序。不是并发访问的同一份索引快照。

| 条件 | 第一轮不同 result ID 数 | 第二轮不同 result ID 数 |
| --- | ---: | ---: |
| 官方 next，filter=0 | 15 | 13 |
| 项目 next 参数重建，filter=0 | 15 | 18 |
| 官方 next，不传 filter（默认过滤） | 15 | 18 |

共 14 次 search 请求，所有实际 next 链接的参数对照差异均为空；默认过滤也复现缺页。
第一组官方请求第一页 total=46、10 条，第二页 total=15、5 条且无 next。
随后通过官方 Search Archive API 对**这两次原请求**读取 JSON/HTML（4 次归档读取）：

| 页偏移 | search_metadata.id | JSON 条数 | 原始 HTML 论文标题数 |
| --- | --- | ---: | ---: |
| 0 | `6aaa56707dcfbee8bca251d2` | 10 | 10 |
| 10 | `6aaa5671e3e176a9451bd910` | 5 | 5 |

第二页 HTML 的论文 result ID 与 JSON 一致，也没有 start=20 的分页链接。
这证明**本次缺失发生在数据进入本地聚合之前，且不是这份 JSON 比归档 HTML 少解析了论文**。
不能进一步据此断言究竟是 Google、SerpApi 出口/会话或其他抓取条件造成；需服务商调查。
参数白名单和 filter=0 不是本次样本缺失的已证实原因，不能再当作既定 bug 解释。
多轮曾取得 46 条，但上述受控实验仍不稳定；因此不承诺每次精确复现用户浏览器的完整列表。

本次增加每页 search_id 审计，便于按官方归档复核；不改变用户精简输出。
离线专项回归：26 passed（诊断工具、缓存、分页、身份/多源聚合）；Ruff 通过。
这些测试证明处理契约，不证明 Google 全库召回率。
依据：[Scholar API](https://serpapi.com/google-scholar-api)、
[Search Archive API](https://serpapi.com/search-archive-api)。未向 SerpApi 发送反馈或上传项目资料。

### 多 cluster / cites 与多轮一致性召回（2026-09-16，当前实现）

本次只使用 CLI 进行真实验证，没有调用 Claude Agent 或 MCP。下面的旧分页探测记录仅为历史诊断，
固定偏移探测已由 next 链接驱动的多轮召回替代。

- 最终离线全量回归：261 passed、33 skipped（175.38 秒；真实联网/OCR 测试未在全量命令中启用）。
  Ruff 与 git diff --check 均通过。
- `test_scholar_consistency.py` 覆盖：cluster 与 cites 不相等、All Versions 遍历和分页、冲突 seed
  排除、显式未核验 ID 提示不伪造身份 claim、不同 cites 保留重复记录、本地/SerpApi 双缓存绕过、
  非法/循环/换 seed 分页、相同数量但不同 ID 不算稳定、总数缺口、非标准偏移 next 链接。
- `test_citation_sources.py` 验证第一次缺失 next 后在新一轮恢复、后页失败保留已取论文；
  两个 Google 列表和另一个来源共 3 条原始观察，在 service 统一合并为 1 篇，保留两个 seed_cites_id。
- 真实 CLI：`citations https://arxiv.org/abs/2603.25723 --source arxiv,google_scholar_serpapi,ads
  --limit 100 --format json`。arXiv 仅用于 seed 解析；Google 主列表各轮观察到 18、19、17、17 条，
  四轮累计 33 条；ADS 返回 21 条；原始 54 条，统一去重后 47 篇，状态 partial。
- 随后的双 ID CLI 诊断中，主 ID 四轮分别得到 20、3、46、22 个不同 result ID；第三轮
  start=0/10/20/30/40，返回 10/10/10/10/6，累计及最终去重结果均为 46。
  第一页曾报告 46、后页却报告 15 并返回空；因此 no_cache 与多轮确实恢复了单轮丢失结果。
  本次只有主 ID 通过自动核验，暴露出显式第二 ID 会被忽略的问题，随后补充 caller-supplied
  提示保留机制。四轮集合仍不一致，因此 46 条不等于已证明稳定或全网完整，仍标记 partial。
- 单独用 Google 对 DOI 或主 cluster 解析曾返回 empty；增加其他来源的 seed 元数据可恢复。
  这是来源解析覆盖限制，不能把 empty 等同于该论文没有 citations。
- 最终双 ID CLI 已跑通：仅在测试进程设置 SCHOLAR_GOOGLE_CITATION_ROUNDS=2，
  seed 小候选解析使用 num=3；主 ID 两轮观察 18、40，累计 40 条；第二 ID 两轮各 2 条且稳定。
  原始 42 条交给统一聚合，最终 42 篇。主 ID 仍有 46 的已知缺口，整体 partial。
  这与前次主 ID 46 条是不同运行，不能把两次数字相加当作一次召回；全网完整性仍未证明。
  所有这些数据由真实 CLI 获得，不是 mock 断言替代。

执行命令与预算、每轮 pages/stop_reason 均可从 JSON 的 provider_reports context 审核；
若要跨进程保留原始响应，需要配置 SCHOLAR_DB_PATH。精简/详细阅读输出不展示这些调试字段。

### 用户 Google Scholar URL 对照（2026-09-16）

用户提供主 cites ID `5554083676653175677`（网页46条、5页）与另一个
`10581113726319067053`，网页参数为 as_sdt=2005、sciodt=0,5、hl=zh-CN。
通过 SerpApi 逐页请求主 ID，start=0/10/20/30/40 返回条数为10/10/10/10/0，
对应报告总数16/46/46/46/16，跨页按 result_id 合并仅33条。
参数回显包含 hl 与 as_sdt，未包含 sciodt，不能宣称网页请求完全等价。

仅对异常首页与末页设置 no_cache=true 再查：首页报告15，末页报告46并实际返回6条。
故缓存不是已证实的唯一原因；当前访问链路存在页面计数和列表不一致，未验收完整46条召回。

第二 ID 的 cluster 查询返回标题 “Natural-language agent harnesses, 2026”，作者
L Pan、L Zou、S Guo、J Ni、HT Zheng，出版信息指向 arxiv.org/abs/2603.25723，
没有独立 landing link，被引计数6。该证据支持同一论文的另一引文索引记录，
不支持“存在第二个论文版本”的结论；两个 ID 的被引列表不可直接相加。
随后 CLI 单独查询第二 ID 返回2条（其本次列表报告总数也为2），与元数据计数6不一致，
同样不能用单次 complete 状态证明覆盖完整。

### Google Scholar 分页不一致与 ADS 授权实测（2026-09-15）

- 用户配置 ADS token 后，修复分页前四源 CLI 实测：OpenAlex 1、Semantic Scholar 42、
  Google Scholar 20、ADS 21，聚合去重后 59。
- 对同一 Google cluster 使用 num=10、filter=0：start=0 报告总数22、返回10；
  start=10 报告18、返回8且无next；继续 start=20 报告46、返回10且有next。
  证明缺失 next 不足以断言终点，也不能用返回条数推进稀疏分页偏移。
- 已实现有界探测、固定页宽偏移、跨页 ID 去重和保留失败前结果。
  新增两项回归覆盖假终点/重复记录与后续页故障；citation_sources 10 项通过，Ruff 通过。
- 修复后四源 CLI：OpenAlex 1、Google Scholar 27（报告总数最大46、truncated=true）、
  ADS 21，Semantic Scholar 429；49 条原始记录聚合为43篇，整体 partial。
  仍未获得 Google 声称的全部46条，不把该次结果当作完整召回。

### 通用 citation 聚合与 ADS 接入（2026-09-15）

- 新增 `tests/test_citation_sources.py`：8 项通过。覆盖 ADS 鉴权缺失/401、ID 规范化、
  引用方向、分页与截断，Google Scholar 空结果与配额错误区分，以及通用标题回退、
  两个 cluster 合并、冲突候选排除、跨源 DOI 去重和来源保留。测试使用虚构论文，
  不将真实样例 ID 写入生产逻辑。
- 既有 SerpApi 与 service 回归：34 passed。Ruff 检查通过。
- 全套自动化测试：247 passed、33 skipped；跳过项为需显式开启的联网/OCR 测试，
  本轮真实联网检索另以 CLI 完成，结果如下。
- CLI 实测 `citations W7141256605 --source openalex,semantic_scholar,opencitations,google_scholar_serpapi --limit 100 --format json`：
  Google Scholar 通过元数据回退自动解析，返回 17 条，OpenAlex 1 条；
  Semantic Scholar 此次重试 5 次仍 429，OpenCitations 无 seed，结果 partial，共 18 篇。
- 随后以 `https://arxiv.org/abs/2603.25723` 为输入，sources 为
  `openalex,semantic_scholar,google_scholar_serpapi`，limit=100：
  OpenAlex 1、Semantic Scholar 42、Google Scholar 17，原始 60 条，最终 papers 数量
  **50**，其中后两源重叠 10 条。三个来源报告均 complete，但不意味着全网完整召回。
- ADS_API_TOKEN 未配置；NASA ADS 已实现并通过模拟接口测试，尚未进行带授权的真实查询。
  不能将三源实测结果称为 ADS＋Google Scholar＋Semantic Scholar 的实测结果。

### CLI 阅读输出更新（2026-09-15）

- `references/citations` 默认 `compact`；`detailed` 增加年份、作者、期刊/会议和可用 PDF。
- 定向回归 `python -m pytest tests/test_cli.py -q -k "reading or compact_audit"`：
  3 passed。覆盖 25 篇全部展示、长标题不截断、多源链接关联、隐藏内部错误、
  详细字段、空结果及默认 CLI 模式。CLI 全套测试：18 passed；相关文件 Ruff 检查通过。
- 真实 CLI 使用 DOI `10.48550/arxiv.2603.25723`、`--source openalex --limit 100`，
  分别执行 `--format compact` 和 `--format detailed`，均成功返回 1 篇：
  Reproducible and shareable bioinformatics pipelines from natural-language prompts。
  DOI 与 OpenAlex 链接完整显示，详细模式显示作者、2026 年及 bioRxiv 平台。
  此数量仅验证该来源当次结果与输出，不是该论文全网引用数或多源召回验收。

测试分为三层，三层不能互相替代：

1. **离线自动化测试**：使用固定响应、故障注入和本地样本验证业务规则，可重复、无公网依赖；
2. **真实 Provider smoke**：低负载访问真实公开接口，验证协议和上游数据格式没有漂移；
3. **Claude Code MCP smoke**：从真实 Agent 客户端调用已暴露的全部 MCP 工具，验证安装、进程启动、工具 schema、网络调用和结果回传的完整链路。

真实 smoke 的目标是验证功能和契约，不是证明科学意义上的完全召回。公开 API 的排序、索引更新、限流和可用性随时可能变化，因此测试只断言稳定事实：响应结构、状态语义、已知标识解析、边方向、非伪造能力以及合理的最小命中。

## 2. 自动化测试矩阵

| 范围 | 主要验证点 | 代表性测试 |
|---|---|---|
| 模型与序列化 | Paper、标识符、状态、cursor、稳定 fingerprint | `test_models.py`、`test_serialization.py` |
| Provider 适配器 | 请求映射、响应解析、404、429、分页、能力声明 | `test_*_provider.py` |
| 聚合与去重 | 强标识合并、标题灰区、字段选择、provenance | `test_dedup.py`、`test_field_merge.py` |
| 四种检索 | keyword、advanced、reference/citation、related | `test_service.py`、`test_related.py` |
| 引文图 | `citing -> cited`、多源 assertion、图预算与路径 | `test_graph.py`、`test_citation_*.py` |
| Reference pipeline | JATS/TEI/LaTeX/BibTeX/PDF、候选评分、拒绝灰区 | `test_reference_*.py`、`test_grobid_client.py` |
| 持久化与评估 | SQLite、缓存、维护、review、gold/evaluation/export | `test_storage.py`、`test_evaluation.py`、`test_exporters.py` |
| 接口一致性 | CLI、FastAPI/OpenAPI、MCP 九工具 schema | `test_cli*.py`、`test_api*.py`、`test_mcp*.py` |
| 生产边界 | 鉴权、限流、请求体限制、Redis jobs、OCR 安全限制 | `test_security.py`、`test_redis_*.py`、`test_ocr_*.py` |
| 仓库契约 | 三主文档、链接、入口点、Skill 聚焦、密钥忽略 | `test_repository_docs.py` |

标准离线验收命令：

```powershell
python -m ruff check src tests
python -m compileall -q src tests
python -m pytest -q
```

预期：ruff 和 compileall 退出码为 0；pytest 无失败。带有 `live` 或 `ocr` 标记的环境型用例在未显式启用时应跳过，而不是偷偷访问公网或本机容器。

## 3. 真实数据源验收

显式启用公网测试：

```powershell
$env:SCHOLAR_RUN_LIVE_TESTS = "1"
python -m pytest -q tests/test_live_provider_contracts.py tests/test_live_open_metadata_providers.py
```

测试覆盖：

- keyword：OpenAlex、OpenAIRE、Semantic Scholar、Crossref、DataCite、DBLP、arXiv、Europe PMC、INSPIRE、OpenReview、ACL Anthology；
- advanced：作者、年份、开放获取、最小引用数、排序及过滤位置；
- references/citations：已知 DOI 的双向关系、OpenAlex 数量核对、Europe PMC/INSPIRE/OpenCitations 适用案例；
- related：文本检索、多检索器融合、正负种子与 ranking evidence；
- reference linking：公开 JATS 中的正文引用、书目条目、候选论文和最终边；
- 可选 Google Scholar/SerpApi：仅在 `SERPAPI_API_KEY` 存在时运行。

单独验证新增领域源：

```powershell
$env:SCHOLAR_RUN_LIVE_TESTS = "1"
python -m pytest -q tests/test_live_open_metadata_providers.py `
  -k "openreview or acl_anthology"
```

不要固定断言公网搜索的完整标题列表或顺序。失败时保留 `provider_reports`、HTTP 状态、截断原因和源贡献，再判断是代码回归、凭证问题、限流还是上游漂移。

## 4. Claude Code 全工具 smoke

### 4.1 前置检查

```powershell
python -c "import sys; print(sys.executable)"
Get-Command scholar-mcp
claude --version
claude mcp get scholarly-retrieval
```

最后一条应显示 MCP 已连接。Claude Code 可以安装在任意环境；关键是 `.mcp.json` 中的命令能从 Claude 进程的 PATH 找到，或改用虚拟环境 Python 的绝对路径。

### 4.2 smoke 案例

Agent 验收采用很小的 limit，并要求 Claude 只返回检查清单，避免把大量论文元数据塞入上下文。

| 编号 | MCP 工具 | 案例与通过条件 |
|---|---|---|
| C01 | `providers` | 返回注册来源及真实能力，不把 count 冒充 list |
| C02 | `search` | 关键词检索 AlphaFold，OpenAlex 返回结构化论文 |
| C03 | `search` | 同一工具使用年份、OA、排序等高级字段，报告过滤位置 |
| C04 | `resolve` | DOI `10.1038/s41586-021-03819-2` 解析到 AlphaFold 论文 |
| C05 | `references` | 返回 seed 引用的论文，边方向为 `seed -> cited` |
| C06 | `citations` | 返回引用 seed 的论文，边方向为 `citing -> seed` |
| C07 | `related` | 文本相关性检索返回 ranking evidence，不宣称概率 |
| C08 | `expand` | `depth=1` 的有界图扩展 obey frontier/top-k/per-node budgets |
| C09 | `extract_references` | 从内嵌 JATS 提取正文 marker 和 DOI 书目条目 |
| C10 | `link_references` | 将上一条 extraction 解析成候选/已链接实体，灰区不强连 |

仓库中用于验收的非交互命令应通过 `--allowedTools` 只开放九个项目工具，使用 `--permission-mode dontAsk` 和 `--no-session-persistence`。权限名必须保留 `.mcp.json` server 名中的连字符，例如
`mcp__scholarly-retrieval__search`。提示词要求每项输出 `PASS`、`FAIL` 或 `PARTIAL`，并附一行可诊断证据；任何 `PARTIAL` 必须区分代码错误与正常的上游退化。

## 5. OCR 与容器验收

离线 OCR 单元测试验证命令构造、超时、大小限制、临时文件清理和失败语义。真实扫描 PDF 是显式、可选的环境验收：

```powershell
docker compose -f compose.ocr.yaml up -d
$env:SCHOLAR_RUN_OCR_TESTS = "1"
python -m pytest -q tests/test_live_ocr_pipeline.py
```

通过条件是 OCRmyPDF 能为 image-only PDF 生成文字层、GROBID 返回 TEI、Reference pipeline 给出结构化条目。没有可工作的 Docker/Podman engine 或镜像时记为“未执行”，不能记成产品功能通过。系统不会自行下载测试论文，也不会绕过付费墙或访问控制。

## 6. 失败定位顺序

1. 安装错误：确认 `sys.executable`、`Get-Command scholar-mcp` 和 editable install 所在环境；
2. MCP 启动错误：检查 `.mcp.json`、绝对命令路径及 `claude mcp get`；
3. 参数/schema 错误：以 MCP 客户端提供的 tool schema 或 `scholar COMMAND --help` 为准；
4. Provider 错误：检查 `provider_reports.status`、HTTP 状态、凭证存在性、cursor 和 `truncated`；
5. 数据合并错误：检查 `source_records`、`field_claims`、`identity_decisions`；
6. 引文错误：检查 assertion 的原始方向、规范边 `citing -> cited` 和 verification status；
7. 上下文过大：降低 limit/depth，先选 seed 再按需 resolve 或追踪关系。

## 7. 当前验收记录

| 日期 | 环境 | 项目 | 结果 |
|---|---|---|---|
| 2026-09-03 | Windows / Python 3.13.5 | OpenReview search、ACL Anthology search/resolve | 3 passed，53.76 s |
| 2026-09-03 | Windows / Python 3.13.5 | ACL advanced author/year/OA | 1 passed，26.54 s |
| 2026-09-03 | Windows / Python 3.13.5 | 文档收敛前完整离线测试 | 227 passed，33 skipped |
| 2026-09-03 | Windows / Python 3.13.5 | 文档收敛后 ruff/compileall/pytest | ruff、compileall 通过；227 passed，33 skipped，140.32 s |
| 2026-09-03 | Claude Code 2.1.62 / stdio MCP | Claude Code C01-C10 | 九个 MCP 工具全部真实调用成功；见下表 |
| 2026-09-14 | Windows / Python 3.13.5 / CLI | S2 429、跨源 seed、Google 多 cluster、关键词召回回归 | ruff/compileall 通过；236 passed，33 skipped；见 7.1；未使用 Claude/MCP |

### 7.1 2026-09-14 CLI 真实回归

本轮只运行 `python -m scholarly_retrieval` CLI，没有启动 Claude Agent 或 MCP。已确认：

- `citations W7141256605 --source openalex,semantic_scholar,crossref,opencitations --limit 100`：OpenAlex
  当前真实索引为 1；OpenAlex ID 已转译为 DOI 后交给其他来源；匿名 Semantic Scholar 在 5 次有界
  退避后仍受 429 限流，因此最终仍是 1 条 `partial`。重试生效不等于上游最终会分配配额。
- Google Scholar DOI 单 cluster 返回 15 条。调用两个历史 cluster 时，第一个可遍历，第二个被 SerpApi
  判为不可用；容错实现保留可用簇并正确返回 `partial`，不会再出现 `failed` 却携带 papers 的矛盾状态。
- 关键词案例 `dense retrieval open-domain question answering`，来源为 OpenAlex/Crossref/arXiv/DBLP，
  `limit=20`。用 10 篇人工标题诊断集计数，修改前后 Recall@20 均为 8/10；修改前明显离题标题为
  4/20，修改后为 0/20。结论是排序精度明显改善，但不能据此宣称全领域 recall 已提升。

该 10 篇小型诊断集见 `benchmarks/keyword-recall-diagnostic.json`。它用于发现排序回归，不是经过双人
标注的可发表 gold，也不能代表其他领域或数据库全库覆盖。

Claude Code 最终证据摘要：

| 案例 | 结果 | 实际证据 |
|---|---|---|
| C01 | PASS | `providers` 返回 13 个当前配置来源；12 个开放来源加已配置的可选 SerpApi 来源 |
| C02 | PASS | OpenAlex 关键词检索返回 2 篇结构化论文 |
| C03 | PASS | 年份 2021-2022、OA、newest 高级检索返回 2 篇；Claude 首次错误封装 sort 后按字符串重试成功 |
| C04 | PASS | OpenAlex 与 Crossref 均解析已知 AlphaFold DOI |
| C05 | PASS | 返回 2 条 references，边为 AlphaFold seed `->` cited work |
| C06 | PASS | 返回 2 条 citations，边为 citing work `->` AlphaFold seed |
| C07 | PASS | 返回 2 条 related 结果及 RRF evidence |
| C08 | PASS | depth 1、frontier/top-k/per-node 均为 2，返回有界两节点结果 |
| C09 | PASS | 内嵌 JATS 提取 1 条带 verified anchor 的 Reference |
| C10 | PASS | 真实 BERT `W2963341956` `->` ELMo `W2787560479`：`doi_resolve`、1 条 verified edge |

首次一体化提示暴露并修复了 README 中 `--allowedTools` 把连字符误写成下划线的问题。C09 到 C10
第一次串联时 Claude 试图借助未授权 Bash 暂存嵌套 JSON，因 `dontAsk` 被拒绝；随后用 MCP 原生结构化
参数直接重试成功。该现象属于 Agent 提示/权限编排，不是 MCP server 或 linking 代码错误。最终每个工具
均无 schema、启动或业务异常，但不能把“单个长提示必然一次编排成功”视为已证明的性质。

“passed”只代表相应验收范围。尤其是公网 smoke 不证明对某主题的完整召回，Reference 功能 smoke 不替代跨领域人工 gold 的精确率/召回率评估。

## 8. 发布门槛

初步版本可以发布需同时满足：

- 离线自动化测试无失败，README、`docs/` 指南与技术/验收文档的本地链接有效；
- 四种检索、resolve、图扩展、Reference extraction/linking 均有契约测试；
- 至少一个无 key 的真实来源完成 keyword、advanced、references、citations、related smoke；
- Claude Code 能发现 MCP 并完成 C01-C10，或将确属上游限流的单项明确记录为 `PARTIAL`；
- `.env` 被忽略，日志、文档和 smoke 输出均不泄露密钥；
- 未运行的 OCR、SerpApi 或多副本环境测试被明确标记，不能用离线 mock 代替真实验收结论。
