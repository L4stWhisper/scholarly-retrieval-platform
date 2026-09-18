# 配置

所有配置都通过环境变量完成，`.env` 文件只是本地便利手段。

## 优先级与查找顺序

1. **进程环境变量永远优先。** 在 shell、CI、容器或 Agent 客户端里已经设置的变量不会被任何文件覆盖。
2. 在此基础上，程序最多加载一个 `.env` 文件，按以下顺序取第一个存在的：
   1. `SCHOLAR_ENV_FILE` 指定的路径；
   2. 当前工作目录下的 `.env`（项目检出目录）；
   3. `~/.config/scholarly-retrieval/.env`（用户级文件，对所有检出目录以及从其他目录启动
      `scholar-mcp` 的 Agent 客户端都生效）。

从模板开始：

```bash
cp .env.example .env                                  # 项目级
# 或
mkdir -p ~/.config/scholarly-retrieval && cp .env.example ~/.config/scholarly-retrieval/.env
```

真实密钥不要提交到 Git；`.env` 已在 `.gitignore` 与 `.dockerignore` 中。

## 检查配置

```bash
scholar doctor
```

`doctor` 报告实际加载的环境文件路径、每个来源的密钥是否存在、存储是否启用，但从不输出密钥值。

## 来源密钥

| 变量 | 用途 | 是否必需 |
|---|---|---|
| `SEMANTIC_SCHOLAR_API_KEY` | Semantic Scholar 独立配额与相关性搜索端点。未配置时自动改走匿名可用的 bulk/batch 端点 | 建议，[免费申请](https://www.semanticscholar.org/product/api) |
| `OPENALEX_API_KEY` / `OPENALEX_MAILTO` | OpenAlex 正式配额与联系邮箱 | 建议 |
| `SERPAPI_API_KEY` | 启用 Google Scholar（SerpApi）来源 | 可选，付费服务 |
| `SCHOLAR_GOOGLE_CITATION_ROUNDS` | Google 被引多轮一致性召回的轮数，默认 4，至少 2 | 可选 |
| `SCHOLAR_GOOGLE_PAGE_RETRIES` | 每轮重拉陈旧快照页的预算，默认 3 | 可选 |
| `ADS_API_TOKEN` | 启用 NASA ADS 来源，[生成 token](https://ui.adsabs.harvard.edu/user/settings/token) | 可选 |
| `OPENCITATIONS_ACCESS_TOKEN` | OpenCitations 推荐 token | 可选 |
| `OPENCITATIONS_MAX_RELATIONS` | OpenCitations 单次关系体上限，默认 5000 | 可选 |
| `CROSSREF_MAILTO` / `ARXIV_MAILTO` / `EUROPE_PMC_EMAIL` | 礼貌池联系邮箱 | 建议 |

未配置 `SERPAPI_API_KEY` 或 `ADS_API_TOKEN` 时对应来源不会注册，`scholar providers` 不会列出它们。

## 存储与匹配

| 变量 | 用途 |
|---|---|
| `SCHOLAR_DB_PATH` | 启用 SQLite 审计、HTTP 缓存与结果持久化，例如 `.scholarly-retrieval/state.sqlite3` |
| `SCHOLAR_REFERENCE_AUTO_MATCH_THRESHOLD` | Reference 实体链接自动匹配阈值，默认 0.92 |
| `SCHOLAR_REFERENCE_MINIMUM_MARGIN` | 自动匹配所需的 top-2 分差，默认 0.08 |

## HTTP API 与 MCP

| 变量 | 用途 |
|---|---|
| `SCHOLAR_API_HOST` / `SCHOLAR_API_PORT` | HTTP API 监听地址，默认 `127.0.0.1:8000` |
| `SCHOLAR_API_KEYS` | 逗号分隔的 API key；设置后请求必须带 `X-API-Key` 或 Bearer token |
| `SCHOLAR_RATE_LIMIT_PER_MINUTE` / `SCHOLAR_API_MAX_REQUEST_BODY_BYTES` | 限流与请求体上限 |
| `SCHOLAR_MCP_TRANSPORT` | `stdio`（默认）或 `streamable-http` |
| `SCHOLAR_MCP_HOST` / `SCHOLAR_MCP_PORT` | Streamable HTTP 监听地址，默认 `127.0.0.1:8001` |
| `SCHOLAR_MCP_API_KEYS` / `SCHOLAR_MCP_RATE_LIMIT_PER_MINUTE` / `SCHOLAR_MCP_MAX_REQUEST_BODY_BYTES` | 远程 MCP 的鉴权与配额 |
| `SCHOLAR_MCP_ISSUER_URL` / `SCHOLAR_MCP_RESOURCE_URL` | 远程 MCP 的 OAuth issuer 与 resource |
| `SCHOLAR_REDIS_URL` / `SCHOLAR_REDIS_PREFIX` / `SCHOLAR_JOB_TTL_SECONDS` | 多副本共享配额与图任务队列 |

## 测试开关

| 变量 | 用途 |
|---|---|
| `SCHOLAR_RUN_LIVE_TESTS=1` | 显式启用访问真实公网 API 的测试 |
| `SCHOLAR_RUN_OCR_TESTS=1` | 显式启用需要 OCR/GROBID 运行时的测试 |

完整默认值以 `.env.example` 与代码中的 Pydantic/OpenAPI schema 为准。
