# Python Library

CLI、HTTP API 和 MCP 都是对同一个 `ScholarService` 的薄封装，直接在代码中调用不会产生另一套
业务行为。

```python
import asyncio

from scholarly_retrieval import ScholarService, SearchQuery


async def main() -> None:
    service = ScholarService()
    try:
        result = await service.search(
            SearchQuery(text="citation graph", year_from=2020, limit=5),
            sources=["openalex", "crossref"],
        )
        print(result.model_dump_json(indent=2))
    finally:
        await service.close()


asyncio.run(main())
```

## 主要入口

| 方法 | 作用 |
|---|---|
| `search(SearchQuery, sources=...)` | 关键词与高级检索 |
| `resolve(identifier, sources=...)` | 解析 DOI、arXiv、PMID、OpenAlex、Semantic Scholar、OpenReview、ACL ID |
| `references(identifier, limit=..., sources=...)` | seed 引用的论文 |
| `citations(identifier, limit=..., sources=...)` | 引用 seed 的论文 |
| `related(RelatedQuery, sources=...)` | 相关论文 |
| `expand(GraphExpansionQuery, sources=...)` | 有预算的局部引文图 |
| `extract_references(...)` / `link_references(...)` | 本地书目抽取与实体链接 |
| `configuration_status()` / `provider_capabilities()` | 配置与来源能力诊断 |

所有结果都是 Pydantic 模型，字段含义见 [CLI 指南](cli.md#如何理解返回结果)，数据模型与算法见
[技术实现](technical-implementation.md)。

## 环境与存储

`ScholarService()` 在构造时读取环境变量（见[配置](configuration.md)）。设置 `SCHOLAR_DB_PATH`
后会自动启用 SQLite 缓存与审计；也可以显式传入 `store=SQLiteStore(path)`。

调用结束务必 `await service.close()` 释放 HTTP 连接与数据库句柄。

## 扩展来源

实现 `ScholarlyProvider` 并声明 `ProviderCapabilities`，然后注册到 `ProviderRegistry`。扩展规则与
能力声明约定见[技术实现](technical-implementation.md)第 7、17 节。
