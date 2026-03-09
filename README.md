# MCP Server - 通用功能服务器

一个基于 [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 的通用功能服务器，为 AI 提供扩展能力。

支持 **本地模式 (stdio)** 与 **远程模式 (Streamable HTTP / SSE)** 双模式运行，并可将 ChatBI -> MCP -> Langfuse 的观测链路串起来。

## ✨ 功能

### 🕐 时间工具

| 工具 | 描述 |
|------|------|
| `get_current_time` | 获取指定时区的当前时间 |
| `get_timestamp` | 获取当前 Unix 时间戳 |
| `format_timestamp` | 将时间戳转换为可读格式 |

### 📊 ChatBI 数据分析工具

| 工具 | 描述 |
|------|------|
| `schema_search` | 基于向量召回 + 关键词检索返回项目相关 Schema |
| `execute_sql` | 执行只读 PostgreSQL 查询并返回 JSON 结果 |

### 🔭 Langfuse 链路观测

- `schema_search` 会记录嵌套阶段：分词、Embedding、向量检索、关键词检索、融合裁剪。
- `execute_sql` 会记录嵌套阶段：SQL Guardrail 校验、SQL 执行。
- 当上游 ChatBI 通过 `traceparent` 和 `x-chatbi-*` 头透传上下文时，MCP 工具观测会自动挂到同一条 Langfuse Trace 下。

## 🚀 快速开始

### 安装依赖

```bash
uv sync
```

### 本地模式运行

```bash
uv run mcp-server
```

### 远程模式运行（默认推荐）

```bash
uv run mcp-server --remote --transport streamable-http --port 8000
```

启动后可访问：

- MCP: `http://localhost:8000/mcp`
- Health: `http://localhost:8000/health`
- SSE 兼容端点: `http://localhost:8000/sse`

### SSE 兼容模式

```bash
uv run mcp-server --remote --transport sse --port 8000
```

## 🔌 客户端集成

### 本地模式配置

适用于 Cursor / Claude Desktop：

```json
{
  "mcpServers": {
    "utility-server": {
      "command": "uv",
      "args": ["--directory", "/path/to/mcp-server", "run", "mcp-server"]
    }
  }
}
```

### 远程模式配置

#### ChatBI / LangChain4j

直接连接 Streamable HTTP 端点：

```text
http://your-server:8000/mcp
```

#### 兼容 `mcp-remote` 的 SSE 客户端

```json
{
  "mcpServers": {
    "utility-server": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://your-server:8000/sse"]
    }
  }
}
```

## 🐳 Docker 部署

### 使用 Docker Compose（推荐）

```bash
docker compose up -d

docker compose logs -f

docker compose down
```

### 手动 Docker 命令

```bash
docker build -t mcp-server .

docker run -d -p 8000:8000 --name mcp-server mcp-server

docker run -d -p 9000:9000 -e PORT=9000 --name mcp-server mcp-server
```

## 🔐 Langfuse 配置

在 `.env` 中配置：

```bash
LANGFUSE_ENABLED=true
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk-lf-xxxx
LANGFUSE_SECRET_KEY=sk-lf-xxxx
LANGFUSE_ENVIRONMENT=production
MCP_SERVER_RELEASE=2026.03.09
```

如果未配置 Langfuse 凭证，服务仍可正常提供 MCP 工具，只是不会上报 tracing 数据。

## 🧪 开发测试

```bash
uv run mcp dev src/mcp_server/server.py
uv run mcp-server --help
```

## 📖 架构说明

```text
ChatBI Java Service
    |
    |  traceparent + x-chatbi-* headers
    v
External MCP Server (/mcp)
    |
    |-- schema_search
    |     |-- tokenize
    |     |-- embedding
    |     |-- vector_search
    |     |-- keyword_search
    |     `-- fusion
    |
    `-- execute_sql
          |-- validate
          `-- query

All observations -> Langfuse
```

## 📄 许可证

MIT
