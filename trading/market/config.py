"""服务配置：全部来自环境变量；缺失即启动失败，消息里只写变量名不回显值."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Mapping, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import URL

from .auth import DEFAULT_CONFIG_DIR, TokenRegistry, TokenStore
from .errors import ConfigError

REQUIRED_DB_VARS = ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME")


def _int(env: Mapping[str, str], key: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(key)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} 必须是整数") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{key} 必须在 {lo}..{hi} 之间")
    return value


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    name: str

    def create_engine(self) -> Engine:
        url = URL.create(
            drivername="mysql+pymysql",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.name,
            query={"charset": "utf8mb4"},
        )
        return create_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            pool_recycle=1800,
            connect_args={"connect_timeout": 10, "read_timeout": 60, "write_timeout": 60},
        )


@dataclass(frozen=True)
class ServerConfig:
    database: DatabaseConfig
    tokens: TokenRegistry = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8765
    allowed_hosts: tuple[str, ...] = ()
    config_dir: str = DEFAULT_CONFIG_DIR
    read_only: bool = False
    read_rate_per_min: int = 120
    refresh_rate_per_min: int = 10
    backtest_rate_per_min: int = 6
    backtest_concurrency: int = 2
    backtest_max_span_days: int = 3660
    backtest_max_symbols: int = 20


def env_token_entries(env: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """收集环境变量/文件里显式配置的 token 文档；返回 (JSON 文档, 问题)。"""
    documents: List[str] = []
    problems: List[str] = []
    single = env.get("MCP_TOKEN")
    if single:
        scopes = [s.strip() for s in (env.get("MCP_TOKEN_SCOPES") or "read").split(",") if s.strip()]
        documents.append(json.dumps([{"name": "env", "scopes": scopes, "token": single}]))
    if env.get("MCP_TOKENS"):
        documents.append(env["MCP_TOKENS"])
    token_file = env.get("MCP_TOKENS_FILE")
    if token_file:
        try:
            documents.append(Path(token_file).read_text(encoding="utf-8"))
        except OSError as exc:
            problems.append(f"无法读取 MCP_TOKENS_FILE（{type(exc).__name__}）")
    return documents, problems


def load_tokens(env: Mapping[str, str]) -> TokenRegistry:
    """环境变量里的 token + 配置目录里的 token 文件。可能为空——服务启动时会自动引导生成。"""
    documents, problems = env_token_entries(env)
    if problems:
        raise ConfigError("; ".join(problems))
    store = TokenStore(env.get("MCP_CONFIG_DIR") or DEFAULT_CONFIG_DIR)
    return TokenRegistry.from_json(*documents, store=store)


def load_config(env: Optional[Mapping[str, str]] = None) -> ServerConfig:
    env = os.environ if env is None else env
    missing = [key for key in REQUIRED_DB_VARS if not env.get(key)]
    if missing:
        raise ConfigError("缺少 MySQL 配置: " + ", ".join(missing))

    tokens = load_tokens(env)
    port = _int(env, "MCP_PORT", 8765, 1, 65535)
    db_port = _int(env, "DB_PORT", 3306, 1, 65535)
    extra = dict(
        read_rate_per_min=_int(env, "MCP_RATE_LIMIT_READ_PER_MIN", 120, 1, 100000),
        refresh_rate_per_min=_int(env, "MCP_RATE_LIMIT_REFRESH_PER_MIN", 10, 1, 100000),
        backtest_rate_per_min=_int(env, "MCP_RATE_LIMIT_BACKTEST_PER_MIN", 6, 1, 100000),
        backtest_concurrency=_int(env, "MCP_BACKTEST_CONCURRENCY", 2, 1, 32),
        backtest_max_span_days=_int(env, "MCP_BACKTEST_MAX_SPAN_DAYS", 3660, 30, 36600),
        backtest_max_symbols=_int(env, "MCP_BACKTEST_MAX_SYMBOLS", 20, 1, 200),
    )
    hosts = tuple(h.strip() for h in env.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip())
    return ServerConfig(
        database=DatabaseConfig(
            host=env["DB_HOST"],
            port=db_port,
            user=env["DB_USER"],
            password=env["DB_PASSWORD"],
            name=env["DB_NAME"],
        ),
        tokens=tokens,
        host=env.get("MCP_HOST") or "127.0.0.1",
        port=port,
        allowed_hosts=hosts,
        config_dir=env.get("MCP_CONFIG_DIR") or DEFAULT_CONFIG_DIR,
        read_only=(env.get("MCP_READ_ONLY") or "").strip().lower() in {"1", "true", "yes", "on"},
        **extra,
    )
