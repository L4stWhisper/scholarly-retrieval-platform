# 测试与验收

本文是 Scholarly Retrieval Platform 唯一的测试说明和验收记录。它同时回答三个问题：代码契约是否稳定、真实学术数据源是否可用、Agent 是否能通过 MCP 完成完整检索流程。

## 1. 验收原则

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

- 离线自动化测试无失败，三份主文档链接有效；
- 四种检索、resolve、图扩展、Reference extraction/linking 均有契约测试；
- 至少一个无 key 的真实来源完成 keyword、advanced、references、citations、related smoke；
- Claude Code 能发现 MCP 并完成 C01-C10，或将确属上游限流的单项明确记录为 `PARTIAL`；
- `.env` 被忽略，日志、文档和 smoke 输出均不泄露密钥；
- 未运行的 OCR、SerpApi 或多副本环境测试被明确标记，不能用离线 mock 代替真实验收结论。
