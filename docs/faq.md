# 常见问题

## Semantic Scholar 一直返回 429

未配置 `SEMANTIC_SCHOLAR_API_KEY` 时，请求共享全球匿名配额池，该池几乎总是耗尽，指数退避也无法
恢复；来源报告会以 `throttled` 状态和 `error_message` 说明这一点。
[免费申请 key](https://www.semanticscholar.org/product/api) 后写入环境变量或 `.env`，并用
`scholar doctor` 确认 `api_key_configured: true`。

## SerpApi 额度用完了

免费套餐每月 250 次搜索，一次两 ID 的 Google 被引检索约消耗 25 次。查看
`https://serpapi.com/account.json?api_key=...` 的 `total_searches_left`。缓解办法：把 Google 当作
补漏来源，先用开放来源；直接传 `google_scholar:ID` 减少发现请求；降低
`SCHOLAR_GOOGLE_CITATION_ROUNDS` 与 `SCHOLAR_GOOGLE_PAGE_RETRIES`；或升级套餐。

## Google Scholar 被引数量比网页少

Google 会用互不一致的索引快照响应分页，项目已按标称总数识别陈旧页并重拉。若结果仍为 `partial`，
查看 `provider_reports[].context.cluster_outcomes` 中每轮的 `pages`、`stale_retries` 与
`stop_reason`，并适当增加轮数预算。见[数据来源](providers.md#google-scholarserpapi)。

## 关键词检索结果里有不相关的论文

检索是有界召回后的软融合排序，不做硬过滤。缩小 `--source` 到领域来源、拆分同义词、使用高级字段
（`--year-from`、`--author`、`--venue`）通常最有效。补充材料、同行评审报告等非论文登记记录已默认
排除。

## Bash 无法进入 Windows 路径

Git Bash 中不要使用裸 `C:\Users\...`：

```bash
cd /d/github_project/scholarly_retrieval_platform
```

## `No module named scholar`

模块名不是 `scholar`。使用已安装命令或正确模块：

```bash
scholar search "query"
python -m scholarly_retrieval search "query"
```

## Agent 找不到 `scholar-mcp`

Agent 启动的进程没有继承安装环境的 PATH。可在 MCP 配置中使用该环境 Python 的绝对路径：

```json
{
  "command": "D:\\path\\to\\.venv\\Scripts\\python.exe",
  "args": ["-m", "scholarly_retrieval.mcp_server"]
}
```

密钥请放在 `~/.config/scholarly-retrieval/.env`，这样从任何目录启动都能读取。

## 某些来源失败但仍有结果

这是正常的多源 `partial`。检查 `provider_reports` 中的 `failed`、`throttled`、`skipped` 和
`truncated`，不要把部分覆盖描述成完整覆盖。
