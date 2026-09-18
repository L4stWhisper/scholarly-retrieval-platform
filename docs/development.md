# 开发指南

## 环境

```bash
uv sync --extra api --extra mcp --extra distributed
uv run ruff check src tests tools
uv run python -m compileall -q src tests
uv run pytest -q
```

`uv.lock` 锁定了所有依赖版本，CI 与本地使用同一份锁文件；修改 `pyproject.toml` 后运行 `uv lock`
并一起提交。不使用 uv 时 `pip install -e ".[dev,api,mcp,distributed]"` 等价。

真实公网测试默认跳过，需要显式设置 `SCHOLAR_RUN_LIVE_TESTS=1`；OCR 测试需要
`SCHOLAR_RUN_OCR_TESTS=1` 与本机运行时。测试矩阵、真实案例与验收记录见[测试验收](test-acceptance.md)。

## 提交规范

提交信息遵循 [Conventional Commits](https://www.conventionalcommits.org/)：

```
<type>(<scope>): <一句话说明做了什么>

<正文：为什么改、怎么验证；可选>
```

| type | 用途 |
|---|---|
| `feat` | 新功能或新来源 |
| `fix` | 修复缺陷、错误行为或数据质量问题 |
| `docs` | 只改文档 |
| `refactor` | 不改变行为的代码重构 |
| `test` | 只改测试或夹具 |
| `chore` | 构建、依赖、配置等杂项 |
| `ci` | CI 流水线 |
| `style` | 格式化，不改语义 |

scope 用模块或来源名，例如 `google-scholar`、`identity`、`search`、`config`、`readme`。
一次提交只做一件事；行为变化需要附带测试和文档更新。PR 模板见 `.github/pull_request_template.md`。

## 文档结构

- `README.md`：简介、功能、快速开始与导航，不放详细说明；
- `docs/*.md` 主题指南：安装、配置、CLI、Library、API、MCP、来源、Reference、存储与部署、FAQ、本指南；
- `docs/technical-implementation.md`：架构、模块、算法、存储与设计决策账本（ADR）；
- `docs/test-acceptance.md`：自动化、真实接口与 Agent 验收记录；
- `docs/scholarly-retrieval-platform-design.md`：早期设计参考。

`skills/scholarly-research/SKILL.md`、`.mcp.json` 和 `.github` 模板是运行/自动化配置，不作为重复的
项目说明维护。`tests/test_repository_docs.py` 会检查本地链接与关键内容，新增指南后请在 README
的文档表中登记。

## 测试资料目录

- `benchmarks/`：机器可读、版本化的稳定验收案例，自动化测试会读取；当前内容是公开真实数据的
  Reference pipeline smoke 快照与关键词排序诊断，不是性能跑分，也不是人工标注 gold；
- `manual-tests/`：人工选择论文后得到的检查结果，供人复核真实检索效果；第三方论文 PDF 只在本地
  使用并由 `.gitignore` 排除，仓库保存论文链接、许可信息和可发布的结果文件。

## 新增来源

实现 `ScholarlyProvider`，声明 `ProviderCapabilities`（search/resolve/references/citations 能力
与过滤执行位置），在 `providers/registry.py` 注册，并补充 `tests/test_<name>_provider.py` 与
[数据来源](providers.md)的能力表。扩展规则与能力语义见[技术实现](technical-implementation.md)。
