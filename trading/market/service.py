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
_FINGERPRINT_FIELDS = ("symbol", "date", "open_price", "close_price", "high", "low", "volume")


class NoDataError(RuntimeError):
    """缓存与上游都没有可返回的日线."""


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


def fingerprint(rows: Sequence[Dict[str, Any]]) -> str:
    canonical = [
        [str(r["date"]) if k == "date" else r[k] for k in _FINGERPRINT_FIELDS]
        + [r["price_basis"], bool(r["is_final"])]
        for r in rows
    ]
    encoded = json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
        return {
            "cache": cache_info,
            "data_fingerprint": fingerprint(views),
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
        if payload["data"] is None:
            raise NoDataError(f"No daily market data for {symbol}")
        payload["data"] = serialize_row(payload["data"])
        return payload
