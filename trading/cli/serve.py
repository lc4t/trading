"""trading-mcp：行情缓存 + 鉴权 MCP（Streamable HTTP）服务入口.

    trading-mcp                 启动服务（默认）
    trading-mcp token new|rotate|list   管理配置目录里的 token（只存哈希）
    trading-mcp gen-token       生成 token 及其 sha256 配置条目（不落盘，用于 MCP_TOKENS）
    trading-mcp healthcheck     容器健康检查（非服务容器直接返回 0）
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

from ..market.auth import (
    ALL_SCOPES,
    DEFAULT_CONFIG_DIR,
    TokenStore,
    bootstrap_token,
    generate_token,
    hash_token,
)
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


def _parse_scopes(raw: str) -> list[str] | None:
    scopes = [x.strip() for x in raw.split(",") if x.strip()]
    if not scopes or any(x not in ALL_SCOPES for x in scopes):
        print(f"scopes 必须是 {', '.join(ALL_SCOPES)} 中的一个或多个（逗号分隔）", file=sys.stderr)
        return None
    return scopes


def _print_token(token: str, name: str, store: TokenStore) -> None:
    print(f"token {name}（只显示这一次，请立即保存）:")
    print(f"  {token}")
    print(f"服务端只在 {store.path} 保存它的 SHA-256；运行中的服务会自动加载变更。")


def _token_cmd(args: argparse.Namespace) -> int:
    from ..market.config import load_tokens
    from ..market.errors import ConfigError as _ConfigError

    store = TokenStore(os.environ.get("MCP_CONFIG_DIR") or DEFAULT_CONFIG_DIR)
    try:
        if args.token_command == "list":
            rows = load_tokens(os.environ).describe()
            if not rows:
                print("没有 token（服务首次启动会自动生成一个 read token）")
            for name, scopes, source in rows:
                print(f"{name}\t{','.join(scopes)}\t{source}")
            return 0
        if not store.writable():
            print(f"配置目录不可写: {store.directory}（请挂载卷，或设置 MCP_CONFIG_DIR）", file=sys.stderr)
            return 2
        if args.token_command == "new":
            scopes = _parse_scopes(args.scopes)
            if scopes is None:
                return 2
            _print_token(store.add(args.name, scopes), args.name, store)
        else:
            _print_token(store.rotate(args.name), args.name, store)
        return 0
    except _ConfigError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


def _announce_bootstrap(boot) -> None:
    bar = "=" * 72
    lines = [
        "",
        bar,
        "  未配置任何 token，已自动生成一个只读（read）token。下面的明文只显示这一次：",
        "",
        f"      {boot.token}",
        "",
    ]
    if boot.persisted:
        lines += [
            f"  已把它的 SHA-256 保存到 {boot.path}（权限 0600）；重启不会再显示。",
            "  请现在保存到客户端的密钥管理里；忘了就用  trading-mcp token rotate --name default  轮换。",
            "  需要更多权限：trading-mcp token new --name ops --scopes read,refresh,backtest",
        ]
    else:
        lines += [
            f"  !! 配置目录 {boot.path.parent} 不可写，这个 token 只在本次进程内有效：重启后会变 !!",
            "  要持久化，请挂载卷到该目录（并让容器用户可写），或改用 MCP_TOKEN / MCP_TOKENS 环境变量。",
        ]
    lines += [bar, ""]
    print("\n".join(lines), file=sys.stderr, flush=True)


def _healthcheck(_: argparse.Namespace) -> int:
    # 同一个镜像也跑回测/同步命令；只有 PID 1 是 trading-mcp 的容器才需要探活
    try:
        with open("/proc/1/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", "replace")
    except OSError:
        cmdline = ""
    if "trading-mcp" not in cmdline and "trading.cli.serve" not in cmdline:
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
    from ..market.store import MarketStore, is_privilege_error

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
    if config.read_only:
        store.configure_read_only()
        log.warning("只读模式（MCP_READ_ONLY）：不建表、不写库、不回源，只读已缓存的行")
    else:
        try:
            store.init_schema()
        except Exception as exc:  # noqa: BLE001
            if not is_privilege_error(exc):
                print(f"启动失败: 建表失败（{type(exc).__name__}），DB 账号需要 CREATE 权限", file=sys.stderr)
                return 2
            # 账号没有 DDL 权限（例如只读账号）：只要 trading_data 能读，就降级为只读模式
            try:
                store.configure_read_only()
                store.query_latest("__probe__")
            except Exception:  # noqa: BLE001
                print("启动失败: 账号没有建表权限，且读不了 trading_data", file=sys.stderr)
                return 2
            log.warning("DB 账号没有建表/写权限，已自动切换为只读模式：不写库、不回源，只读已缓存的行")

    boot = bootstrap_token(config.tokens, TokenStore(config.config_dir))
    if boot is not None:
        _announce_bootstrap(boot)

    cache = MarketCache(store, RangePriceFetcher())
    backtests = BacktestTools(store, cache, config)
    app = create_app(config, MarketService(cache), store.ping, backtests)

    if hasattr(os, "getuid") and os.getuid() == 0:
        log.warning("正以 root 运行；建议用非 root 用户启动容器（docker run --user 10001:10001）")
    log.info(
        "trading-mcp 启动: %s:%s，已配置 %d 个 token",
        config.host,
        config.port,
        len(config.tokens),
    )
    import uvicorn

    uvicorn.run(app, host=config.host, port=config.port, log_level="info", access_log=False)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="trading-mcp", description="行情缓存 + 鉴权 MCP 服务")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="启动服务（默认）").set_defaults(func=_serve)
    gen = sub.add_parser("gen-token", help="生成 token 和 sha256 配置条目")
    gen.add_argument("--name", required=True, help="token 名字，如 agent-laptop")
    gen.add_argument("--scopes", default="read", help="逗号分隔: read,refresh,backtest")
    gen.set_defaults(func=_gen_token)
    tok = sub.add_parser("token", help="管理配置目录里的 token").add_subparsers(
        dest="token_command", required=True
    )
    new = tok.add_parser("new", help="新建 token")
    new.add_argument("--name", required=True)
    new.add_argument("--scopes", default="read", help="逗号分隔: read,refresh,backtest")
    rot = tok.add_parser("rotate", help="轮换同名 token（保留 scopes）")
    rot.add_argument("--name", required=True)
    tok.add_parser("list", help="列出名字和 scopes（不显示 token）")
    for p in tok.choices.values():
        p.set_defaults(func=_token_cmd)
    sub.add_parser("healthcheck", help="容器健康检查").set_defaults(func=_healthcheck)
    args = parser.parse_args(argv)
    sys.exit((getattr(args, "func", _serve))(args))


if __name__ == "__main__":
    main()
