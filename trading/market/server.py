"""Streamable HTTP MCP 服务 + Bearer 鉴权中间件.

鉴权/限流在 ASGI 中间件里完成（返回真正的 HTTP 401/403/429），
工具函数本身不接触 token。
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Any, Callable, Dict, List, Literal, Optional

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import (
    SCOPE_BACKTEST,
    SCOPE_READ,
    SCOPE_REFRESH,
    Principal,
    RateLimiter,
    TokenRegistry,
)
from .config import ServerConfig
from .errors import FetchError, RequestError
from .service import MarketService, NoDataError

logger = logging.getLogger("trading.market.server")

RefreshPolicy = Literal["missing", "always", "never"]
AdjustMode = Literal["none", "qfq"]

MCP_PATH = "/mcp"
HEALTH_PATH = "/healthz"
MAX_BODY_BYTES = 1 * 1024 * 1024
HEALTH_CACHE_SECONDS = 5.0

# 固定需要更高权限的工具；refresh=always 另行判断
TOOL_SCOPES: Dict[str, str] = {
    "refresh_fund_nav_cache": SCOPE_REFRESH,
    "run_momentum_basket_backtest": SCOPE_BACKTEST,
    "run_momentum_topn_backtest": SCOPE_BACKTEST,
    "run_dual_ma_backtest": SCOPE_BACKTEST,
}
_PUBLIC_ERRORS = (RequestError, NoDataError, FetchError, ValueError)


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must use YYYY-MM-DD") from exc


async def _run(fn: Callable[..., Any], *args: Any) -> Any:
    """在线程里跑同步业务代码；非预期异常对外只暴露统一信息，细节只进服务端日志."""
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args))
    except _PUBLIC_ERRORS:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("tool failed: %s", type(exc).__name__)
        raise RuntimeError("internal error; see server logs") from None


def create_mcp(
    service: MarketService,
    config: ServerConfig,
    backtests: Optional[Any] = None,
) -> FastMCP:
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(config.allowed_hosts),
        allowed_hosts=list(config.allowed_hosts),
        allowed_origins=[],
    )
    mcp = FastMCP(
        "Trading Market",
        instructions=(
            "Cached daily stock/fund/index prices. Prefer refresh='missing'; 'latest' means the "
            "newest daily row in the cache, not a real-time quote. Every row carries as_of, "
            "fetched_at, source, is_final and price_basis: rows with is_final=false are intraday "
            "snapshots, not closing values. Off-market funds (six-digit codes) use official unit NAV."
        ),
        host=config.host,
        port=config.port,
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )

    @mcp.tool()
    async def get_price_history(
        symbols: list[str],
        start_date: str,
        end_date: str,
        refresh: RefreshPolicy = "missing",
        adjust: AdjustMode = "none",
    ) -> dict[str, Any]:
        """Return inclusive daily OHLCV history for stocks, funds, or indices.

        Rows carry as_of/fetched_at/source/is_final/price_basis. adjust='qfq' returns a computed
        dividend-adjusted view (only for raw/split-adjusted series); default is the stored basis.
        refresh='always' requires the refresh scope.
        """
        return await _run(
            service.price_history, symbols, _date(start_date), _date(end_date), refresh, adjust
        )

    @mcp.tool()
    async def get_latest_price(
        symbol: str, refresh: RefreshPolicy = "missing", adjust: AdjustMode = "none"
    ) -> dict[str, Any]:
        """Return the newest cached daily row; this is not a real-time quote."""
        return await _run(service.latest_price, symbol, refresh, adjust)

    @mcp.tool()
    async def get_fund_nav_history(
        fund_code: str, start_date: str, end_date: str, refresh: RefreshPolicy = "missing"
    ) -> dict[str, Any]:
        """Return official unit-NAV history for a six-digit off-market fund."""
        return await _run(
            service.fund_nav_history, fund_code, _date(start_date), _date(end_date), refresh
        )

    @mcp.tool()
    async def get_latest_fund_nav(
        fund_code: str, refresh: RefreshPolicy = "missing"
    ) -> dict[str, Any]:
        """Return the newest cached official unit NAV for one off-market fund."""
        return await _run(service.latest_fund_nav, fund_code, refresh)

    @mcp.tool()
    async def refresh_fund_nav_cache(
        fund_code: str, start_date: str, end_date: str
    ) -> dict[str, Any]:
        """Fetch official unit NAV rows from upstream and record them in the cache (refresh scope)."""
        return await _run(service.refresh_fund_nav, fund_code, _date(start_date), _date(end_date))

    if backtests is not None:
        backtests.register(mcp, _run)
    return mcp


# --------------------------------------------------------------------------- ASGI
def required_scope(message: Any) -> Optional[str]:
    """JSON-RPC 消息需要的 scope；None 表示任一有效 token 即可（initialize/tools/list/ping）."""
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return SCOPE_READ
    name = params.get("name")
    if isinstance(name, str) and name in TOOL_SCOPES:
        return TOOL_SCOPES[name]
    args = params.get("arguments")
    if isinstance(args, dict) and args.get("refresh") == "always":
        return SCOPE_REFRESH
    return SCOPE_READ


class AuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        tokens: TokenRegistry,
        health: Callable[[], None],
        *,
        read_rate: int = 120,
        refresh_rate: int = 10,
        backtest_rate: int = 6,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.tokens = tokens
        self.health = health
        self._limiters = {
            SCOPE_READ: RateLimiter(read_rate, clock=clock),
            SCOPE_REFRESH: RateLimiter(refresh_rate, clock=clock),
            SCOPE_BACKTEST: RateLimiter(backtest_rate, clock=clock),
        }
        self._clock = clock
        self._health_at = -1e9
        self._health_ok = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method, path = scope["method"], scope["path"]

        if path == HEALTH_PATH:
            await self._handle_health(method, scope, receive, send)
            return

        principal = self._authenticate(scope)
        if principal is None:
            response = JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"}
            )
            await response(scope, receive, send)
            return

        if method == "POST" and path.rstrip("/") == MCP_PATH:
            body = await self._read_body(receive)
            if body is None:
                await JSONResponse({"error": "payload too large"}, status_code=413)(
                    scope, receive, send
                )
                return
            denied = self._check_body(principal, body)
            if denied is not None:
                await denied(scope, receive, send)
                return
            sent = False

            async def replay() -> Message:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            await self.app(scope, replay, send)
            return

        await self.app(scope, receive, send)

    # ------------------------------------------------------------------ pieces
    def _authenticate(self, scope: Scope) -> Optional[Principal]:
        header = ""
        for key, value in scope.get("headers", []):
            if key == b"authorization":
                header = value.decode("latin-1")
                break
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            return None
        return self.tokens.authenticate(token.strip())

    async def _read_body(self, receive: Receive) -> Optional[bytes]:
        chunks: List[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return b"".join(chunks)
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                return None
            chunks.append(chunk)
            if not message.get("more_body"):
                return b"".join(chunks)

    def _check_body(self, principal: Principal, body: bytes) -> Optional[JSONResponse]:
        try:
            parsed = json.loads(body) if body else None
        except ValueError:
            parsed = None  # 交给 MCP 层返回协议错误
        messages = parsed if isinstance(parsed, list) else [parsed]
        calls: Dict[str, int] = {}
        for message in messages:
            scope_needed = required_scope(message)
            if scope_needed is not None:
                calls[scope_needed] = calls.get(scope_needed, 0) + 1
        for scope_needed in calls:
            if not principal.allows(scope_needed):
                return JSONResponse(
                    {"error": "forbidden", "required_scope": scope_needed}, status_code=403
                )
        for scope_needed, count in calls.items():
            for _ in range(count):
                wait = self._limiters[scope_needed].acquire(f"{principal.name}")
                if wait is not None:
                    return JSONResponse(
                        {"error": "rate_limited", "scope": scope_needed},
                        status_code=429,
                        headers={"Retry-After": str(int(wait) + 1)},
                    )
        return None

    async def _handle_health(self, method: str, scope: Scope, receive: Receive, send: Send) -> None:
        if method not in {"GET", "HEAD"}:
            await JSONResponse({"error": "method not allowed"}, status_code=405)(scope, receive, send)
            return
        now = self._clock()
        if now - self._health_at >= HEALTH_CACHE_SECONDS:
            try:
                await anyio.to_thread.run_sync(self.health)
                self._health_ok = True
            except Exception:  # noqa: BLE001 - 不泄露任何内部信息
                self._health_ok = False
            self._health_at = now
        body = {"status": "ok"} if self._health_ok else {"status": "unavailable"}
        await JSONResponse(body, status_code=200 if self._health_ok else 503)(scope, receive, send)


def create_app(
    config: ServerConfig,
    service: MarketService,
    health: Callable[[], None],
    backtests: Optional[Any] = None,
) -> ASGIApp:
    mcp = create_mcp(service, config, backtests)
    return AuthMiddleware(
        mcp.streamable_http_app(),
        config.tokens,
        health,
        read_rate=config.read_rate_per_min,
        refresh_rate=config.refresh_rate_per_min,
        backtest_rate=config.backtest_rate_per_min,
    )
