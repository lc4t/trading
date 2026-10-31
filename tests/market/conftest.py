"""行情缓存测试夹具：SQLite 内存库 + 伪造 fetcher + 可控时钟，不访问网络、不需要 MySQL。"""
from __future__ import annotations

import datetime as dt
import warnings
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from trading.market.cache import MarketCache
from trading.market.fetchers import FetchResult
from trading.market.store import MarketStore

warnings.filterwarnings("ignore", message=".*Decimal.*SQLite.*")


def cn_time(year, month, day, hour=0, minute=0) -> dt.datetime:
    """北京时间 → naive UTC（测试里用北京时间写更直观）。"""
    local = dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    return local.astimezone(dt.timezone.utc).replace(tzinfo=None)


def make_row(symbol: str, day: dt.date, close: float, *, volume: int = 1000) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "date": day,
        "open_price": close - 0.01,
        "close_price": close,
        "high": close + 0.02,
        "low": close - 0.02,
        "volume": volume,
        "amount": None,
        "change": None,
        "change_pct": None,
    }


@dataclass
class Clock:
    now: dt.datetime
    mono: float = 0.0

    def __call__(self) -> dt.datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def advance(self, **kwargs) -> None:
        delta = dt.timedelta(**kwargs)
        self.now += delta
        self.mono += delta.total_seconds()


@dataclass
class FakeFetcher:
    """按 symbol 存一份「上游当前数据」；记录每次调用的参数。"""

    data: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    basis: str = "raw"
    source: str = "fake_upstream"
    actions: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    fail: Optional[Exception] = None
    calls: List[tuple] = field(default_factory=list)

    def set_rows(self, symbol: str, rows: List[Dict[str, Any]]) -> None:
        self.data[symbol] = rows

    def fetch(self, symbol, start, end, *, mode="raw", retry=None) -> FetchResult:
        self.calls.append((symbol, start, end, mode))
        if self.fail is not None:
            raise self.fail
        rows = [dict(r) for r in self.data.get(symbol, []) if start <= r["date"] <= end]
        basis = self.basis if mode == "raw" else "ths_qfq"
        return FetchResult(rows, self.source, basis, actions=list(self.actions.get(symbol, [])))


@pytest.fixture
def store() -> MarketStore:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    s = MarketStore(engine)
    s.init_schema()
    return s


@pytest.fixture
def clock() -> Clock:
    # 2024-06-05（周三）北京时间 10:00，A 股盘中
    return Clock(cn_time(2024, 6, 5, 10, 0))


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher()


@pytest.fixture
def cache(store, fetcher, clock) -> MarketCache:
    return MarketCache(
        store,
        fetcher,
        now=clock,
        monotonic=clock.monotonic,
        tail_check_ttl=600,
        intraday_ttl=300,
        failure_backoff=60,
    )
