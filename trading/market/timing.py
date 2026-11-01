"""每次工具调用的服务端耗时统计：总耗时、DB 往返/累计毫秒、上游回源次数/毫秒、逐标的明细.

用 ContextVar 把统计对象带进线程池里的同步代码；没有统计对象时（tt-fetcher 等）所有记录都是空操作。
"""
from __future__ import annotations

import contextlib
import functools
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional


@dataclass
class CallStats:
    started: float = field(default_factory=time.perf_counter)
    db_ops: int = 0  # 进入存储层的次数（含建连、pre_ping、提交/回滚的完整往返）
    db_ms: float = 0.0
    db_statements: int = 0  # 实际执行的 SQL 语句条数
    upstream_calls: int = 0
    upstream_ms: float = 0.0
    symbols: List[Dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, kind: str, ms: float) -> None:
        with self._lock:
            if kind == "db":
                self.db_ops += 1
                self.db_ms += ms
            else:
                self.upstream_calls += 1
                self.upstream_ms += ms

    def count_statement(self) -> None:
        with self._lock:
            self.db_statements += 1

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            out: Dict[str, Any] = {
                "server_ms": round((time.perf_counter() - self.started) * 1000, 1),
                "db_ops": self.db_ops,
                "db_statements": self.db_statements,
                "db_ms": round(self.db_ms, 1),
                "upstream_calls": self.upstream_calls,
                "upstream_ms": round(self.upstream_ms, 1),
            }
            if self.symbols:
                out["symbols"] = list(self.symbols)
            return out


current: ContextVar[Optional[CallStats]] = ContextVar("tt_call_stats", default=None)


@contextlib.contextmanager
def timed(kind: str) -> Iterator[None]:
    stats = current.get()
    if stats is None:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        stats.add(kind, (time.perf_counter() - start) * 1000)


def per_symbol(fn):
    """给 cache.get_range / get_latest 记逐标的耗时（总耗时、其中 DB 与上游各占多少、是否回源）."""

    @functools.wraps(fn)
    def wrapper(self, symbol, *args, **kwargs):
        stats = current.get()
        if stats is None:
            return fn(self, symbol, *args, **kwargs)
        db0, up0, t0 = stats.db_ms, stats.upstream_ms, time.perf_counter()
        result = fn(self, symbol, *args, **kwargs)
        with stats._lock:
            stats.symbols.append(
                {
                    "symbol": str(symbol).strip().upper(),
                    "ms": round((time.perf_counter() - t0) * 1000, 1),
                    "db_ms": round(stats.db_ms - db0, 1),
                    "upstream_ms": round(stats.upstream_ms - up0, 1),
                    "refreshed": bool(result.get("refreshed")),
                }
            )
        return result

    return wrapper


def install_statement_counter(engine) -> None:
    from sqlalchemy import event

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        stats = current.get()
        if stats is not None:
            stats.count_statement()
