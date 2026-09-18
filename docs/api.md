# HTTP API

安装并启动常驻服务：

```bash
uv sync --extra api        # 或 pip install -e ".[api]"
uv run scholar-api
```

默认地址为 `http://127.0.0.1:8000`：

- 健康检查：`GET /health`
- OpenAPI UI：`GET /docs`
- 机器规范：`GET /openapi.json`
- 核心路由：`/v1/providers`、`/v1/search`、`/v1/resolve`、`/v1/related`、
  `/v1/references`、`/v1/citations`、`/v1/graph/expand`
- Reference：`/v1/references/extract`、`/v1/references/link`
- 异步图任务：`POST /v1/jobs/expand`、`GET/DELETE /v1/jobs/{job_id}`

```bash
curl -X POST http://127.0.0.1:8000/v1/search \
  -H "Content-Type: application/json" \
  -d '{"text": "citation graph", "year_from": 2020, "limit": 5}'
```

PowerShell：

```powershell
$body = @{ text = "citation graph"; year_from = 2020; limit = 5 } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/search `
  -ContentType application/json -Body $body
```

## 鉴权与边界

设置 `SCHOLAR_API_KEYS` 后，请求必须带 `X-API-Key` 或 Bearer token。远程部署必须使用 TLS、反向代理、
请求体限制和配额；不要将默认无鉴权服务暴露到公网。相关变量见[配置](configuration.md)，容器与
多副本部署见[存储与部署](storage-and-deployment.md)。

响应结构与 CLI 的 `--format json` 一致，字段含义见 [CLI 指南](cli.md#如何理解返回结果)。
