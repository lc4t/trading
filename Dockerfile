FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system \
    UV_NO_SYNC=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates build-essential \
 && rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install --upgrade pip && pip install uv

# Install Python deps first for better layer caching.
# .python-version is copied here so the build-time venv matches what
# `uv run` will resolve at runtime — avoiding a venv rebuild on first run.
# README.md is included because pyproject.toml's `readme = ...` requires it.
COPY pyproject.toml uv.lock .python-version README.md ./
RUN uv sync --no-dev --frozen --no-install-project

# Copy the rest of the backend
COPY . .
# Install the trading package itself now that source is in place.
RUN uv sync --no-dev --frozen

# The frontend is deployed separately; keep the image lean.
RUN rm -rf frontend tests

# ---- tt-serve（行情缓存 + 鉴权 MCP）：同一个镜像，用 `tt-serve` 命令启动 ----
# 默认仍然是 root（每日流水线挂载 data/ 目录并依赖这一点）；运行服务时请显式指定非 root：
#   docker run --user 10001:10001 ... ghcr.io/lc4t/trading-2.0 /app/.venv/bin/tt-serve
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin trading
ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=8765
EXPOSE 8765
# 只有 PID 1 是 tt-serve 的容器才会真正探活；回测/同步等一次性容器直接判健康
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["/app/.venv/bin/python", "-m", "trading.cli.serve", "healthcheck"]

CMD ["uv", "run", "python", "fetcher.py", "--help"]
