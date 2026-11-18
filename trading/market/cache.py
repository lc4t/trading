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
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from .clock import (
    as_of_for,
    awaiting_close,
    business_today,
    to_iso,
    wait_text,
    weekday_on_or_before,
    final_ready_at,
    market_today,
    is_final_snapshot,
    local_naive_to_utc,
    utcnow,
)
from . import corporate
from .basis import (
    BACKUP_BASES,
    MISMATCH,
    OVERLAP,
    append_rule,
    check_overlap_rows,
)
from .errors import (
    BackupExhausted,
    BackupFailed,
    BasisMismatch,
    FetchError,
    RateLimited,
    ReadOnlyError,
    RequestError,
)
from .fetchers import (
    BACKUP_BASIS,
    RangePriceFetcher,
    backup_sources,
    overseas_kind,
    series_type_for_symbol,
    upstream_route_for_symbol,
)
from .models import STABLE_BASES
from .store import MarketStore
from .timing import per_symbol, timed

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9^._=-]{1,20}$")
OFF_MARKET_FUND_PATTERN = re.compile(r"^\d{6}$")
REFRESH_POLICIES = ("missing", "always", "never")
ADJUST_MODES = ("none", "qfq")

DEFAULT_TAIL_CHECK_TTL = 600.0  # 尾部（缓存最新日之后）检查间隔
DEFAULT_INTRADAY_TTL = 300.0  # 盘中非终值行的最长缓存时间
RATE_LIMITED_ERROR = "rate_limited"  # tail_refresh_error 的固定取值：上游限流/冷却中，同时带 retry_after（秒）
FLIGHT_WAIT_SECONDS = 90.0  # 等待同一标的正在进行的刷新的上限
READ_ONLY_ERROR = "read_only"  # tail_refresh_error 的固定取值：数据库只读，没有回源
BASIS_MISMATCH_ERROR = "basis_mismatch"  # tail_refresh_error 的固定取值：备用源口径与已存序列不同，没有追加
BACKUP_NO_FINAL_WINDOW = 45  # 找「最后一个已定稿行」时往前看的天数
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
    retry_after: Optional[int] = None
    fallback: Optional[Dict[str, Any]] = None  # 这次刷新用了备用源：{reason, source, price_basis, attempts, overlap}
    basis_mismatch: Optional[Dict[str, Any]] = None  # 备用源因口径不同 / 重叠日校验失败没有追加
    attempts: Optional[List[Dict[str, Any]]] = None  # 备用源逐个的结果（失败时给调用方看）


class _Flight:
    """同一标的正在进行的上游刷新；并发的刷新请求挂在它上面等结果，而不是各自再打一次上游."""

    def __init__(self, start: date, end: date) -> None:
        self.start, self.end = start, end
        self.done = threading.Event()
        self.outcome: Optional[_Outcome] = None
        self.exc: Optional[BaseException] = None


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
        backup: bool = True,
    ) -> None:
        self.store = store
        self.fetcher = fetcher
        self._now = now
        self._monotonic = monotonic
        self._tail_ttl = tail_check_ttl
        self._intraday_ttl = intraday_ttl
        self._failure_backoff = failure_backoff
        self._tail_checked_at: Dict[str, float] = {}
        self._tail_checked_wall: Dict[str, dt.datetime] = {}
        self._backoff: Dict[str, tuple[float, _Outcome]] = {}
        self._backup = backup
        self._flights: Dict[str, _Flight] = {}
        self._flights_guard = threading.Lock()

    # ----------------------------------------------------------------- helpers
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

    def _stamp_tail_check(self, symbol: str) -> None:
        self._tail_checked_at[symbol] = self._monotonic()
        self._tail_checked_wall[symbol] = self._now()

    def _tail_check_due(self, symbol: str, finalizable_day: Optional[date] = None) -> bool:
        """尾部检查是否到期：超过 TTL；或者上一次检查发生在 ``finalizable_day`` 可定稿之前、而现在已经可以定稿
        （刚过收盘缓冲时，第一次请求就该去取当天的收盘价，不必再等 TTL）。"""
        checked = self._tail_checked_at.get(symbol)
        if checked is None or self._monotonic() - checked >= self._tail_ttl:
            return True
        if finalizable_day is not None:
            wall = self._tail_checked_wall.get(symbol)
            ready = final_ready_at(symbol, finalizable_day)
            return wall is not None and wall < ready <= self._now()
        return False

    # ------------------------------------------------------------------ fetch
    def _backup_possible(self, symbol: str) -> bool:
        if not self._backup or overseas_kind(symbol) is None:
            return False
        probe = getattr(self.fetcher, "has_backup", None)
        return bool(probe()) if callable(probe) else hasattr(self.fetcher, "fetch_backup")

    def _series_state(self, symbol: str) -> tuple[Optional[str], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """已存序列的口径状态：(序列口径, 最后一个已定稿行, 最后一行). 序列口径 = 最后一个已定稿行的口径；
        没有已定稿行就取最后一行；库里没有这个标的则为 None（空序列，任何口径都可以起头）."""
        latest = self.store.query_latest(symbol)
        if latest is None:
            return None, None, None
        tail = self.view(symbol, latest)
        anchor: Optional[Dict[str, Any]] = tail if tail["is_final"] else None
        if anchor is None:
            recent = self.store.query_range(symbol, tail["date"] - timedelta(days=BACKUP_NO_FINAL_WINDOW), tail["date"])
            finals = [v for v in (self.view(symbol, r) for r in recent) if v["is_final"]]
            anchor = finals[-1] if finals else None
        return (anchor or tail)["price_basis"], anchor, tail

    def _fetch_and_persist(self, symbol: str, start: date, end: date) -> tuple[Any, int]:
        mode = self.store.series_mode(symbol)
        result = None
        state = None
        if mode == "legacy" and self._backup_possible(symbol):
            state = self._series_state(symbol)
            if state[0] in BACKUP_BASES:
                # 序列已经是备用口径（不复权 / 腾讯前复权）：Yahoo 的行接不上去，继续用备用源，不去碰 Yahoo
                result = self._fetch_backup(symbol, start, end, state, cause=None)
        if result is None:
            try:
                with timed("upstream"):
                    result = self.fetcher.fetch(symbol, start, end, mode=mode)
            except RateLimited as exc:
                # 港股 / 美股的主上游只有 Yahoo：它限流 / 冷却中才切换到备用源（备用源自己的限流在 _fetch_backup 里处理）
                if not self._backup_possible(symbol):
                    raise
                state = state or self._series_state(symbol)
                result = self._fetch_backup(symbol, start, end, state, cause=exc)
        if result.fallback is not None:
            rows = list(result.rows)  # 备用源已经按「只追加尾部」裁好了
        else:
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

    def _fetch_backup(
        self,
        symbol: str,
        start: date,
        end: date,
        state: tuple[Optional[str], Optional[Dict[str, Any]], Optional[Dict[str, Any]]],
        cause: Optional[RateLimited],
    ) -> Any:
        """Yahoo 不可达时从备用源取数，并保证**不同口径不拼接**.

        逐个备用源尝试（顺序见 ``backup_sources``）：口径锁（``append_rule``）→ 请求（受限流冷却/间隔保护，
        每个 HTTP 请求最多 2 次）→ 需要时做重叠日校验 → 只保留「可定稿」且在已存序列尾部之后的行.
        所有备用源都没给出可追加的数据时抛出带明确原因的异常（口径不符 / 备用源失败 / 备用源也限流）.
        """
        series_basis, anchor, tail = state
        fetched_at = self._now()
        attempts: List[Dict[str, Any]] = []
        mismatch: Optional[Dict[str, Any]] = None
        reason = "yahoo_rate_limited" if cause is not None else "series_basis_locked"
        for source in backup_sources(symbol):
            basis = BACKUP_BASIS[source]
            rule = append_rule(series_basis, basis)
            detail = {"series_basis": series_basis, "candidate_basis": basis, "source": source}
            if rule == MISMATCH:
                mismatch = mismatch or {**detail, "reason": "series_basis_differs"}
                attempts.append({"source": source, "result": "skipped_basis_mismatch", "price_basis": basis})
                continue
            if rule == OVERLAP and anchor is None:
                mismatch = mismatch or {**detail, "reason": "no_final_row_to_verify"}
                attempts.append({"source": source, "result": "skipped_no_overlap_anchor", "price_basis": basis})
                continue
            win_start = min(start, anchor["date"]) if anchor is not None else start
            try:
                with timed("upstream"):
                    res = self.fetcher.fetch_backup(symbol, win_start, end, source)
            except RateLimited as exc:
                attempts.append({"source": source, "result": "rate_limited", "retry_after": math.ceil(exc.retry_after)})
                continue
            except FetchError as exc:
                attempts.append({"source": source, "result": "fetch_failed", "detail": str(exc)})
                continue
            rows = [r for r in res.rows if win_start <= r["date"] <= end]
            if not rows:
                attempts.append({"source": source, "result": "no_rows"})
                continue
            overlap = None
            if rule == OVERLAP:
                check = check_overlap_rows(anchor, rows)
                overlap = check.as_dict()
                if not check.ok:
                    mismatch = {**detail, "reason": "overlap_check_failed", "overlap": overlap}
                    attempts.append({"source": source, "result": "overlap_check_failed", "overlap": overlap})
                    continue
            rows = self._appendable(rows, anchor, tail, same_basis=(series_basis == basis))
            # 只写已可定稿的行：备用源不提供盘中价（盘中值需要 intraday + 可用的 Yahoo）
            rows = [r for r in rows if is_final_snapshot(symbol, r["date"], fetched_at)]
            attempts.append({"source": source, "result": "ok", "rows": len(rows)})
            res.rows = rows
            res.fallback = {
                "used": True,
                "reason": reason,
                "source": source,
                "price_basis": basis,
                "overlap": overlap,
                "attempts": attempts,
            }
            logger.warning(f"{symbol} 使用备用上游 {source}（{reason}），写入 {len(rows)} 行，口径 {basis}")
            return res

        errors = [a for a in attempts if a["result"] in {"rate_limited", "fetch_failed"}]
        refused_by_data = any(a["result"] == "overlap_check_failed" for a in attempts)
        # 备用源抓到了数据、但重叠日对不上：这是数据层面的口径冲突，如实报 basis_mismatch。
        # 仅仅是序列口径与备用口径不同（根本没去请求）：Yahoo 恢复后序列照常续上，所以对外仍是 rate_limited，
        # basis_mismatch 只作为附带说明
        if mismatch is not None and not errors and (refused_by_data or cause is None):
            raise BasisMismatch(mismatch, attempts)
        limited = [a["retry_after"] for a in attempts if a["result"] == "rate_limited"]
        if cause is not None or limited:
            hints = ([cause.retry_after] if cause is not None else []) + limited
            if any(a["result"] == "fetch_failed" for a in attempts):
                hints.append(self._failure_backoff)  # 有备用源只是临时失败：不必等到 Yahoo 冷却结束
            raise BackupExhausted(
                cause.source if cause is not None else "backup",
                min(hints),
                attempts,
                basis_mismatch=mismatch,
            ) from cause
        raise BackupFailed(attempts)

    @staticmethod
    def _appendable(
        rows: List[Dict[str, Any]],
        anchor: Optional[Dict[str, Any]],
        tail: Optional[Dict[str, Any]],
        *,
        same_basis: bool,
    ) -> List[Dict[str, Any]]:
        """备用源只追加尾部，不改写已有的历史行：口径不同的序列只写锚点日之后的行；
        同口径可以连锚点日一起刷新；没有已定稿行时从最后一行（可能是盘中行）开始."""
        if anchor is not None:
            floor = anchor["date"]
            return [r for r in rows if r["date"] > floor or (same_basis and r["date"] == floor)]
        if tail is not None:
            return [r for r in rows if r["date"] >= tail["date"]]
        return rows

    def _refresh_window(self, symbol: str, start: date, end: date) -> _Outcome:
        """抓 [start, end] 并落库；检测到会改写历史的新公司行动时，把更早的历史也重写."""
        if self.store.read_only:
            raise ReadOnlyError("database is read-only")
        first_cached = self.store.first_date(symbol)
        result, _ = self._fetch_and_persist(symbol, start, end)
        self._stamp_tail_check(symbol)  # 刚回过源，尾部检查间隔从现在算起
        outcome = _Outcome(refreshed=True, source=result.source, fallback=result.fallback)
        if result.fallback is not None:
            # 备用源不提供分红 / 拆股事件：不建「首次检测」基线，否则 Yahoo 恢复后第一次同步会把存量事件误判成新事件而整段重写
            return outcome
        verdict = corporate.assess(self.store, symbol, result, self._now())
        if verdict.trigger and first_cached is not None and first_cached < start:
            logger.warning(f"{symbol} 检测到新的公司行动 {[(a['kind'], a['ex_date']) for a in verdict.new]}，整段历史重写")
            if result.price_basis in STABLE_BASES:
                outcome.corrected_rows = self.store.invalidate_before(symbol, start)  # 失败时留下「非终值」标记等下次重试
            try:
                self._fetch_and_persist(symbol, first_cached, start - timedelta(days=1))
            except FetchError as exc:
                failed = self._error_outcome(exc)
                outcome.error = f"history_refetch_failed: {failed.error}" if failed.retry_after is None else failed.error
                outcome.retry_after = failed.retry_after
                return outcome  # 新事件不记录：下次刷新仍会判定为「新」，继续重试
        corporate.commit(self.store, symbol, verdict.new, self._now())
        return outcome

    @staticmethod
    def _error_outcome(exc: BaseException) -> _Outcome:
        """把刷新失败翻译成对外的 tail_refresh_error（限流 / 只读 / 其它）."""
        if isinstance(exc, BackupExhausted):
            return _Outcome(
                error=RATE_LIMITED_ERROR,
                retry_after=math.ceil(exc.retry_after),
                attempts=exc.attempts,
                basis_mismatch=exc.basis_mismatch,
            )
        if isinstance(exc, RateLimited):
            return _Outcome(error=RATE_LIMITED_ERROR, retry_after=math.ceil(exc.retry_after))
        if isinstance(exc, ReadOnlyError):
            return _Outcome(error=READ_ONLY_ERROR)
        if isinstance(exc, BasisMismatch):
            return _Outcome(error=BASIS_MISMATCH_ERROR, basis_mismatch=exc.detail, attempts=exc.attempts)
        if isinstance(exc, BackupFailed):
            return _Outcome(error=f"BackupFailed: {exc}", attempts=exc.attempts)
        # 对外只给自己组装的 FetchError 文案；其它异常的消息可能带路径等内部细节，只暴露类型
        message = f"{type(exc).__name__}: {exc}" if isinstance(exc, FetchError) else type(exc).__name__
        return _Outcome(error=message)

    def _guarded_refresh(self, symbol: str, start: date, end: date) -> _Outcome:
        """尾部/非终值修复用：失败不抛出，只报告；失败后进入静默期（限流另由上游冷却保护）."""
        active = self._backoff_outcome(symbol)
        if active is not None:
            return active
        try:
            outcome = self._refresh_window(symbol, start, end)
        except BackupExhausted as exc:
            outcome = self._error_outcome(exc)
            if any(a["result"] == "fetch_failed" for a in exc.attempts):
                # 备用源只是临时失败（没有冷却保护）：按失败静默期处理，避免每次读取都把 2~3 个备用源再打一遍
                self._stamp_tail_check(symbol)
                self._set_backoff(symbol, outcome, min(outcome.retry_after or self._failure_backoff, self._failure_backoff))
            # 其余情形（Yahoo 冷却 + 备用源口径不符 / 自己也在冷却）都由各来源的冷却保护兜底：
            # 不记 tail 检查时间、不进静默期，冷却结束后第一次读取就会恢复，retry_after 随冷却递减
            return outcome
        except (RateLimited, ReadOnlyError) as exc:
            # 冷却中：不记 tail 检查时间，也不进失败静默期——冷却结束后第一次读取就会恢复
            return self._error_outcome(exc)
        except Exception as exc:  # noqa: BLE001 - 上游任何失败都降级为返回缓存
            logger.warning(f"tail refresh failed for {symbol}: {type(exc).__name__}: {exc}")
            outcome = self._error_outcome(exc)
            self._stamp_tail_check(symbol)
            self._set_backoff(symbol, outcome, self._failure_backoff)
            return outcome
        self._stamp_tail_check(symbol)
        if outcome.error:
            self._set_backoff(symbol, outcome, self._failure_backoff)
        else:
            self._backoff.pop(symbol, None)
        return outcome

    def _set_backoff(self, symbol: str, outcome: _Outcome, seconds: float) -> None:
        self._backoff[symbol] = (self._monotonic() + seconds, outcome)

    def _backoff_outcome(self, symbol: str) -> Optional[_Outcome]:
        """静默期内：不回源，把上次失败的原因原样（含 retry_after 剩余秒数、口径不符详情）带出来."""
        backoff = self._backoff.get(symbol)
        if not backoff or self._monotonic() >= backoff[0]:
            return None
        until, last = backoff
        remaining = max(math.ceil(until - self._monotonic()), 1)
        return _Outcome(
            refreshed=False,
            error=last.error,
            retry_after=remaining if last.retry_after is not None else None,
            basis_mismatch=last.basis_mismatch,
            attempts=last.attempts,
        )

    def _plain_refresh(self, symbol: str, start: date, end: date) -> _Outcome:
        """显式刷新 / 缓存为空时的回源：限流与只读转成 outcome，其它失败照常抛出."""
        try:
            return self._refresh_window(symbol, start, end)
        except (RateLimited, ReadOnlyError, BasisMismatch) as exc:
            return self._error_outcome(exc)

    def _refresh_coalesced(self, symbol: str, start: date, end: date, *, guarded: bool) -> _Outcome:
        """同一标的的并发刷新合并成一次（single-flight）：先到的执行，后到的等它并共享结果；
        只有当先到者的窗口覆盖不了后到者的区间时，后到者才会在它结束后自己再刷新一次."""
        while True:
            with self._flights_guard:
                flight = self._flights.get(symbol)
                leader = flight is None
                if leader:
                    flight = self._flights[symbol] = _Flight(start, end)
            if leader:
                break
            flight.done.wait(FLIGHT_WAIT_SECONDS)
            if flight.done.is_set() and flight.start <= start and flight.end >= end:
                if flight.exc is not None:
                    if guarded:
                        return self._error_outcome(flight.exc)
                    raise flight.exc
                return flight.outcome or _Outcome()
        try:
            outcome = (
                self._guarded_refresh(symbol, start, end)
                if guarded
                else self._plain_refresh(symbol, start, end)
            )
            flight.outcome = outcome
            return outcome
        except BaseException as exc:
            flight.exc = exc
            raise
        finally:
            with self._flights_guard:
                self._flights.pop(symbol, None)
            flight.done.set()

    # ------------------------------------------------------------------ reads
    def _awaiting(self, symbol: str, end: date, intraday: bool) -> Optional[Dict[str, Any]]:
        """请求范围包含该标的所在市场的「当日交易日」且还没到可定稿时刻 → 收盘前不回源，也不返回盘中行."""
        if intraday:
            return None
        info = awaiting_close(symbol, self._now())
        if info is None or end < info["date"]:
            return None
        return info

    def _awaiting_view(self, info: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if info is None:
            return None
        wait = max(int(math.ceil((info["ready"] - self._now()).total_seconds())), 0)
        return {
            "date": info["date"].isoformat(),
            "final_ready_at": to_iso(info["ready"]),
            "wait_seconds": wait,
        }

    @per_symbol
    def get_range(
        self,
        symbol: str,
        start: date,
        end: date,
        refresh: str = "missing",
        adjust: str = "none",
        intraday: bool = False,
    ) -> Dict[str, Any]:
        symbol = normalize_symbol(symbol)
        self._validate_policy(refresh)
        self._validate_adjust(adjust)
        if start > end:
            raise RequestError("start must be on or before end")
        if end > self.today():
            raise RequestError("end cannot be later than today")

        awaiting = self._awaiting(symbol, end, intraday)
        # 收盘前：当日这一行不可能是收盘价，所以回源窗口只到前一天；整个请求只有当日时根本不回源
        fetch_end = awaiting["date"] - timedelta(days=1) if awaiting else end
        can_fetch = fetch_end >= start
        outcome = _Outcome()
        raw_rows = self.store.query_range(symbol, start, end)
        fetchable = [r for r in raw_rows if r["date"] <= fetch_end]

        if refresh == "always" or (refresh == "missing" and not fetchable):
            if can_fetch:
                outcome = self._refresh_coalesced(symbol, start, fetch_end, guarded=False)
                raw_rows = self.store.query_range(symbol, start, end)
        elif refresh == "missing" and can_fetch:
            views = [self.view(symbol, r) for r in fetchable]
            nonfinal_from = self._nonfinal_due(symbol, views, self._now())
            last = max(v["date"] for v in views)
            # 收盘前只要求缓存追到前一个工作日（周末没有交易，不需要追）
            expected = weekday_on_or_before(fetch_end) if awaiting else end
            finalizable = None if awaiting else weekday_on_or_before(min(end, market_today(symbol, self._now())))
            tail_from = last if (last < expected and self._tail_check_due(symbol, finalizable)) else None
            candidates = [d for d in (nonfinal_from, tail_from) if d is not None]
            if candidates:
                outcome = self._refresh_coalesced(symbol, min(candidates), fetch_end, guarded=True)
                raw_rows = self.store.query_range(symbol, start, end)

        views = [self.view(symbol, r) for r in raw_rows]
        if awaiting:  # 默认不返回当日的盘中快照，只给定稿的收盘价
            views = [v for v in views if not (v["date"] >= awaiting["date"] and not v["is_final"])]
        self._report_backoff(symbol, outcome, refresh)
        payload = self._payload(symbol, views, outcome, refresh, adjust, start=start, end=end)
        payload["awaiting_close"] = self._awaiting_view(awaiting)
        return payload

    @per_symbol
    def get_latest(
        self, symbol: str, refresh: str = "missing", adjust: str = "none", intraday: bool = False
    ) -> Dict[str, Any]:
        symbol = normalize_symbol(symbol)
        self._validate_policy(refresh)
        self._validate_adjust(adjust)
        today = self.today()
        awaiting = self._awaiting(symbol, today, intraday)
        before = awaiting["date"] if awaiting else None
        fetch_end = awaiting["date"] - timedelta(days=1) if awaiting else today
        outcome = _Outcome()
        row = self.store.query_latest(symbol, before=before)

        if refresh == "always" or (refresh == "missing" and row is None):
            outcome = self._refresh_coalesced(symbol, fetch_end - timedelta(days=45), fetch_end, guarded=False)
            row = self.store.query_latest(symbol, before=before)
        elif refresh == "missing":
            view = self.view(symbol, row)
            nonfinal_from = self._nonfinal_due(symbol, [view], self._now())
            expected = weekday_on_or_before(fetch_end) if awaiting else today
            finalizable = None if awaiting else weekday_on_or_before(min(today, market_today(symbol, self._now())))
            tail_from = view["date"] if (view["date"] < expected and self._tail_check_due(symbol, finalizable)) else None
            candidates = [d for d in (nonfinal_from, tail_from) if d is not None]
            if candidates:
                outcome = self._refresh_coalesced(symbol, min(candidates), fetch_end, guarded=True)
                row = self.store.query_latest(symbol, before=before)

        views = [self.view(symbol, row)] if row else []
        self._report_backoff(symbol, outcome, refresh)
        payload = self._payload(symbol, views, outcome, refresh, adjust)
        payload["data"] = payload["data"][0] if payload["data"] else None
        info = self._awaiting_view(awaiting)
        payload["awaiting_close"] = info
        if info:
            got = payload["data"]["date"] if payload["data"] else None
            payload["awaiting_close_message"] = (
                f"{info['date']} 的收盘价要等到 {info['final_ready_at']} 之后才能获取（还需{wait_text(info['wait_seconds'])}），"
                + (f"当前返回的是 {got.isoformat()} 的收盘价。" if got else "目前没有更早的已定稿数据。")
                + "需要盘中价请传 intraday=true。"
            )
        return payload

    def _report_backoff(self, symbol: str, outcome: _Outcome, refresh: str) -> None:
        """静默期内没有真正回源，但缓存数据可能因此偏旧——继续如实报告上次的失败原因."""
        if outcome.error is None and refresh == "missing":
            active = self._backoff_outcome(symbol)
            if active is not None:
                outcome.error = active.error
                outcome.retry_after = active.retry_after
                outcome.basis_mismatch = active.basis_mismatch
                outcome.attempts = active.attempts

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
        if views and views[-1]["price_basis"] in BACKUP_BASES:
            mode = views[-1]["price_basis"]  # 最新一行来自备用源：路由说明如实反映
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
            "retry_after": outcome.retry_after,
            "fallback": outcome.fallback is not None,
            "fallback_detail": outcome.fallback,
            "basis_mismatch": outcome.basis_mismatch,
            "fallback_attempts": outcome.attempts if outcome.error else None,
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
        if bad & BACKUP_BASES:
            raise RequestError(
                f"adjust=qfq 不可用：{symbol} 的行来自备用上游（{sorted(bad & BACKUP_BASES)}），备用源不提供分红事件，"
                "无法在本服务里前复权，请使用 adjust=none"
            )
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
