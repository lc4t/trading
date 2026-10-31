"""tt-serve：行情缓存 + 鉴权 MCP（Streamable HTTP）服务入口.

    tt-serve                 启动服务（默认）
    tt-serve gen-token       生成 token 及其 sha256 配置条目
    tt-serve healthcheck     容器健康检查（非服务容器直接返回 0）
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
from typing import Optional, Sequence

from ..market.auth import ALL_SCOPES, generate_token, hash_token
from ..market.errors import ConfigError

DB_WAIT_SECONDS = 30.0


def _gen_token(args: argparse.Namespace) -> int:
    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()]
    bad = [s for s in scopes if s not in ALL_SCOPES]
    if not scopes or bad:
        print(f"scopes 必须是 {', '.join(ALL_SCOPES)} 中的一个或多个（逗号分隔）", file=sys.stderr)
        return 2
    token = generate_token()
    entry = {"name": args.name, "scopes": scopes, "sha256": hash_token(token).hex()}
    print("token（交给客户端保管，只显示这一次）:")
    print(f"  {token}")
    print("MCP_TOKENS 配置条目（服务端只存哈希；多个 token 放进同一个 JSON 数组）:")
    print(f"  {json.dumps(entry, ensure_ascii=False)}")
    return 0


def _healthcheck(_: argparse.Namespace) -> int:
    # 同一个镜像也跑回测/同步命令；只有 PID 1 是 tt-serve 的容器才需要探活
    try:
        with open("/proc/1/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", "replace")
    except OSError:
        cmdline = ""
    if "tt-serve" not in cmdline and "trading.cli.serve" not in cmdline:
        return 0
    port = os.environ.get("MCP_PORT") or "8765"
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=4) as resp:
            return 0 if resp.status == 200 else 1
    except Exception:  # noqa: BLE001
        return 1


def _serve(_: argparse.Namespace) -> int:
    from ..market.backtest_tools import BacktestTools
    from ..market.cache import MarketCache
    from ..market.config import load_config
    from ..market.fetchers import RangePriceFetcher
    from ..market.server import create_app
    from ..market.service import MarketService
    from ..market.store import MarketStore

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("trading.market")
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    engine = config.database.create_engine()
    store = MarketStore(engine)
    deadline = time.monotonic() + DB_WAIT_SECONDS
    while True:
        try:
            store.ping()
            break
        except Exception as exc:  # noqa: BLE001
            if time.monotonic() >= deadline:
                # 只报类型：驱动的错误消息里可能带连接细节
                print(f"启动失败: 无法连接 MySQL（{type(exc).__name__}），请检查 DB_* 配置", file=sys.stderr)
                return 2
            time.sleep(2)
    try:
        store.init_schema()
    except Exception as exc:  # noqa: BLE001
        print(f"启动失败: 建表失败（{type(exc).__name__}），DB 账号需要 CREATE 权限", file=sys.stderr)
        return 2

    cache = MarketCache(store, RangePriceFetcher())
    backtests = BacktestTools(store, cache, config)
    app = create_app(config, MarketService(cache), store.ping, backtests)

    if hasattr(os, "getuid") and os.getuid() == 0:
        log.warning("正以 root 运行；建议用非 root 用户启动容器（docker run --user 10001:10001）")
    log.info(
        "tt-serve 启动: %s:%s，已配置 %d 个 token",
        config.host,
        config.port,
        len(config.tokens),
    )
    import uvicorn

    uvicorn.run(app, host=config.host, port=config.port, log_level="info", access_log=False)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="tt-serve", description="行情缓存 + 鉴权 MCP 服务")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="启动服务（默认）").set_defaults(func=_serve)
    gen = sub.add_parser("gen-token", help="生成 token 和 sha256 配置条目")
    gen.add_argument("--name", required=True, help="token 名字，如 agent-laptop")
    gen.add_argument("--scopes", default="read", help="逗号分隔: read,refresh,backtest")
    gen.set_defaults(func=_gen_token)
    sub.add_parser("healthcheck", help="容器健康检查").set_defaults(func=_healthcheck)
    args = parser.parse_args(argv)
    sys.exit((getattr(args, "func", _serve))(args))


if __name__ == "__main__":
    main()
