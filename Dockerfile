# MCP 远程服务器 Docker 镜像
# 使用 Python 3.12 slim 基础镜像
FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 安装 uv 包管理器（比 pip 更快）
RUN pip install uv

# 复制依赖配置文件和 README（hatchling 构建需要 README）
COPY pyproject.toml uv.lock README.md ./

# 复制源代码（uv sync 需要包代码）
COPY src/ ./src/

# 安装依赖
RUN uv sync --frozen --no-dev

# 暴露端口（默认 8000）
EXPOSE 8000

# 设置环境变量
ENV PYTHONUNBUFFERED=1

# 可以通过 docker run -e PORT=8001 来覆盖端口
ENV PORT=8000
ENV HOST=0.0.0.0

CMD ["sh", "-c", "uv run python src/mcp_server/server.py --remote --host $HOST --port $PORT"]
