"""tt-fetcher 同步 / 回测取数经过统一数据层后的行为（SQLite 内存库）。"""
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from trading.data import fetcher as data_fetcher
from trading.data.repository import TradingData
from trading.engine.single import fetch_benchmark_daily_returns, fetch_symbol_data
from trading.market.models import MarketDataMeta
from trading.market.store import MarketStore

from .conftest import FakeFetcher, make_row

D = dt.date
SYM = "159915.SZ"


@pytest.fixture
def db_client(store):
    return SimpleNamespace(engine=store.engine, Session=store.Session)


@pytest.fixture
def manager(monkeypatch, db_client):
    monkeypatch.setattr(data_fetcher, "DBClient", lambda: db_client)
    mgr = data_fetcher.StockDataManager()
    mgr.market_fetcher = FakeFetcher()
    return mgr


def insert_legacy(store, day, close, update_time=dt.datetime(2024, 6, 5, 0, 0)):
    with store.Session() as s:
        d = Decimal(str(close))
        s.add(TradingData(symbol=SYM, date=day, open_price=d, close_price=d, high=d, low=d,
                          volume=1, create_time=update_time, update_time=update_time))
        s.commit()


def test_sync_rewrites_legacy_history_like_before_and_tags_basis(manager, store):
    for day, close in [(D(2024, 6, 3), 1.0), (D(2024, 6, 4), 1.1)]:
        insert_legacy(store, day, close)
    manager.market_fetcher.set_rows(
        SYM, [make_row(SYM, D(2024, 6, 3), 0.95), make_row(SYM, D(2024, 6, 4), 1.05)]
    )
    ok = manager.fetch_data(SYM, start_date=D(2024, 6, 3), end_date=D(2024, 6, 4), db_client=manager.db_client)
    assert ok is True
    # 遗留序列：沿用 legacy 口径抓取，值被覆盖（与历史 upsert 行为一致）
    assert manager.market_fetcher.calls[0][3] == "legacy"
    rows = store.query_range(SYM, D(2024, 6, 1), D(2024, 6, 30))
    assert [r["close_price"] for r in rows] == [0.95, 1.05]
    assert {r["meta"]["price_basis"] for r in rows} == {"ths_qfq"}
    assert all(r["meta"]["is_final"] for r in rows)  # 6/3、6/4 早已收盘


def test_sync_new_symbol_is_raw_and_quantized_to_four_decimals(manager, store):
    manager.market_fetcher.set_rows("NEW.SZ", [make_row("NEW.SZ", D(2024, 6, 3), 1.123456)])
    assert manager.fetch_data("NEW.SZ", start_date=D(2024, 6, 3), end_date=D(2024, 6, 3),
                              db_client=manager.db_client) is True
    row = store.query_range("NEW.SZ", D(2024, 6, 3), D(2024, 6, 3))[0]
    assert row["close_price"] == 1.1235 and row["meta"]["price_basis"] == "raw"


def test_sync_skips_nan_rows_but_keeps_the_rest(manager, store):
    rows = [make_row(SYM, D(2024, 6, 3), 1.0), make_row(SYM, D(2024, 6, 4), float("nan"))]
    manager.market_fetcher.set_rows(SYM, rows)
    assert manager.fetch_data(SYM, start_date=D(2024, 6, 3), end_date=D(2024, 6, 4),
                              db_client=manager.db_client) is True
    assert [r["date"] for r in store.query_range(SYM, D(2024, 6, 1), D(2024, 6, 30))] == [D(2024, 6, 3)]


def test_sync_degrades_to_trading_data_only_when_meta_table_cannot_be_created(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    TradingData.__table__.create(engine)  # 只有主表，且建旁表会失败
    store = MarketStore(engine)

    class Boom:
        def create(self, *_a, **_k):
            raise SQLAlchemyError("no DDL privilege")

    monkeypatch.setattr("trading.market.store.META_TABLES", (Boom(),))
    assert store.ensure_meta_tables() is False
    written = store.upsert([make_row(SYM, D(2024, 6, 3), 1.0)], price_basis="ths_qfq")
    assert written == 1 and store.meta_enabled is False
    row = store.query_range(SYM, D(2024, 6, 3), D(2024, 6, 3))[0]
    assert row["close_price"] == 1.0 and row["meta"] is None
    assert store.series_mode(SYM) == "legacy"


def test_upsert_updates_in_place_and_leaves_single_row(store):
    store.upsert([make_row(SYM, D(2024, 6, 3), 1.0)], price_basis="raw")
    store.upsert([make_row(SYM, D(2024, 6, 3), 2.0)], price_basis="raw")
    with store.Session() as s:
        assert s.query(TradingData).count() == 1
        assert s.query(MarketDataMeta).count() == 1
    assert store.query_range(SYM, D(2024, 6, 3), D(2024, 6, 3))[0]["close_price"] == 2.0


# ----------------------------------------------------- 回测取数与旧实现逐位一致
def reference_fetch_symbol_data(engine, symbol, start, end):
    """2.0 之前 trading.engine.single.fetch_symbol_data 的原实现（用来做回归对照）。"""
    from sqlalchemy.orm import sessionmaker

    with sessionmaker(bind=engine)() as session:
        df = pd.read_sql(
            session.query(TradingData)
            .filter(TradingData.symbol == symbol, TradingData.date >= start, TradingData.date <= end)
            .statement,
            session.bind,
        )
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    for col in ("open_price", "close_price", "high", "low"):
        df[col] = df[col].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df


def test_backtest_read_path_is_identical_to_previous_implementation(store, db_client):
    closes = [1.0, 1.02, 1.01, 1.05, 1.04]
    store.upsert([make_row(SYM, D(2024, 6, 3) + dt.timedelta(days=i), c) for i, c in enumerate(closes)],
                 price_basis="ths_qfq")
    start, end = dt.datetime(2024, 6, 1), dt.datetime(2024, 6, 30)
    new = fetch_symbol_data(db_client, SYM, start, end)
    old = reference_fetch_symbol_data(store.engine, SYM, start, end)
    pd.testing.assert_frame_equal(new, old)
    assert fetch_symbol_data(db_client, "NOPE.SZ", start, end).empty


def test_benchmark_daily_returns(store, db_client):
    store.upsert([make_row("000300.SS", D(2024, 6, 3) + dt.timedelta(days=i), c)
                  for i, c in enumerate([100.0, 101.0, 99.99])], price_basis="yahoo_adjusted")
    returns = fetch_benchmark_daily_returns(db_client, "000300.SS", dt.datetime(2024, 6, 1), dt.datetime(2024, 6, 30))
    assert returns[D(2024, 6, 4)] == pytest.approx(0.01)
    assert returns[D(2024, 6, 5)] == pytest.approx(99.99 / 101.0 - 1)
    assert fetch_benchmark_daily_returns(db_client, "MISSING", dt.datetime(2024, 6, 1), dt.datetime(2024, 6, 30)) == {}


def test_single_backtest_runs_end_to_end_on_the_unified_store(store, db_client):
    import numpy as np

    from trading.engine.single import SingleBacktestEngine

    rng = np.random.default_rng(7)
    price, rows = 10.0, []
    day = D(2023, 1, 2)
    while len(rows) < 160:
        if day.weekday() < 5:
            price *= 1 + rng.normal(0.0004, 0.01)
            rows.append(make_row("FAKE.SZ", day, round(price, 4)))
        day += dt.timedelta(days=1)
    store.upsert(rows, price_basis="ths_qfq")
    result = SingleBacktestEngine(db_client).run(
        symbol="FAKE.SZ",
        start_date=dt.datetime(2023, 1, 1),
        end_date=dt.datetime(2023, 12, 31),
        params={"initial_capital": 50000, "commission_rate": 0.0001, "use_ma": True,
                "short_period": 5, "long_period": 20, "use_chandelier": False, "use_adr": False,
                "trade_start_time": None, "trade_end_time": None, "benchmark": None, "risk_free_rate": 0.03},
    )
    assert result["initial_capital"] == 50000 and "metrics" in result and "next_signal" in result


# ------------------------------------------------------------- 数据库瞬时故障重试
def test_transient_db_errors_are_retried_but_others_are_not(store, monkeypatch):
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr("time.sleep", lambda *_: None)
    store.upsert([make_row(SYM, D(2024, 6, 3), 1.0)], price_basis="raw")
    real = store.ReadSession
    state = {"fail": 2}

    def flaky(*a, **k):
        if state["fail"]:
            state["fail"] -= 1
            raise OperationalError("SELECT", {}, Exception(1213, "Deadlock found"))
        return real(*a, **k)

    monkeypatch.setattr(store, "ReadSession", flaky)
    assert store.query_range(SYM, D(2024, 6, 1), D(2024, 6, 30))[0]["close_price"] == 1.0  # 第 3 次成功

    state["fail"] = 5
    with pytest.raises(OperationalError):
        store.query_range(SYM, D(2024, 6, 1), D(2024, 6, 30))  # 超过重试次数照常抛出

    def broken(*a, **k):
        raise OperationalError("SELECT", {}, Exception(1064, "syntax error"))

    monkeypatch.setattr(store, "ReadSession", broken)
    calls = []
    monkeypatch.setattr("time.sleep", lambda s: calls.append(s))
    with pytest.raises(OperationalError):
        store.query_range(SYM, D(2024, 6, 1), D(2024, 6, 30))
    assert calls == []  # 非瞬时错误不重试


def test_pool_timeout_is_treated_as_transient(store, monkeypatch):
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    monkeypatch.setattr("time.sleep", lambda *_: None)
    store.upsert([make_row(SYM, D(2024, 6, 3), 1.0)], price_basis="raw")
    real, state = store.ReadSession, {"fail": 1}

    def exhausted(*a, **k):
        if state["fail"]:
            state["fail"] -= 1
            raise PoolTimeout("QueuePool limit of size 10 overflow 20 reached")
        return real(*a, **k)

    monkeypatch.setattr(store, "ReadSession", exhausted)
    assert store.query_range(SYM, D(2024, 6, 1), D(2024, 6, 30))[0]["close_price"] == 1.0
