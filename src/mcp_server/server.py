# -*- coding: utf-8 -*-
"""
MCP服务器主模块 - 支持本地(stdio)和远程HTTP双模式。

远程模式默认使用 Streamable HTTP `/mcp`，也兼容 SSE 传输，
提供时间查询、数据库 Schema 搜索、SQL 执行等 AI 扩展能力。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jieba
import psycopg2
import psycopg2.extras
from mcp.server.fastmcp import Context, FastMCP
from openai import OpenAI
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp_server.tracing import start_observation

logger = logging.getLogger(__name__)

DEFAULT_HOST = os.environ.get("HOST", "0.0.0.0")
DEFAULT_PORT = int(os.environ.get("PORT", "8000"))
MAX_ROWS = int(os.environ.get("MAX_ROWS", "100"))

mcp = FastMCP(
    name="utility-server",
    instructions="这是一个通用工具服务器，提供时间查询、数据库Schema搜索、SQL执行等能力。",
    host=DEFAULT_HOST,
    port=DEFAULT_PORT,
    sse_path="/sse",
    message_path="/messages",
    streamable_http_path="/mcp",
)


@mcp.custom_route("/", methods=["GET"])
async def root(_: Request) -> Response:
    return JSONResponse(
        {
            "status": "ok",
            "service": "mcp-server",
            "transport": "streamable-http",
            "mcp_path": "/mcp",
        }
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> Response:
    return JSONResponse({"status": "ok", "service": "mcp-server"})


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

_openai_client = None

_FORBIDDEN_KEYWORDS = {
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "TRUNCATE",
    "CREATE",
    "GRANT",
    "REVOKE",
    "EXEC",
    "EXECUTE",
    "CALL",
}
_COMMENT_PATTERN = re.compile(r"/\*.*?\*/|--[^\n]*", re.DOTALL)


def _get_openai_client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(
            api_key=DASHSCOPE_API_KEY,
            base_url=DASHSCOPE_BASE_URL,
        )
    return _openai_client


def _get_db_connection():
    return psycopg2.connect(**DB_CONFIG)


def _vector_to_string(vector: list[float]) -> str:
    return "[" + ",".join(str(v) for v in vector) + "]"


def _validate_sql(sql: str) -> str | None:
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
    if "LIMIT" not in sql.upper():
        return sql.rstrip().rstrip(";") + f" LIMIT {max_rows}"
    return sql


def _get_embedding(text: str) -> list[float]:
    with start_observation(
        name="schema_search.embedding",
        as_type="span",
        input_payload={"query": text[:200]},
        metadata={"embedding_dimensions": EMBEDDING_DIMENSIONS},
        model=EMBEDDING_MODEL,
    ) as observation:
        client = _get_openai_client()
        response = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text,
            dimensions=EMBEDDING_DIMENSIONS,
        )
        embedding = response.data[0].embedding
        observation.update(output={"embedding_dimensions": len(embedding)})
        return embedding


def _vector_search(query: str, top_k: int, project_id: int) -> list[dict]:
    with start_observation(
        name="schema_search.vector_search",
        as_type="retriever",
        input_payload={"query": query[:200], "top_k": top_k, "project_id": project_id},
        metadata={"strategy": "pgvector"},
    ) as observation:
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
                    rows = [dict(row) for row in cur.fetchall()]
                    observation.update(
                        output={
                            "hit_count": len(rows),
                            "table_names": [row["table_name"] for row in rows],
                        }
                    )
                    return rows
        except Exception as exc:
            logger.warning("向量搜索失败: %s", exc)
            observation.update(level="ERROR", status_message=str(exc), output={"hit_count": 0})
            return []


def _ilike_search(keywords: list[str], top_k: int, project_id: int) -> list[dict]:
    with start_observation(
        name="schema_search.keyword_search",
        as_type="retriever",
        input_payload={"keywords": keywords, "top_k": top_k, "project_id": project_id},
        metadata={"strategy": "ilike"},
    ) as observation:
        if not keywords:
            observation.update(output={"hit_count": 0})
            return []

        conditions = []
        params: list = [project_id]
        for keyword in keywords:
            pattern = f"%{keyword}%"
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
                    rows = [dict(row) for row in cur.fetchall()]
                    observation.update(
                        output={
                            "hit_count": len(rows),
                            "table_names": [row["table_name"] for row in rows],
                        }
                    )
                    return rows
        except Exception as exc:
            logger.warning("ILIKE 搜索失败: %s", exc)
            observation.update(level="ERROR", status_message=str(exc), output={"hit_count": 0})
            return []


@mcp.tool()
def schema_search(
    query: str,
    project_id: int,
    top_k: int = 5,
    ctx: Context | None = None,
) -> str:
    """
    根据用户问题搜索相关的数据库表结构，返回表名、字段名、字段类型和注释。
    在生成 SQL 之前必须先调用此工具了解可用的表和字段。
    使用向量语义搜索 + jieba 中文分词关键词搜索，混合召回。
    """
    with start_observation(
        name="mcp.schema_search",
        as_type="tool",
        input_payload={"query": query, "project_id": project_id, "top_k": top_k},
        metadata={"tool": "schema_search"},
        ctx=ctx,
    ) as observation:
        results = _vector_search(query, top_k, project_id)
        seen = {row["id"] for row in results}

        with start_observation(
            name="schema_search.tokenize",
            as_type="span",
            input_payload={"query": query},
        ) as tokenize_observation:
            keywords = list({word for word in jieba.cut_for_search(query) if len(word) >= 2})
            if len(query) >= 2 and query not in keywords:
                keywords.append(query)
            tokenize_observation.update(output={"keywords": keywords})

        for row in _ilike_search(keywords, top_k, project_id):
            if row["id"] not in seen:
                seen.add(row["id"])
                results.append(row)

        with start_observation(
            name="schema_search.fusion",
            as_type="span",
            input_payload={"candidate_count": len(results), "top_k": top_k},
        ) as fusion_observation:
            results = results[:top_k]
            fusion_observation.update(
                output={
                    "final_hit_count": len(results),
                    "table_names": [row["table_name"] for row in results],
                }
            )

        if not results:
            observation.update(output={"hit_count": 0, "table_names": []})
            return "未找到与查询相关的表。"

        parts = []
        for table in results:
            if table.get("schema_text"):
                parts.append(table["schema_text"])
            else:
                parts.append(f"表名: {table['table_name']}")

        output = "\n---\n".join(parts)
        observation.update(
            output={
                "hit_count": len(results),
                "table_names": [row["table_name"] for row in results],
                "schema_preview": output[:500],
            }
        )
        return output


@mcp.tool()
def execute_sql(sql: str, ctx: Context | None = None) -> str:
    """
    执行 SQL SELECT 查询并返回 JSON 格式结果。
    仅支持 SELECT 语句，最多返回 100 行。
    如果执行失败会返回错误信息，请根据错误修正 SQL 后重试。
    """
    with start_observation(
        name="mcp.execute_sql",
        as_type="tool",
        input_payload={"sql": sql[:2000]},
        metadata={"tool": "execute_sql"},
        ctx=ctx,
    ) as observation:
        with start_observation(
            name="execute_sql.validate",
            as_type="guardrail",
            input_payload={"sql": sql[:2000]},
        ) as validate_observation:
            error = _validate_sql(sql)
            if error:
                message = f"SQL 校验失败: {error}"
                validate_observation.update(level="ERROR", status_message=error, output={"valid": False})
                observation.update(level="ERROR", status_message=error, output={"error": message})
                return message
            validate_observation.update(output={"valid": True})

        safe_sql = _ensure_limit(sql, MAX_ROWS)

        try:
            with start_observation(
                name="execute_sql.query",
                as_type="span",
                input_payload={"sql": safe_sql[:2000], "max_rows": MAX_ROWS},
                metadata={"database": DB_CONFIG["dbname"]},
            ) as query_observation:
                with _get_db_connection() as conn:
                    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                        cur.execute(safe_sql)
                        rows = [dict(row) for row in cur.fetchall()]

                truncated = len(rows) >= MAX_ROWS and "LIMIT" not in sql.upper()
                query_observation.update(
                    output={
                        "row_count": len(rows),
                        "truncated": truncated,
                    }
                )

            result = json.dumps(rows, ensure_ascii=False, default=str)
            logger.info("[Tool] execute_sql 返回 %d 行", len(rows))
            observation.update(
                output={
                    "row_count": len(rows),
                    "truncated": len(rows) >= MAX_ROWS,
                    "result_preview": rows[:3],
                }
            )
            return result
        except Exception as exc:
            message = f"SQL 执行失败: {exc}"
            logger.warning("[Tool] %s", message)
            observation.update(level="ERROR", status_message=str(exc), output={"error": message})
            return message


@mcp.tool()
def get_current_time(timezone: str = "Asia/Shanghai") -> str:
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise ValueError(
            f"无效的时区名称: {timezone}。请使用标准时区名称，如 'Asia/Shanghai', 'UTC', 'America/New_York' 等。"
        )

    now = datetime.now(tz)
    return now.strftime("%Y-%m-%d %H:%M:%S %Z")


@mcp.tool()
def get_timestamp() -> int:
    return int(datetime.now().timestamp())


@mcp.tool()
def format_timestamp(timestamp: int, timezone: str = "Asia/Shanghai") -> str:
    try:
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise ValueError(f"无效的时区名称: {timezone}")

    try:
        dt = datetime.fromtimestamp(timestamp, tz=tz)
        return dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except (OSError, OverflowError) as exc:
        raise ValueError(f"无效的时间戳: {timestamp}。错误: {exc}")


def run_remote_server(host: str, port: int, transport: str) -> None:
    mcp.settings.host = host
    mcp.settings.port = port

    print("启动远程MCP服务器...", file=sys.stderr)
    print(f"   地址: http://{host}:{port}", file=sys.stderr)
    print(f"   MCP 端点: http://{host}:{port}/mcp", file=sys.stderr)
    print(f"   健康检查: http://{host}:{port}/health", file=sys.stderr)
    if transport == "sse":
        print(f"   SSE 端点: http://{host}:{port}/sse", file=sys.stderr)
        print(f"   消息端点: http://{host}:{port}/messages", file=sys.stderr)

    mcp.run(transport=transport)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="MCP通用工具服务器 - 支持本地和远程模式"
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="启用远程模式，默认使用 streamable-http 传输",
    )
    parser.add_argument(
        "--transport",
        choices=["streamable-http", "sse"],
        default=os.environ.get("MCP_TRANSPORT", "streamable-http"),
        help="远程模式传输协议，默认 streamable-http",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=DEFAULT_HOST,
        help=f"远程模式监听地址，默认 {DEFAULT_HOST}",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"远程模式端口号，默认 {DEFAULT_PORT}",
    )

    args = parser.parse_args()

    if args.remote:
        run_remote_server(args.host, args.port, args.transport)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
