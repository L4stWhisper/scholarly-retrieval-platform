# 存储与部署

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

SQLite 适合单机单库。运行中可能出现 `-wal` 和 `-shm` 文件，热备份不能只复制主文件。

## 维护

先预览，再加 `--apply` 执行：

```bash
scholar maintenance --retention-days 30 --max-raw-responses 10000
scholar maintenance --retention-days 30 --max-raw-responses 10000 --apply
```

人工身份决策：

```bash
scholar review list
scholar review decide openalex:W1 crossref:10.1000/x --action merge --reason "same manuscript"
scholar review revert <event-id> --reason "merged by mistake"
```

`decide` 的两个参数是待判断的记录 ID，`--relation` 可选 `same_manifestation`、`version_of`、
`different_work`；所有决策都会保留审计记录并可回滚。

## Docker

```bash
docker build -t scholarly-retrieval .
docker run --rm -p 8000:8000 -v scholar-data:/data \
  -e SCHOLAR_API_KEYS=replace-with-a-secret scholarly-retrieval
```

镜像默认启动 `scholar-api`，数据库位于 `/data/scholar.sqlite3`。

## 多副本

`compose.distributed.yaml` 使用 Redis 共享 API 配额、图任务队列、结果和取消状态，`scholar-worker`
处理异步图任务。它不共享 SQLite 长期证据库；多副本长期审计仍需要未来的服务型数据库。
`deploy/distributed-nginx.conf` 是反向代理示例。远程部署必须使用 TLS、鉴权、请求体限制和配额。
