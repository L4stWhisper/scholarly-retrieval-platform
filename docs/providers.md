# 数据来源

Registry 默认包含 12 个开放来源；配置 `SERPAPI_API_KEY` 和 `ADS_API_TOKEN` 后分别启用
Google Scholar（SerpApi）和 NASA ADS，最多 14 个来源。

| Provider 名 | Search | Resolve | References | Citations | 需要 key |
|---|---:|---:|---:|---:|---:|
| `openalex` | 是 | 多种 ID | list | list | 否，生产推荐 key |
| `openaire` | 是 | DOI/arXiv/PMID/OpenAIRE | none | none | 否 |
| `semantic_scholar` | 是 | 多种 ID | list | list | 否，建议 key（无 key 走 bulk/batch 端点） |
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

CLI 默认来源是 `openalex,semantic_scholar,crossref,arxiv,europe_pmc`，用 `--source` 覆盖。

## 按领域选源

```bash
scholar search "BERT pre-training" --source acl_anthology,dblp,arxiv --limit 10
scholar search "vision transformer" --source openreview,openalex --limit 10
scholar search "gauge gravity duality" --source inspire,openalex,arxiv --limit 10
scholar search "cancer immunotherapy" --source europe_pmc,openalex,crossref --limit 10
```

## 多源去重

所有来源的原始记录在多源检索完成后进入同一次身份解析：按 DOI、arXiv、PMID/PMCID 强标识合并，
DataCite 的 arXiv DOI（`10.48550/arxiv.<id>`）等价于 arXiv ID，arXiv 版本号与预印本服务器的版本
后缀 DOI（`.v2`、`/v1`）归为同一作品。题名相似但强标识不同的记录只进入人工复核。单个来源不会提前
去重，Google Scholar 两个 cites ID 的列表也是与其他来源一起统一合并。

## Semantic Scholar 限流与匿名访问

Semantic Scholar 按端点分别限流。未配置 `SEMANTIC_SCHOLAR_API_KEY` 时，`/paper/search` 与
`/paper/{id}` 共享的匿名池几乎总是耗尽，而 `/paper/search/bulk`、`/paper/batch`、
`/paper/{id}/citations|references` 和 Recommendations 通常可用。因此：

| 操作 | 有 key | 无 key（或有 key 但 429 后回退） |
|---|---|---|
| search | `/paper/search`（相关性排序） | `/paper/search/bulk`，按引用数降序取前 limit 条，相关性由本项目的词项融合提供 |
| resolve | `/paper/{id}` | `POST /paper/batch`（未知 ID 返回 null，映射为未找到） |
| references / citations / related | 同一端点 | 同一端点 |

回退发生时来源报告 `context.endpoint` 与 `fallback_reason` 会说明实际端点；bulk 模式下
`open_access=false` 在本地过滤并标记 `local`。熔断器按端点路径独立计数，一个端点被限流不会
阻断同一来源的其他端点。

匿名端点同样可能被限流。此时来源报告为 `throttled`，`error_message` 说明原因与申请地址，
`context.attempt_count` / `retry_delays` 记录实际重试。匿名模式重试 3 次（约 6 秒）以免拖慢多源
检索；配置 key 后重试 5 次、约 2/4/8/16 秒退避，并遵守 `Retry-After`。

[免费申请 key](https://www.semanticscholar.org/product/api) 可获得独立配额和相关性排序的搜索端点。
重复实验建议同时设置 `SCHOLAR_DB_PATH` 复用成功响应缓存。

## arXiv 论文的多源被引聚合

arXiv 页面的 ADS、Google Scholar、Semantic Scholar 是外部索引入口，各自的引用覆盖可能不同。
CLI 会调用配置的数据源，再按论文身份聚合去重；一个来源的计数不是全网总数。

```bash
# 无需 Semantic Scholar key 即可尝试；匿名走 bulk/batch 端点，仍可能限流
scholar citations "https://arxiv.org/abs/2603.25723" \
  --source openalex,semantic_scholar,google_scholar_serpapi --limit 100

# 配置 ADS_API_TOKEN 后加入 NASA ADS
scholar citations "https://arxiv.org/abs/2603.25723" \
  --source openalex,semantic_scholar,google_scholar_serpapi,ads --limit 100
```

## Google Scholar（SerpApi）

Google 没有官方 Scholar API。本项目只通过用户授权的 SerpApi 访问，绝不直接抓取
scholar.google.com；SerpApi 与所有同类代抓服务一样，上游权利由用户自行承担。SerpApi 免费套餐
每月 250 次搜索，一次两 ID 的被引检索约消耗 25 次，请把 Google 当作补漏来源。

### seed 发现

解析 seed 后继续检索标题和强标识符，沿匹配记录的 All Versions 发现更多 cluster。候选须通过标识符
或标题、作者、年份核验；分别保存 `versions.cluster_id` 与 `cited_by.cites_id`，不假设两者相等。
显式 `google_scholar:ID1,ID2` 是用户指定的被引列表提示：无法通过 All Versions 核验的 ID 仍会抓取，
但保留 `caller_supplied_not_verified` 标记。请只传入已人工确认属于同一 seed 的 ID。

### 被引分页与陈旧快照

Google 会用互不一致的索引快照响应同一个 cites 列表：某页标称 21 条而下一页标称 47 条，或 next
链接指向的页返回空；小快照页与大快照页重叠，简单合并后条数偏少。被引抓取因此使用多轮一致性召回：

- 每轮从第一页开始，只跟随 `serpapi_pagination.next`；每页 20 条（Scholar 上限），`filter=0`，
  每次 `no_cache=true` 并绕过本地 HTTP 缓存；
- 每页的标称总数与本次已见最大值比较，标称更少或 next 承诺的页为空即判定为陈旧页，在每轮
  `SCHOLAR_GOOGLE_PAGE_RETRIES`（默认 3）预算内立即重拉，所有尝试结果并入观察集；
- seed 记录上 Google 自己标注的 "Cited by N" 作为该列表总数的下限；
- 连续两轮完整分页得到相同结果 ID 集合、且无总数缺口时停止，默认最多
  `SCHOLAR_GOOGLE_CITATION_ROUNDS=4` 轮（至少 2）；
- 达到预算、集合不稳定或存在缺口时在 JSON 结果中标记 `partial`，已获取记录仍会返回。

这不保证枚举 Google 内部全部 cluster，也不保证拿到网页标称的全部被引。`as_sdt` 之类的 URL 参数
只改变采样路径，不是稳定修复，因此不采用。每页附带 `search_metadata.id`，可用 SerpApi 的
Search Archive 复核同一请求的 JSON 与 HTML；细节见 `provider_reports[].context.cluster_outcomes`。

维护者可以运行官方分页对照诊断（消耗 SerpApi 额度；默认最多 20 次搜索）：

```bash
python tools/diagnose_scholar_pagination.py --cites 5554083676653175677 --rounds 2 --max-pages 5 --live
```

### 替代来源

支持 `cites` 与 `cluster` 参数的代抓服务还有 SearchApi.io 与 Scrapingdog，接入方式与 SerpApi 类似，
但同样受上述快照不一致影响。开放来源（OpenAlex、Semantic Scholar、ADS、OpenCitations）合并后
通常已接近 Google 的覆盖，Google 独有的多为网页 PDF、学位论文和未入库预印本。
