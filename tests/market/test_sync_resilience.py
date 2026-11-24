"""trading-fetcher 的失败分级：基准 / 非核心取不到时降级，核心标的没有可用数据才致命（SQLite 内存库，不访问网络）.

背景：Yahoo 对 000300.SS 只给最近一根日线，盘中请求「到今天」（结束日不含当天）会拿到空，
旧逻辑把空数据一律当失败，整条工作流在 A 股盘中被拖垮。
"""
import datetime as dt
from types import SimpleNamespace

import pytest

from trading.data import fetcher as data_fetcher
from trading.data.fetcher import FetchStatus

from .conftest import FakeFetcher, make_row

D = dt.date
CORE = "159915.SZ"
BENCH = "000300.SS"
END = D(2024, 6, 5)  # 周三；「今天」盘中
START = D(2024, 6, 1)


@pytest.fixture
def db_client(store):
    return SimpleNamespace(
        engine=store.engine,
        Session=store.Session,
        get_active_symbols=lambda: [BENCH, CORE],
        close=lambda: None,
    )


@pytest.fixture
def manager(monkeypatch, db_client):
    monkeypatch.setattr(data_fetcher, "DBClient", lambda: db_client)
    # 取数异常会触发 tenacity 重试（4~10 秒退避），测试里不真睡
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda _s: None)
    mgr = data_fetcher.StockDataManager()
    mgr.market_fetcher = FakeFetcher()
    return mgr


def seed_cache(store, symbol, day, close=1.0):
    store.upsert([make_row(symbol, day, close)], price_basis="raw")


def sync(mgr, symbol, end=END):
    return mgr.sync_symbol(symbol, start_date=START, end_date=end, db_client=mgr.db_client)


# ---------------------------------------------------------------- 盘中请求今天无数据 / 基准为空
def test_benchmark_empty_intraday_falls_back_to_cache_and_is_not_fatal(manager, store):
    seed_cache(store, BENCH, D(2024, 6, 4))  # 昨天已缓存；今天上游为空
    outcome = sync(manager, BENCH)
    assert outcome.status is FetchStatus.DEGRADED
    assert "沿用数据库缓存" in outcome.reason and "2024-06-04" in outcome.reason
    assert manager.fetch_data(BENCH, START, END, manager.db_client) is True


def test_benchmark_empty_without_any_cache_is_still_degraded_not_fatal(manager):
    outcome = sync(manager, BENCH)
    assert outcome.status is FetchStatus.DEGRADED
    assert "数据库也没有缓存" in outcome.reason


def test_benchmark_empty_with_stale_cache_is_degraded(manager, store):
    seed_cache(store, BENCH, D(2024, 5, 20))
    outcome = sync(manager, BENCH)
    assert outcome.status is FetchStatus.DEGRADED and "已过旧" in outcome.reason


def test_hang_seng_benchmark_is_recognised_too(manager):
    assert sync(manager, "^HSI").status is FetchStatus.DEGRADED


def test_non_core_symbol_can_be_declared_as_degradable(monkeypatch, db_client):
    monkeypatch.setattr(data_fetcher, "DBClient", lambda: db_client)
    mgr = data_fetcher.StockDataManager(benchmark_symbols=["XYZ.SZ"])
    mgr.market_fetcher = FakeFetcher()
    assert sync(mgr, "XYZ.SZ").status is FetchStatus.DEGRADED
    assert sync(mgr, BENCH).status is FetchStatus.FATAL  # 自定义集合替换默认集合


# ---------------------------------------------------------------- 核心标的
def test_core_symbol_with_no_data_and_no_cache_is_fatal(manager):
    outcome = sync(manager, CORE)
    assert outcome.status is FetchStatus.FATAL
    assert manager.fetch_data(CORE, START, END, manager.db_client) is False


def test_core_symbol_with_stale_cache_and_empty_fetch_is_fatal(manager, store):
    seed_cache(store, CORE, D(2024, 5, 20))
    assert sync(manager, CORE).status is FetchStatus.FATAL


def test_core_symbol_empty_but_fresh_cache_degrades_instead_of_failing(manager, store):
    seed_cache(store, CORE, D(2024, 6, 4))
    outcome = sync(manager, CORE)
    assert outcome.status is FetchStatus.DEGRADED and "沿用数据库缓存" in outcome.reason


def test_fetch_exception_uses_cache_when_fresh_and_is_fatal_for_core_otherwise(manager, store):
    manager.market_fetcher.fail = RuntimeError("upstream down")
    seed_cache(store, CORE, D(2024, 6, 4))
    assert sync(manager, CORE).status is FetchStatus.DEGRADED
    assert sync(manager, "OTHER.SZ").status is FetchStatus.FATAL


# ---------------------------------------------------------------- 既有行为回归
def test_normal_success_is_ok(manager, store):
    manager.market_fetcher.set_rows(CORE, [make_row(CORE, D(2024, 6, 4), 1.0)])
    outcome = sync(manager, CORE)
    assert outcome.status is FetchStatus.OK and outcome.reason == ""
    assert [r["date"] for r in store.query_range(CORE, START, END)] == [D(2024, 6, 4)]


def test_one_trading_day_lag_is_still_ok(manager):
    manager.market_fetcher.set_rows(CORE, [make_row(CORE, D(2024, 6, 3), 1.0)])
    assert sync(manager, CORE).status is FetchStatus.OK  # 6/3 → 6/5 差 2 个交易日，仍在容忍范围


def test_fetched_but_stale_data_is_fatal_for_core_and_degraded_for_benchmark(manager):
    old = D(2024, 5, 28)
    manager.market_fetcher.set_rows(CORE, [make_row(CORE, old, 1.0)])
    manager.market_fetcher.set_rows(BENCH, [make_row(BENCH, old, 1.0)])
    manager.market_fetcher.set_rows("^HSI", [make_row("^HSI", old, 1.0)])
    assert sync(manager, CORE).status is FetchStatus.FATAL
    assert sync(manager, BENCH).status is FetchStatus.DEGRADED
    assert sync(manager, "^HSI").status is FetchStatus.DEGRADED


def test_persist_failure_is_fatal_even_for_a_benchmark(manager, monkeypatch):
    manager.market_fetcher.set_rows(BENCH, [make_row(BENCH, D(2024, 6, 4), 1.0)])
    monkeypatch.setattr(manager, "_persist", lambda *_a, **_k: False)
    assert sync(manager, BENCH).status is FetchStatus.FATAL


# ---------------------------------------------------------------- 复现线上：Yahoo 只给最近一根日线，end 不含当天
class OneBarYahooTicker:
    """复刻 2026-11 线上观察到的 000300.SS：不论 start 多早，上游只有「最近一根」日线."""

    latest_bar = D(2024, 6, 5)  # 「今天」盘中的实时日线

    def __init__(self, symbol):
        self.symbol = symbol

    def history(self, **kwargs):
        import pandas as pd

        if not kwargs["start"] <= self.latest_bar < kwargs["end"]:  # end 不含当日
            return pd.DataFrame()
        frame = pd.DataFrame(
            [{"Open": 3900.0, "High": 3950.0, "Low": 3890.0, "Close": 3920.0, "Volume": 1,
              "Dividends": 0.0, "Stock Splits": 0.0}],
            index=pd.DatetimeIndex([pd.Timestamp(self.latest_bar)], tz="Asia/Shanghai"),
        )
        return frame


def test_real_yahoo_path_intraday_empty_is_degraded_then_next_day_recovers(
    manager, store, monkeypatch
):
    from trading.market.fetchers import RangePriceFetcher

    monkeypatch.setattr("yfinance.Ticker", OneBarYahooTicker)
    manager.market_fetcher = RangePriceFetcher()
    store.upsert([make_row(BENCH, D(2024, 6, 4), 3900.0)], price_basis="yahoo_adjusted", source="yahoo_auto_adjust")

    # 盘中（结束日=当天）：上游空 → 降级沿用缓存，而不是失败
    intraday = sync(manager, BENCH, end=D(2024, 6, 5))
    assert intraday.status is FetchStatus.DEGRADED and "2024-06-04" in intraday.reason

    # 次日（结束日=下一个自然日）：同一根日线落进窗口 → 正常写入
    OneBarYahooTicker.latest_bar = D(2024, 6, 5)
    assert sync(manager, BENCH, end=D(2024, 6, 6)).status is FetchStatus.OK
    assert [r["date"] for r in store.query_range(BENCH, D(2024, 6, 5), D(2024, 6, 5))] == [D(2024, 6, 5)]


# ---------------------------------------------------------------- CLI 退出码
def run_main(monkeypatch, *extra):
    monkeypatch.setattr(
        "sys.argv", ["trading-fetcher", "--start-date", "2024-06-01", "--end-date", "2024-06-05", *extra]
    )
    data_fetcher.main()


def test_main_exits_zero_when_only_the_benchmark_is_missing(manager, store, monkeypatch):
    seed_cache(store, BENCH, D(2024, 6, 4))
    manager.market_fetcher.set_rows(CORE, [make_row(CORE, D(2024, 6, 4), 1.0)])
    monkeypatch.setattr(data_fetcher, "StockDataManager", lambda **kw: manager_with(manager, kw))
    run_main(monkeypatch)  # 不抛 SystemExit
    assert store.query_range(CORE, START, END)


def test_main_exits_one_when_a_core_symbol_has_no_data(manager, store, monkeypatch):
    seed_cache(store, BENCH, D(2024, 6, 4))
    monkeypatch.setattr(data_fetcher, "StockDataManager", lambda **kw: manager_with(manager, kw))
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch)
    assert exc.value.code == 1


def test_main_benchmark_symbols_option_overrides_default(manager, monkeypatch):
    monkeypatch.setattr(data_fetcher, "StockDataManager", lambda **kw: manager_with(manager, kw))
    with pytest.raises(SystemExit) as exc:  # 把 000300.SS 移出基准集合后，它就是核心标的
        run_main(monkeypatch, "--benchmark-symbols", "^HSI")
    assert exc.value.code == 1


def manager_with(manager, kwargs):
    """main() 里按参数构造 manager；测试里复用带 FakeFetcher 的实例，只套用基准集合."""
    if kwargs.get("benchmark_symbols") is not None:
        manager.benchmark_symbols = frozenset(kwargs["benchmark_symbols"])
    return manager
