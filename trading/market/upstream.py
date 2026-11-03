"""上游来源的限流保护：冷却（熔断）、请求间隔、排队上限、持久化.

设计要点
- **按来源冷却**：命中限流后该来源进入冷却，冷却期内不发起任何上游请求，直接给调用方 ``RateLimited``
  （带 ``retry_after``）。冷却时长指数增长（连续触发越多越久）、带抖动、有上限，成功一次即清零。
- **请求间隔**：同一来源两次上游请求之间至少隔 ``min_interval``；排队等待超过 ``max_queue_wait`` 就不再排，
  直接返回 ``RateLimited``——宁可让调用方稍后再来，也不在请求路径里长时间 sleep。
- **持久化（尽力而为）**：冷却状态写进 ``market_upstream_state`` 旁表，重启和多实例共享；
  写不进去（只读账号 / 无权限）就退回只在进程内生效。取舍：多一次很便宜的旁表读（每 5 秒最多一次）
  和一次写（只在触发冷却时），换来重启后不会立刻又去撞限流。
"""
from __future__ import annotations

import datetime as dt
import math
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from loguru import logger

from .clock import is_off_market_fund
from .errors import FetchError, RateLimited
from .fetchers import is_china_exchange_symbol

SOURCE_YAHOO = "yahoo"
SOURCE_EASTMONEY = "eastmoney"
SOURCE_EASTMONEY_FUND = "eastmoney_fund"
SOURCE_THS = "ths"

STORE_SYNC_SECONDS = 5.0
MAX_QUEUE_WAIT = 5.0


@dataclass(frozen=True)
class UpstreamPolicy:
    base_cooldown: float = 120.0
    max_cooldown: float = 1800.0
    min_interval: float = 0.5


DEFAULT_POLICIES: Dict[str, UpstreamPolicy] = {
    SOURCE_YAHOO: UpstreamPolicy(120.0, 1800.0, 1.0),
    SOURCE_EASTMONEY: UpstreamPolicy(60.0, 900.0, 0.3),
    SOURCE_EASTMONEY_FUND: UpstreamPolicy(60.0, 900.0, 0.3),
    SOURCE_THS: UpstreamPolicy(60.0, 900.0, 1.0),
}


def source_for(symbol: str, mode: str = "raw") -> str:
    if is_off_market_fund(symbol):
        return SOURCE_EASTMONEY_FUND
    if is_china_exchange_symbol(symbol):
        return SOURCE_THS if mode == "legacy" else SOURCE_EASTMONEY
    return SOURCE_YAHOO


class UpstreamGuard:
    def __init__(
        self,
        *,
        policies: Optional[Dict[str, UpstreamPolicy]] = None,
        now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        store: Any = None,
        max_queue_wait: float = MAX_QUEUE_WAIT,
    ) -> None:
        self.policies = dict(DEFAULT_POLICIES)
        self.policies.update(policies or {})
        self._now = now
        self._mono = monotonic
        self._sleep = sleep
        self._rng = rng
        self._store = store
        self._max_queue_wait = max_queue_wait
        self._lock = threading.Lock()
        self._until: Dict[str, dt.datetime] = {}
        self._failures: Dict[str, int] = {}
        self._next_slot: Dict[str, float] = {}
        self._synced_at = -1e9

    def _policy(self, source: str) -> UpstreamPolicy:
        return self.policies.get(source, UpstreamPolicy())

    # ---------------------------------------------------------------- 持久化
    def _store_ok(self) -> bool:
        return self._store is not None and not getattr(self._store, "read_only", False)

    def _sync_from_store(self) -> None:
        if self._store is None or self._mono() - self._synced_at < STORE_SYNC_SECONDS:
            return
        self._synced_at = self._mono()
        try:
            states = self._store.load_cooldowns()
        except Exception:  # noqa: BLE001 - 持久化只是增强，读不到就只用进程内状态
            return
        with self._lock:
            for source, (until, failures) in states.items():
                if until > self._until.get(source, dt.datetime.min):
                    self._until[source] = until
                    self._failures[source] = max(failures, self._failures.get(source, 0))

    # ------------------------------------------------------------------ 查询
    def remaining(self, source: str) -> float:
        """该来源还要冷却多少秒；0 表示可用."""
        self._sync_from_store()
        with self._lock:
            until = self._until.get(source)
        if until is None:
            return 0.0
        return max((until - self._now()).total_seconds(), 0.0)

    # ------------------------------------------------------------------ 动作
    def acquire(self, source: str) -> None:
        """发上游请求前调用：冷却中抛 ``RateLimited``；否则按最小间隔排队（有上限）."""
        left = self.remaining(source)
        if left > 0:
            raise RateLimited(source, math.ceil(left), f"upstream {source} is cooling down after rate limiting")
        policy = self._policy(source)
        with self._lock:
            now = self._mono()
            slot = max(now, self._next_slot.get(source, 0.0))
            wait = slot - now
            if wait > self._max_queue_wait:
                raise RateLimited(source, math.ceil(wait), f"upstream {source} request queue is full")
            self._next_slot[source] = slot + policy.min_interval * (1.0 + 0.2 * self._rng())
        if wait > 0:
            self._sleep(wait)

    def trip(self, source: str, retry_after: Optional[float] = None) -> float:
        """命中限流：进入冷却，返回冷却秒数（指数增长 + 抖动 + 上限；上游给了 Retry-After 就以它为下限）."""
        policy = self._policy(source)
        with self._lock:
            failures = self._failures.get(source, 0) + 1
            self._failures[source] = failures
            duration = min(policy.base_cooldown * (2 ** (failures - 1)), policy.max_cooldown)
            duration *= 0.8 + 0.4 * self._rng()  # ±20% 抖动，避免多实例同时恢复
            if retry_after:
                duration = max(duration, float(retry_after))
            duration = min(max(duration, 1.0), policy.max_cooldown * 1.2)
            until = self._now() + dt.timedelta(seconds=duration)
            if until > self._until.get(source, dt.datetime.min):
                self._until[source] = until
            failures_now = failures
        logger.warning(f"上游 {source} 被限流，冷却 {int(duration)}s（连续第 {failures_now} 次）")
        if self._store_ok():
            try:
                self._store.save_cooldown(source, until, failures_now)
            except Exception:  # noqa: BLE001
                pass
        return duration

    def success(self, source: str) -> None:
        with self._lock:
            had = self._failures.pop(source, 0)
            self._until.pop(source, None)
        if had and self._store_ok():
            try:
                self._store.clear_cooldown(source)
            except Exception:  # noqa: BLE001
                pass


class GuardedFetcher:
    """给任意 fetcher 套上限流保护；对外接口与 ``RangePriceFetcher.fetch`` 一致."""

    def __init__(self, inner: Any, guard: UpstreamGuard) -> None:
        self.inner = inner
        self.guard = guard

    def source_for(self, symbol: str, mode: str = "raw") -> str:
        return source_for(symbol, mode)

    def fetch(self, symbol: str, start, end, *, mode: str = "raw", retry=None):
        source = source_for(symbol, mode)
        self.guard.acquire(source)
        try:
            result = self.inner.fetch(symbol, start, end, mode=mode, retry=retry)
        except RateLimited as exc:
            seconds = self.guard.trip(source, exc.retry_after if exc.retry_after > 1 else None)
            raise RateLimited(source, math.ceil(seconds)) from exc
        except FetchError:
            raise
        self.guard.success(source)
        return result
