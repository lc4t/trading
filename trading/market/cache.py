"""缓存优先的日线读取，带「已定稿不回源 / 非终值行必修复」语义.

规则速览：
1. 已定稿（``is_final``）的历史行只读，不再回源；缺口通过 ``coverage`` 暴露，不静默补。
2. 当日（业务日）数据允许缓存，但每行带 ``as_of / fetched_at / source / is_final / price_basis``；
   只有抓取时刻晚于该交易日收盘 + 缓冲才标 ``is_final``。非终值行每次读取都按 TTL 判断是否重抓，
   且一旦过了收盘时刻就必定重抓——即使请求的 end 恰好等于缓存里最新的一天。
3. 存不复权/仅拆分调整的价格；新拆分事件会让受影响的历史行失效并重抓。
4. 上游失败时返回缓存并在 ``tail_refresh_error`` 里说明；失败后短时间内不再重试，避免打爆上游。
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from .clock import (
    as_of_for,
    business_today,
    final_ready_at,
    is_final_snapshot,
    local_naive_to_utc,
    utcnow,
)
from .errors import FetchError, ReadOnlyError, RequestError
from .fetchers import RangePriceFetcher, series_type_for_symbol, upstream_route_for_symbol
from .models import STABLE_BASES
from .store import MarketStore

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9^._=-]{1,20}$")
OFF_MARKET_FUND_PATTERN = re.compile(r"^\d{6}$")
REFRESH_POLICIES = ("missing", "always", "never")
ADJUST_MODES = ("none", "qfq")

DEFAULT_TAIL_CHECK_TTL = 600.0  # 尾部（缓存最新日之后）检查间隔
DEFAULT_INTRADAY_TTL = 300.0  # 盘中非终值行的最长缓存时间
READ_ONLY_ERROR = "read_only"  # tail_refresh_error 的固定取值：数据库只读，没有回源
DEFAULT_FAILURE_BACKOFF = 60.0  # 上游失败后的静默期
GAP_TOLERANCE_DAYS = 10  # 超过它的日期缺口才算缺口（长假 + 周末约 9 天）


def normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if not SYMBOL_PATTERN.fullmatch(normalized):
        raise RequestError("Invalid symbol. Use 1-20 letters, digits, or one of ^ . _ = -.")
    return normalized


def normalize_off_market_fund_symbol(symbol: str) -> str:
    normalized = normalize_symbol(symbol)
    if not OFF_MARKET_FUND_PATTERN.fullmatch(normalized):
        raise RequestError("Off-market fund code must contain exactly six digits, for example 005698")
    return normalized


@dataclass
class _Outcome:
    refreshed: bool = False
    error: Optional[str] = None
    source: Optional[str] = None
    corrected_rows: int = 0


class MarketCache:
    def __init__(
        self,
        store: MarketStore,
        fetcher: RangePriceFetcher,
        *,
        now: Callable[[], dt.datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        tail_check_ttl: float = DEFAULT_TAIL_CHECK_TTL,
        intraday_ttl: float = DEFAULT_INTRADAY_TTL,
        failure_backoff: float = DEFAULT_FAILURE_BACKOFF,
    ) -> None:
        self.store = store
        self.fetcher = fetcher
        self._now = now
        self._monotonic = monotonic
        self._tail_ttl = tail_check_ttl
        self._intraday_ttl = intraday_ttl
        self._failure_backoff = failure_backoff
        self._tail_checked_at: Dict[str, float] = {}
        self._backoff: Dict[str, tuple[float, str]] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ----------------------------------------------------------------- helpers
    def _lock_for(self, symbol: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(symbol, threading.Lock())

    @staticmethod
    def _validate_policy(refresh: str) -> None:
        if refresh not in REFRESH_POLICIES:
            raise RequestError("refresh must be one of: missing, always, never")

    @staticmethod
    def _validate_adjust(adjust: str) -> None:
        if adjust not in ADJUST_MODES:
            raise RequestError("adjust must be one of: none, qfq")

    def today(self) -> date:
        return business_today(self._now())

    def view(self, symbol: str, row: Dict[str, Any]) -> Dict[str, Any]:
        """把库里的一行补全成带元数据的视图；没有旁表记录的遗留行按 update_time 推断."""
        out = {k: v for k, v in row.items() if k != "meta"}
        meta = row.get("meta")
        if meta:
            out.update(meta)
            return out
        updated = row.get("update_time")
        fetched_at = local_naive_to_utc(updated) if updated else None
        final = fetched_at is not None and is_final_snapshot(symbol, row["date"], fetched_at)
        out.update(
            {
                "price_basis": "legacy_unknown",
                "source": None,
                "fetched_at": fetched_at,
                "is_final": final,
                "as_of": as_of_for(symbol, row["date"], fetched_at, True)
                if final
                else fetched_at,
            }
        )
        return out

    def _nonfinal_due(self, symbol: str, views: List[Dict[str, Any]], now: dt.datetime) -> Optional[date]:
        """最早一个需要重抓的非终值行日期；没有则 None."""
        earliest: Optional[date] = None
        for v in views:
            if v["is_final"]:
                continue
            if now >= final_ready_at(symbol, v["date"]):
                due = True  # 已过收盘，肯定能抓到最终值
            else:
                fetched = v["fetched_at"]
                due = fetched is None or (now - fetched).total_seconds() >= self._intraday_ttl
            if due and (earliest is None or v["date"] < earliest):
                earliest = v["date"]
        return earliest

    def _tail_check_due(self, symbol: str) -> bool:
        checked = self._tail_checked_at.get(symbol)
        return checked is None or self._monotonic() - checked >= self._tail_ttl

    # ------------------------------------------------------------------ fetch
    def _fetch_and_persist(self, symbol: str, start: date, end: date) -> tuple[Any, int]:
        mode = self.store.series_mode(symbol)
        result = self.fetcher.fetch(symbol, start, end, mode=mode)
        rows = [r for r in result.rows if start <= r["date"] <= end]
        fetched_at = self._now()
        finals = {r["date"]: is_final_snapshot(symbol, r["date"], fetched_at) for r in rows}
        as_of = {d: as_of_for(symbol, d, fetched_at, f) for d, f in finals.items()}
        written = self.store.upsert(
            rows,
            price_basis=result.price_basis,
            source=result.source,
            fetched_at=fetched_at,
            final_flags=finals,
            as_of=as_of,
        )
        return result, written

    def _refresh_window(self, symbol: str, start: date, end: date) -> _Outcome:
        """抓 [start, end] 并落库；检测到新的拆分事件时让更早的历史失效并补抓."""
        if self.store.read_only:
            raise ReadOnlyError("database is read-only")
        first_cached = self.store.first_date(symbol)
        result, _ = self._fetch_and_persist(symbol, start, end)
        self._tail_checked_at[symbol] = self._monotonic()  # 刚回过源，尾部检查间隔从现在算起
        outcome = _Outcome(refreshed=True, source=result.source)
        new_actions = self.store.record_actions(symbol, result.actions, self._now())
        splits = [a for a in new_actions if a["kind"] == "split"]
        if splits and first_cached is not None and first_cached < start:
            logger.warning(f"{symbol} 检测到新的拆分事件，历史行失效并重抓")
            outcome.corrected_rows = self.store.invalidate_before(symbol, start)
            try:
                self._fetch_and_persist(symbol, first_cached, start - timedelta(days=1))
            except FetchError as exc:
                outcome.error = f"split_history_refetch_failed: {exc}"
        return outcome

    def _guarded_refresh(self, symbol: str, start: date, end: date) -> _Outcome:
        """尾部/非终值修复用：失败不抛出，只报告；失败后进入静默期."""
        backoff = self._backoff.get(symbol)
        if backoff and self._monotonic() < backoff[0]:
            return _Outcome(refreshed=False, error=backoff[1])
        self._tail_checked_at[symbol] = self._monotonic()
        try:
            outcome = self._refresh_window(symbol, start, end)
        except ReadOnlyError:
            return _Outcome(refreshed=False, error=READ_ONLY_ERROR)
        except Exception as exc:  # noqa: BLE001 - 上游任何失败都降级为返回缓存
            logger.warning(f"tail refresh failed for {symbol}: {type(exc).__name__}: {exc}")
            # 对外只给自己组装的 FetchError 文案；其它异常的消息可能带路径等内部细节，只暴露类型
            message = f"{type(exc).__name__}: {exc}" if isinstance(exc, FetchError) else type(exc).__name__
            self._backoff[symbol] = (self._monotonic() + self._failure_backoff, message)
            return _Outcome(refreshed=False, error=message)
        if outcome.error:
            self._backoff[symbol] = (self._monotonic() + self._failure_backoff, outcome.error)
        else:
            self._backoff.pop(symbol, None)
        return outcome

    # ------------------------------------------------------------------ reads
    def get_range(
        self,
        symbol: str,
        start: date,
        end: date,
        refresh: str = "missing",
        adjust: str = "none",
    ) -> Dict[str, Any]:
        symbol = normalize_symbol(symbol)
        self._validate_policy(refresh)
        self._validate_adjust(adjust)
        if start > end:
            raise RequestError("start must be on or before end")
        if end > self.today():
            raise RequestError("end cannot be later than today")

        outcome = _Outcome()
        raw_rows = self.store.query_range(symbol, start, end)

        if refresh == "always" or (refresh == "missing" and not raw_rows):
            with self._lock_for(symbol):
                raw_rows = self.store.query_range(symbol, start, end)
                if refresh == "always" or not raw_rows:
                    try:
                        outcome = self._refresh_window(symbol, start, end)
                    except ReadOnlyError:
                        outcome = _Outcome(error=READ_ONLY_ERROR)
                    raw_rows = self.store.query_range(symbol, start, end)
        elif refresh == "missing":
            views = [self.view(symbol, r) for r in raw_rows]
            nonfinal_from = self._nonfinal_due(symbol, views, self._now())
            last = max(v["date"] for v in views)
            tail_from = last if (last < end and self._tail_check_due(symbol)) else None
            candidates = [d for d in (nonfinal_from, tail_from) if d is not None]
            if candidates:
                with self._lock_for(symbol):
                    # 排队期间别的请求可能已经修复过
                    raw_rows = self.store.query_range(symbol, start, end)
                    views = [self.view(symbol, r) for r in raw_rows]
                    nonfinal_from = self._nonfinal_due(symbol, views, self._now())
                    last = max(v["date"] for v in views)
                    tail_from = last if (last < end and self._tail_check_due(symbol)) else None
                    candidates = [d for d in (nonfinal_from, tail_from) if d is not None]
                    if candidates:
                        outcome = self._guarded_refresh(symbol, min(candidates), end)
                        raw_rows = self.store.query_range(symbol, start, end)

        views = [self.view(symbol, r) for r in raw_rows]
        self._report_backoff(symbol, outcome, refresh)
        return self._payload(
            symbol, views, outcome, refresh, adjust, start=start, end=end
        )

    def get_latest(
        self, symbol: str, refresh: str = "missing", adjust: str = "none"
    ) -> Dict[str, Any]:
        symbol = normalize_symbol(symbol)
        self._validate_policy(refresh)
        self._validate_adjust(adjust)
        today = self.today()
        outcome = _Outcome()
        row = self.store.query_latest(symbol)

        if refresh == "always" or (refresh == "missing" and row is None):
            with self._lock_for(symbol):
                row = self.store.query_latest(symbol)
                if refresh == "always" or row is None:
                    try:
                        outcome = self._refresh_window(symbol, today - timedelta(days=45), today)
                    except ReadOnlyError:
                        outcome = _Outcome(error=READ_ONLY_ERROR)
                    row = self.store.query_latest(symbol)
        elif refresh == "missing":
            view = self.view(symbol, row)
            nonfinal_from = self._nonfinal_due(symbol, [view], self._now())
            tail_from = view["date"] if (view["date"] < today and self._tail_check_due(symbol)) else None
            candidates = [d for d in (nonfinal_from, tail_from) if d is not None]
            if candidates:
                with self._lock_for(symbol):
                    outcome = self._guarded_refresh(symbol, min(candidates), today)
                    row = self.store.query_latest(symbol)

        views = [self.view(symbol, row)] if row else []
        self._report_backoff(symbol, outcome, refresh)
        payload = self._payload(symbol, views, outcome, refresh, adjust)
        payload["data"] = payload["data"][0] if payload["data"] else None
        return payload

    def _report_backoff(self, symbol: str, outcome: _Outcome, refresh: str) -> None:
        """静默期内没有真正回源，但缓存数据可能因此偏旧——继续如实报告上次的失败原因."""
        if outcome.error is None and refresh == "missing":
            backoff = self._backoff.get(symbol)
            if backoff and self._monotonic() < backoff[0]:
                outcome.error = backoff[1]

    # ---------------------------------------------------------------- payload
    def _payload(
        self,
        symbol: str,
        views: List[Dict[str, Any]],
        outcome: _Outcome,
        refresh: str,
        adjust: str,
        *,
        start: Optional[date] = None,
        end: Optional[date] = None,
    ) -> Dict[str, Any]:
        if adjust == "qfq" and views:
            views = self._adjust(symbol, views)
        bases = sorted({v["price_basis"] for v in views})
        mode = "legacy" if any(b not in STABLE_BASES for b in bases) else "raw"
        sources = [v["source"] for v in views if v["source"]]
        payload: Dict[str, Any] = {
            "symbol": symbol,
            "series_type": series_type_for_symbol(symbol),
            "upstream_route": upstream_route_for_symbol(symbol, mode),
            "upstream_source": outcome.source or (sources[-1] if sources else None),
            "refresh_policy": refresh,
            "refreshed": outcome.refreshed,
            "tail_refreshed": outcome.refreshed and refresh == "missing",
            "tail_refresh_error": outcome.error,
            "cache_status": (
                "filled" if views else "empty_after_refresh" if outcome.refreshed else "empty"
            ),
            "price_view": "adjusted_qfq" if adjust == "qfq" else "as_stored",
            "price_basis": bases[0] if len(bases) == 1 else ("mixed" if bases else None),
            "price_bases": bases,
            "nonfinal_count": sum(1 for v in views if not v["is_final"]),
            "corrected_rows": outcome.corrected_rows,
            "data": views,
        }
        if start is not None and end is not None:
            payload["start"] = start.isoformat()
            payload["end"] = end.isoformat()
            payload["count"] = len(views)
            payload["coverage"] = coverage_info([v["date"] for v in views], start, end)
        return payload

    def _adjust(self, symbol: str, views: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        bad = {v["price_basis"] for v in views if v["price_basis"] not in STABLE_BASES}
        if bad:
            raise RequestError(
                f"adjust=qfq 不可用：{symbol} 的价格口径 {sorted(bad)} 已经是调整价或口径未知，"
                "请使用 adjust=none"
            )
        return apply_dividend_adjustment(views, self.store.list_actions(symbol))


def coverage_info(
    dates: List[date], start: date, end: date, tolerance: int = GAP_TOLERANCE_DAYS
) -> Dict[str, Any]:
    """明确暴露缓存对请求区间的覆盖情况；缺口不会被静默补齐."""
    ordered = sorted(dates)
    gaps: List[Dict[str, Any]] = []
    if not ordered:
        return {
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "first_date": None,
            "last_date": None,
            "complete": False,
            "gaps": [{"from": start.isoformat(), "to": end.isoformat(), "kind": "all"}],
        }
    if (ordered[0] - start).days > tolerance:
        gaps.append(
            {"from": start.isoformat(), "to": (ordered[0] - timedelta(days=1)).isoformat(), "kind": "head"}
        )
    for prev, cur in zip(ordered, ordered[1:]):
        if (cur - prev).days > tolerance:
            gaps.append(
                {
                    "from": (prev + timedelta(days=1)).isoformat(),
                    "to": (cur - timedelta(days=1)).isoformat(),
                    "kind": "internal",
                }
            )
    if (end - ordered[-1]).days > tolerance:
        gaps.append(
            {"from": (ordered[-1] + timedelta(days=1)).isoformat(), "to": end.isoformat(), "kind": "tail"}
        )
    return {
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "first_date": ordered[0].isoformat(),
        "last_date": ordered[-1].isoformat(),
        "complete": not gaps,
        "gaps": gaps,
    }


def apply_dividend_adjustment(
    views: List[Dict[str, Any]], actions: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """用已记录的分红事件计算前复权价（只对不复权口径可用，不落库）.

    因子沿用 Yahoo/CRSP 的做法：除息日之前所有价格乘 ``1 - 分红 / 除息日前一交易日收盘价``。
    """
    dividends = sorted(
        (a for a in actions if a["kind"] == "dividend" and a["value"] > 0), key=lambda a: a["ex_date"]
    )
    if not dividends:
        return [dict(v) for v in views]
    ordered = sorted(views, key=lambda v: v["date"])
    factors = [1.0] * len(ordered)
    for action in dividends:
        prior = [i for i, v in enumerate(ordered) if v["date"] < action["ex_date"]]
        if not prior:
            continue
        prev_close = ordered[prior[-1]]["close_price"]
        if not prev_close or action["value"] >= prev_close:
            continue
        factor = 1.0 - action["value"] / prev_close
        for i in prior:
            factors[i] *= factor
    out = []
    for v, f in zip(ordered, factors):
        item = dict(v)
        for col in ("open_price", "close_price", "high", "low"):
            item[col] = round(item[col] * f, 6)
        out.append(item)
    return out
