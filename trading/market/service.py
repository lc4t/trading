"""MCP 行情工具的业务层：多标的汇总、指纹、序列化.

返回结构沿用既有的行情 MCP 契约（``cache`` / ``data_fingerprint`` / ``count`` / ``data``），
只做加法：行上多了 ``as_of / fetched_at / source / is_final / price_basis``。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from datetime import date
from typing import Any, Dict, List, Sequence

from .cache import MarketCache, normalize_off_market_fund_symbol, normalize_symbol
from .clock import to_iso
from .errors import RequestError

MAX_SYMBOLS_PER_REQUEST = 50


class NoDataError(RuntimeError):
    """缓存与上游都没有可返回的日线."""


class RateLimitedError(NoDataError):
    """完全没有缓存、而上游正在限流冷却：返回结构化错误（带 retry_after），不是让客户端立刻重试的泛化异常."""

    def __init__(self, retry_after: int, symbols: List[str]) -> None:
        self.retry_after = int(retry_after)
        self.symbols = symbols
        super().__init__(
            f"upstream is rate limited and nothing is cached for {', '.join(symbols)}; "
            f"retry after {self.retry_after}s"
        )

    def body(self) -> Dict[str, Any]:
        return {
            "error": {
                "code": "rate_limited",
                "retry_after": self.retry_after,
                "symbols": self.symbols,
                "message": str(self),
            },
            "retry_after": self.retry_after,
        }


def serialize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key, value in row.items():
        if key in {"as_of", "fetched_at"}:
            out[key] = to_iso(value)
        elif isinstance(value, (dt.datetime, dt.date)):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


_LEGACY_COLUMNS = (
    "symbol", "date", "open_price", "close_price", "high", "low",
    "volume", "amount", "change", "change_pct",
)  # fmt: skip


def fingerprint(rows: Sequence[Dict[str, Any]]) -> str:
    """``data_fingerprint``：与 quant-engine 的同名字段算法逐字一致（只覆盖行情数值列，不含元数据）。

    步骤：按 (date, symbol) 排序去重 → 取 symbol/date/开高低收/成交量/成交额/涨跌/涨跌幅 十列 →
    date 用 ``Timestamp.isoformat()`` → ``json.dumps(sort_keys, 紧凑分隔, default=str)`` → sha256。
    """
    import pandas as pd

    frame = pd.DataFrame.from_records([{c: r.get(c) for c in _LEGACY_COLUMNS} for r in rows])
    frame["symbol"] = frame["symbol"].astype(str).str.strip().str.upper()
    frame["date"] = pd.to_datetime(frame["date"])
    frame.sort_values(["date", "symbol"], inplace=True)
    frame.drop_duplicates(["date", "symbol"], keep="last", inplace=True)
    canonical = []
    for row in frame.reindex(columns=list(_LEGACY_COLUMNS)).itertuples(index=False):
        canonical.append([v.isoformat() if isinstance(v, pd.Timestamp) else v for v in row])
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def state_fingerprint(rows: Sequence[Dict[str, Any]]) -> str:
    """在行情数值之外还覆盖 price_basis / is_final：盘中行转为定稿、口径变化时它会变."""
    canonical = [
        [str(r["symbol"]), str(r["date"]), r["price_basis"], bool(r["is_final"])] for r in rows
    ]
    encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded + fingerprint(rows).encode()).hexdigest()


class MarketService:
    def __init__(self, cache: MarketCache) -> None:
        self.cache = cache

    # ----------------------------------------------------------------- history
    def price_history(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        refresh: str = "missing",
        adjust: str = "none",
    ) -> Dict[str, Any]:
        normalized = list(dict.fromkeys(normalize_symbol(s) for s in symbols))
        if not normalized:
            raise RequestError("At least one symbol is required")
        if len(normalized) > MAX_SYMBOLS_PER_REQUEST:
            raise RequestError(f"At most {MAX_SYMBOLS_PER_REQUEST} symbols per request")
        return self._history(normalized, start, end, refresh, adjust)

    def fund_nav_history(
        self, fund_code: str, start: date, end: date, refresh: str = "missing"
    ) -> Dict[str, Any]:
        code = normalize_off_market_fund_symbol(fund_code)
        return self._history([code], start, end, refresh, "none")

    def refresh_fund_nav(self, fund_code: str, start: date, end: date) -> Dict[str, Any]:
        return self.fund_nav_history(fund_code, start, end, "always")

    def _history(
        self, symbols: List[str], start: date, end: date, refresh: str, adjust: str
    ) -> Dict[str, Any]:
        payloads = [self.cache.get_range(s, start, end, refresh, adjust) for s in symbols]
        cache_info: Dict[str, Dict[str, Any]] = {}
        views: List[Dict[str, Any]] = []
        for payload in payloads:
            data = payload["data"]
            views.extend(data)
            dates = sorted(str(v["date"]) for v in data)
            cache_info[payload["symbol"]] = {
                "cache_status": payload["cache_status"],
                "series_type": payload["series_type"],
                "upstream_route": payload["upstream_route"],
                "upstream_source": payload["upstream_source"],
                "refreshed": payload["refreshed"],
                "tail_refreshed": payload["tail_refreshed"],
                "tail_refresh_error": payload["tail_refresh_error"],
                "retry_after": payload["retry_after"],
                "count": payload["count"],
                "coverage_start": dates[0] if dates else None,
                "coverage_end": dates[-1] if dates else None,
                "requested_start": start.isoformat(),
                "requested_end": end.isoformat(),
                "coverage": payload["coverage"],
                "price_view": payload["price_view"],
                "price_basis": payload["price_basis"],
                "price_bases": payload["price_bases"],
                "nonfinal_count": payload["nonfinal_count"],
                "corrected_rows": payload["corrected_rows"],
            }
        limited = [
            (sym, c["retry_after"]) for sym, c in cache_info.items() if c["tail_refresh_error"] == "rate_limited"
        ]
        if not views and limited:
            raise RateLimitedError(max(r for _, r in limited), [sym for sym, _ in limited])
        if not views:
            hint = (
                " (database is read-only: nothing cached and upstream refresh is disabled)"
                if any(c["tail_refresh_error"] == "read_only" for c in cache_info.values())
                else ""
            )
            raise NoDataError(
                "The market data service returned no rows for "
                + ", ".join(symbols)
                + f" in [{start.isoformat()}, {end.isoformat()}]"
                + hint
            )
        views.sort(key=lambda v: (v["date"], v["symbol"]))
        retry_after = max((r for _, r in limited), default=None)
        return {
            **({"retry_after": retry_after} if retry_after else {}),
            "cache": cache_info,
            "data_fingerprint": fingerprint(views),
            "state_fingerprint": state_fingerprint(views),
            "count": len(views),
            "data": [serialize_row(v) for v in views],
        }

    # ------------------------------------------------------------------ latest
    def latest_price(
        self, symbol: str, refresh: str = "missing", adjust: str = "none"
    ) -> Dict[str, Any]:
        return self._latest(normalize_symbol(symbol), refresh, adjust)

    def latest_fund_nav(self, fund_code: str, refresh: str = "missing") -> Dict[str, Any]:
        return self._latest(normalize_off_market_fund_symbol(fund_code), refresh, "none")

    def _latest(self, symbol: str, refresh: str, adjust: str) -> Dict[str, Any]:
        payload = self.cache.get_latest(symbol, refresh, adjust)
        if payload["data"] is None and payload["tail_refresh_error"] == "rate_limited":
            raise RateLimitedError(payload["retry_after"], [symbol])
        if payload["data"] is None:
            raise NoDataError(f"No daily market data for {symbol}")
        payload["data"] = serialize_row(payload["data"])
        return payload
