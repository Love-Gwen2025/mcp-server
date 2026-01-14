# -*- coding: utf-8 -*-
"""
MCP服务器主模块 - 支持本地(stdio)和远程(SSE)双模式

使用FastMCP框架实现的通用功能MCP服务器，
提供时间查询等AI扩展能力。

运行方式：
    本地模式: uv run mcp-server
    远程模式: uv run mcp-server --remote --port 8000
"""

import sys
import argparse
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mcp.server.fastmcp import FastMCP

# ============================================================
# 创建MCP服务器实例
# ============================================================
# 服务器名称用于在客户端配置中标识此服务器
# instructions 参数让AI了解这个服务器的用途
mcp = FastMCP(
    name="utility-server",
    instructions="这是一个通用工具服务器，提供时间查询、时间戳转换等基础能力。",
)


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
        # 获取指定时区对象
        tz = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        # 时区名称无效时返回错误信息
        raise ValueError(
            f"无效的时区名称: {timezone}。"
            f"请使用标准时区名称，如 'Asia/Shanghai', 'UTC', 'America/New_York' 等。"
        )

    # 获取当前时间并格式化
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
        # 从时间戳创建datetime对象
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

    # 创建 SSE 传输层，指定消息接收端点路径
    sse_transport = SseServerTransport("/messages")

    async def handle_sse(scope, receive, send):
        """
        处理 SSE 连接请求 (纯 ASGI 处理函数)
        建立连接然后返回两个流
        """
        async with sse_transport.connect_sse(scope, receive, send) as streams:
            await mcp._mcp_server.run(
                streams[0],
                streams[1],
                mcp._mcp_server.create_initialization_options(),
            )

    async def app(scope, receive, send):
        """
        ASGI 应用入口

        根据请求路径分发到不同的处理函数
        """
        if scope["type"] != "http":
            return

        path = scope["path"]
        method = scope.get("method", "GET")

        if path == "/sse" and method == "GET":
            # SSE 连接端点
            await handle_sse(scope, receive, send)
        elif path == "/messages" and method == "POST":
            # 消息处理端点
            await sse_transport.handle_post_message(scope, receive, send)
        else:
            # 404 Not Found
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

    print("🌐 启动远程MCP服务器...", file=sys.stderr)
    print(f"   地址: http://{host}:{port}", file=sys.stderr)
    print(f"   SSE端点: http://{host}:{port}/sse", file=sys.stderr)
    print(f"   消息端点: http://{host}:{port}/messages", file=sys.stderr)

    # 使用 Uvicorn 启动 ASGI 服务器
    uvicorn.run(app, host=host, port=port, log_level="info")


# ============================================================
# 服务器入口函数
# ============================================================


def main():
    """
    MCP服务器入口函数 - 支持本地和远程双模式

    本地模式 (默认):
        通过stdio传输方式运行，AI客户端通过标准输入输出通信
        适合本地安装使用

    远程模式 (--remote):
        通过SSE (Server-Sent Events) 传输方式运行
        启动HTTP服务器，允许远程客户端通过网络调用
    """
    # 解析命令行参数
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
        help="远程模式监听地址，默认 0.0.0.0 (允许所有IP访问)",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="远程模式端口号，默认 8000"
    )

    args = parser.parse_args()

    if args.remote:
        # ========== 远程模式 (SSE) ==========
        # 使用低级 API 启动 SSE 服务器
        run_sse_server(args.host, args.port)
    else:
        # ========== 本地模式 (stdio) ==========
        # 通过标准输入输出通信，这是MCP协议推荐的本地服务器通信方式
        mcp.run(transport="stdio")


# 允许直接运行此模块
if __name__ == "__main__":
    main()
