# MCP 与 Agent

MCP 让 Claude Code、Codex 等 Agent 使用结构化工具。服务器暴露：

`providers`、`search`、`resolve`、`related`、`references`、`citations`、`expand`、
`extract_references`、`link_references`。

安装：`uv sync --extra mcp` 或 `pip install -e ".[mcp]"`。

## 本地 stdio（推荐）

Agent 客户端负责启动 `scholar-mcp`，不需要手动常驻服务器：

```json
{
  "mcpServers": {
    "scholarly-retrieval": {
      "command": "scholar-mcp"
    }
  }
}
```

仓库根目录已提供 `.mcp.json`。直接在终端运行 `scholar-mcp` 会等待 MCP 握手，看起来没有输出是
正常现象。

密钥通过环境变量或 `~/.config/scholarly-retrieval/.env` 提供，见[配置](configuration.md)；
不要把密钥写进 `.mcp.json`。

## Claude Code

从项目根目录注册并检查：

```bash
claude mcp add --scope project scholarly-retrieval -- scholar-mcp
claude mcp get scholarly-retrieval
```

预期为 `Connected`。非交互最小测试：

```bash
claude -p "必须使用 scholarly-retrieval MCP：先调用 providers，再只用 openalex 搜索 AlphaFold protein structure prediction，limit=3；报告 status、provider_reports 和标题。不要使用 Bash 或网页搜索。" \
  --allowedTools "mcp__scholarly-retrieval__providers,mcp__scholarly-retrieval__search" \
  --permission-mode dontAsk --max-budget-usd 1 --output-format json \
  --no-session-persistence
```

`--allowedTools` 中的 server 名必须与 `.mcp.json` 完全一致；本项目名称含连字符，不能写成
`mcp__scholarly_retrieval__...`。交互模式也可以在 Claude Code 首次询问时逐项批准工具。

安装 Agent Skill 到 Claude Code 项目：

```powershell
New-Item -ItemType Directory -Force .claude\skills\scholarly-research | Out-Null
Copy-Item -Recurse -Force skills\scholarly-research\* .claude\skills\scholarly-research
```

```bash
mkdir -p .claude/skills/scholarly-research
cp -r skills/scholarly-research/* .claude/skills/scholarly-research/
```

重新启动 Claude Code 后可使用 `/scholarly-research`。Skill 是调用策略，不包含另一套检索逻辑，也
不保存密钥。

## Codex

```bash
codex mcp add scholarly-retrieval -- scholar-mcp
codex mcp get scholarly-retrieval
```

项目级配置也可以写入 `.codex/config.toml`：

```toml
[mcp_servers.scholarly-retrieval]
command = "scholar-mcp"
```

Codex 项目 Skill 安装路径是 `.agents/skills`：

```powershell
New-Item -ItemType Directory -Force .agents\skills\scholarly-research | Out-Null
Copy-Item -Recurse -Force skills\scholarly-research\* .agents\skills\scholarly-research
```

## Streamable HTTP（可选）

```bash
export SCHOLAR_MCP_TRANSPORT=streamable-http
export SCHOLAR_MCP_HOST=127.0.0.1
export SCHOLAR_MCP_PORT=8001
scholar-mcp
```

端点为 `http://127.0.0.1:8001/mcp`。远程使用时配置 `SCHOLAR_MCP_API_KEYS`、TLS、issuer/resource
URL 和调用配额。

## 上下文预算

MCP 工具调用的有界结果会进入 Agent 当前上下文；SQLite 中的历史结果和来源原始响应不会自动进入。
推荐先返回 5～10 篇基本信息，选定少数论文后再 `resolve`、查引用或处理全文，不要一次请求几百篇
摘要或大规模多跳图。
