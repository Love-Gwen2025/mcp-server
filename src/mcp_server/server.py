# -*- coding: utf-8 -*-
"""
MCP服务器主模块 - 支持本地(stdio)和远程(SSE)双模式

使用FastMCP框架实现的通用功能MCP服务器，
提供时间查询、数据库Schema搜索、SQL执行等AI扩展能力。

运行方式：
    本地模式: uv run mcp-server
    远程模式: uv run mcp-server --remote --port 8000
"""

import os
import re
import sys
import json
import logging
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jieba
import psycopg2
import psycopg2.extras
from openai import OpenAI
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# ============================================================
# 创建MCP服务器实例
# ============================================================
mcp = FastMCP(
    name="utility-server",
    instructions="这是一个通用工具服务器，提供时间查询、数据库Schema搜索、SQL执行等能力。",
)

# ============================================================
# 数据库 & Embedding 配置（环境变量）
# ============================================================
DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", "5432")),
    "dbname": os.environ.get("DB_NAME", "chatbi"),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "postgres"),
}

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = os.environ.get(
    "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-v3")
EMBEDDING_DIMENSIONS = int(os.environ.get("EMBEDDING_DIMENSIONS", "1024"))

MAX_ROWS = 100

# ============================================================
# 基础设施：DB 连接、Embedding、SQL 校验
# ============================================================

_openai_client = None


def _get_openai_client() -> OpenAI:
    """懒加载 OpenAI 客户端"""
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(
            api_key=DASHSCOPE_API_KEY, base_url=DASHSCOPE_BASE_URL
        )
    return _openai_client


def _get_db_connection():
    """创建 PostgreSQL 连接"""
    return psycopg2.connect(**DB_CONFIG)


def _get_embedding(text: str) -> list[float]:
    """调用 DashScope API 获取文本向量"""
    client = _get_openai_client()
    response = client.embeddings.create(
        model=EMBEDDING_MODEL, input=text, dimensions=EMBEDDING_DIMENSIONS
    )
    return response.data[0].embedding


def _vector_to_string(vector: list[float]) -> str:
    """向量转 pgvector 字符串格式 [0.1,0.2,...]"""
    return "[" + ",".join(str(v) for v in vector) + "]"


# SQL 安全校验（移植自 Java SqlSanitizer）
_FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
    "CREATE", "GRANT", "REVOKE", "EXEC", "EXECUTE", "CALL",
}
_COMMENT_PATTERN = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)


def _validate_sql(sql: str) -> str | None:
    """校验 SQL 是否安全，返回错误信息，None 表示安全"""
    if not sql or not sql.strip():
        return "SQL 不能为空"

    cleaned = _COMMENT_PATTERN.sub(" ", sql).strip()
    upper = cleaned.upper()

    if not upper.startswith("SELECT") and not upper.startswith("WITH"):
        return "仅允许 SELECT 查询语句"

    for keyword in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", cleaned, re.IGNORECASE):
            return f"SQL 包含禁止操作: {keyword}"

    return None


def _ensure_limit(sql: str, max_rows: int) -> str:
    """确保 SQL 包含 LIMIT"""
    if "LIMIT" not in sql.upper():
        return sql.rstrip().rstrip(";") + f" LIMIT {max_rows}"
    return sql


# ============================================================
# 数据分析工具
# ============================================================


def _vector_search(query: str, top_k: int, project_id: int) -> list[dict]:
    """向量相似度搜索"""
    try:
        vector = _get_embedding(query)
        vector_str = _vector_to_string(vector)

        with _get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, table_name, table_comment, schema_name, project_id, schema_text
                    FROM table_meta
                    WHERE project_id = %s AND embedding IS NOT NULL
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (project_id, vector_str, top_k),
                )
                return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        logger.warning("向量搜索失败: %s", e)
        return []


def _ilike_search(keywords: list[str], top_k: int, project_id: int) -> list[dict]:
    """多关键词 ILIKE 搜索（1 次 DB 查询）"""
    if not keywords:
        return []

    conditions = []
    params: list = [project_id]
    for kw in keywords:
        pattern = f"%{kw}%"
        conditions.append(
            "(t.table_name ILIKE %s OR t.table_comment ILIKE %s"
            " OR c.column_name ILIKE %s OR c.column_comment ILIKE %s)"
        )
        params.extend([pattern, pattern, pattern, pattern])

    where_clause = " OR ".join(conditions)

    sql = f"""
        SELECT DISTINCT t.id, t.table_name, t.table_comment,
               t.schema_name, t.project_id, t.schema_text
        FROM table_meta t
        LEFT JOIN column_meta c ON c.table_id = t.id
        WHERE t.project_id = %s AND ({where_clause})
        LIMIT %s
    """
    params.append(top_k)

    try:
        with _get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(sql, params)
                return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        logger.warning("ILIKE 搜索失败: %s", e)
        return []


@mcp.tool()
def schema_search(query: str, project_id: int, top_k: int = 5) -> str:
    """
    根据用户问题搜索相关的数据库表结构，返回表名、字段名、字段类型和注释。
    在生成 SQL 之前必须先调用此工具了解可用的表和字段。
    使用向量语义搜索 + jieba 中文分词关键词搜索，混合召回。

    Args:
        query: 与用户问题相关的搜索关键词
        project_id: 项目 ID
        top_k: 返回的最大表数量，默认 5
    """
    # 1. 向量搜索
    results = _vector_search(query, top_k, project_id)
    seen = {r["id"] for r in results}

    # 2. jieba 分词 + ILIKE 搜索
    keywords = list({
        w for w in jieba.cut_for_search(query) if len(w) >= 2
    })
    if len(query) >= 2 and query not in keywords:
        keywords.append(query)

    for row in _ilike_search(keywords, top_k, project_id):
        if row["id"] not in seen:
            seen.add(row["id"])
            results.append(row)

    # 3. 截断
    results = results[:top_k]

    # 4. 拼接 schema_text
    if not results:
        return "未找到与查询相关的表。"

    parts = []
    for t in results:
        if t.get("schema_text"):
            parts.append(t["schema_text"])
        else:
            parts.append(f"表名: {t['table_name']}")
    return "\n---\n".join(parts)


@mcp.tool()
def execute_sql(sql: str) -> str:
    """
    执行 SQL SELECT 查询并返回 JSON 格式结果。
    仅支持 SELECT 语句，最多返回 100 行。
    如果执行失败会返回错误信息，请根据错误修正 SQL 后重试。

    Args:
        sql: 要执行的 PostgreSQL SELECT 语句
    """
    # 安全校验
    error = _validate_sql(sql)
    if error:
        return f"SQL 校验失败: {error}"

    try:
        safe_sql = _ensure_limit(sql, MAX_ROWS)

        with _get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(safe_sql)
                rows = [dict(row) for row in cur.fetchall()]

        result = json.dumps(rows, ensure_ascii=False, default=str)
        logger.info("[Tool] execute_sql 返回 %d 行", len(rows))
        return result

    except Exception as e:
        msg = f"SQL 执行失败: {e}"
        logger.warning("[Tool] %s", msg)
        return msg


# ============================================================
# 时间相关工具
# ============================================================


@mcp.tool()
def get_current_time(timezone: str = "Asia/Shanghai") -> str:
    """
    获取指定时区的当前时间

    当AI需要知道当前时间时可以调用此工具。
    支持世界各地的时区，默认使用中国标准时间。

    Args:
        timezone: 时区名称，常用时区包括:
            - Asia/Shanghai (中国标准时间)
            - UTC (协调世界时)
            - America/New_York (美国东部时间)
            - Europe/London (英国时间)
            - Asia/Tokyo (日本时间)

    Returns:
        格式化的当前时间字符串，格式为: YYYY-MM-DD HH:MM:SS 时区

    Raises:
        ValueError: 当提供的时区名称无效时
    """
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise ValueError(
            f"无效的时区名称: {timezone}。"
            f"请使用标准时区名称，如 'Asia/Shanghai', 'UTC', 'America/New_York' 等。"
        )

    now = datetime.now(tz)
    return now.strftime("%Y-%m-%d %H:%M:%S %Z")


@mcp.tool()
def get_timestamp() -> int:
    """
    获取当前Unix时间戳（秒）

    返回从1970年1月1日00:00:00 UTC至今的秒数。
    这个值在全球任何时区都是相同的。

    Returns:
        当前的Unix时间戳（整数，单位为秒）
    """
    return int(datetime.now().timestamp())


@mcp.tool()
def format_timestamp(timestamp: int, timezone: str = "Asia/Shanghai") -> str:
    """
    将Unix时间戳转换为指定时区的可读时间格式

    Args:
        timestamp: Unix时间戳（秒）
        timezone: 目标时区名称，默认为中国标准时间

    Returns:
        格式化的时间字符串，格式为: YYYY-MM-DD HH:MM:SS 时区

    Raises:
        ValueError: 当时区名称无效或时间戳不合法时
    """
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise ValueError(f"无效的时区名称: {timezone}")

    try:
        dt = datetime.fromtimestamp(timestamp, tz=tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (OSError, OverflowError) as e:
        raise ValueError(f"无效的时间戳: {timestamp}。错误: {e}")


# ============================================================
# 远程模式 SSE 服务器实现
# ============================================================


def run_sse_server(host: str, port: int):
    """
    使用 Starlette + Uvicorn 启动 SSE 远程服务器

    FastMCP.run() 不直接支持 host/port 配置，
    所以需要手动使用低级 API 来实现远程模式。
    """
    import uvicorn
    from mcp.server.sse import SseServerTransport

    sse_transport = SseServerTransport("/messages")

    async def handle_sse(scope, receive, send):
        async with sse_transport.connect_sse(scope, receive, send) as streams:
            await mcp._mcp_server.run(
                streams[0],
                streams[1],
                mcp._mcp_server.create_initialization_options(),
            )

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return

        path = scope["path"]
        method = scope.get("method", "GET")

        if path == "/sse" and method == "GET":
            await handle_sse(scope, receive, send)
        elif path == "/messages" and method == "POST":
            await sse_transport.handle_post_message(scope, receive, send)
        elif path == "/" or path == "/health":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [[b"content-type", b"application/json"]],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"status":"ok","service":"mcp-server"}',
                }
            )
        elif path == "/favicon.ico":
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [],
                }
            )
            await send({"type": "http.response.body", "body": b""})
        else:
            await send(
                {
                    "type": "http.response.start",
                    "status": 404,
                    "headers": [[b"content-type", b"text/plain"]],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"Not Found",
                }
            )

    print("启动远程MCP服务器...", file=sys.stderr)
    print(f"   地址: http://{host}:{port}", file=sys.stderr)
    print(f"   SSE端点: http://{host}:{port}/sse", file=sys.stderr)
    print(f"   消息端点: http://{host}:{port}/messages", file=sys.stderr)

    uvicorn.run(app, host=host, port=port, log_level="warning")


# ============================================================
# 服务器入口函数
# ============================================================


def main():
    """
    MCP服务器入口函数 - 支持本地和远程双模式
    """
    parser = argparse.ArgumentParser(
        description="MCP通用工具服务器 - 支持本地和远程模式"
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="启用远程模式 (SSE)，默认为本地模式 (stdio)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="远程模式监听地址，默认 0.0.0.0",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="远程模式端口号，默认 8000"
    )

    args = parser.parse_args()

    if args.remote:
        run_sse_server(args.host, args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
