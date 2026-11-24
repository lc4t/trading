"""读库路径统一「只看已完成日线」：不依赖 is_final 标记，日期晚于应有最后完成日的行一律忽略.

背景：159665.SZ 的当天（11-24 10:30）盘中行被读进了回测 / 日报的 dataEnd。库里的这一行没有
``is_final=0`` 标记（旧代码写入 / 元数据缺失 / 遗留行），旧保护只跳过「明确标记非终值」的行，
而 trading-backtest 的结束日和 JSON 的 dateRange.end 还直接走无保护的原生 SQL。
"""
import datetime as dt

import pytest
from sqlalchemy import text

from trading.cli import backtest as backtest_cli
from trading.data.repository import DBClient
from trading.market.clock import latest_completed_date

from .conftest import cn_time, make_row

D = dt.date
CN = "159665.SZ"
OTHER_CN = "159915.SZ"
HK = "0700.HK"
MON = D(2024, 6, 3)
TUE = D(2024, 6, 4)
WED = D(2024, 6, 5)
RANGE = (D(2024, 6, 1), D(2024, 6, 30))


@pytest.fixture
def at(monkeypatch):
    """把读库路径用到的时钟设成北京时间的某一刻."""

    def _set(*args):
        moment = cn_time(*args)
        monkeypatch.setattr("trading.market.store.utcnow", lambda: moment)
        monkeypatch.setattr("trading.data.repository.utcnow", lambda: moment)

    return _set


@pytest.fixture
def db_client(store):
    """只带 Session 的 DBClient（不读环境变量、不连 MySQL）."""
    client = DBClient.__new__(DBClient)
    client.Session = store.Session
    client.engine = store.engine
    return client


def seed_unmarked_intraday(store, symbol, day, close=9.0):
    """写一行「没有元数据」的当天盘中行：模拟旧代码 / 元数据缺失时写入的行."""
    store.upsert([make_row(symbol, day, close)])  # price_basis=None → 不写旁表
    assert store.query_range(symbol, day, day)[0]["meta"] is None


# ------------------------------------------------------------------ 纯函数
@pytest.mark.parametrize(
    "symbol, when, expected",
    [
        (CN, (2024, 6, 5, 10, 30), TUE),
        (CN, (2024, 6, 5, 15, 30), WED),
        (HK, (2024, 6, 5, 16, 0), TUE),
        (HK, (2024, 6, 5, 16, 40), WED),
        (CN, (2024, 6, 8, 10, 0), D(2024, 6, 7)),  # 周六 → 周五
    ],
)
def test_latest_completed_date(symbol, when, expected):
    assert latest_completed_date(symbol, cn_time(*when)) == expected


# ------------------------------------------------------------------ 白天回测不使用无标记的盘中行
def test_daytime_reads_ignore_an_unmarked_intraday_row(store, at):
    at(2024, 6, 5, 10, 30)
    store.upsert([make_row(CN, TUE, 1.0)], price_basis="raw", final_flags={TUE: True})
    seed_unmarked_intraday(store, CN, WED)

    frame = store.ohlcv_frame(CN, *RANGE)
    assert [d.date() for d in frame["date"]] == [TUE]
    assert [d for d, _ in store.close_series(CN, *RANGE)] == [TUE]


def test_daytime_reads_ignore_a_row_marked_final_but_dated_after_the_last_completed_day(store, at):
    """标记不可信时（例如被误标成定稿）也以日期为准."""
    at(2024, 6, 5, 10, 30)
    store.upsert([make_row(CN, TUE, 1.0)], price_basis="raw", final_flags={TUE: True})
    store.upsert([make_row(CN, WED, 9.0)], price_basis="raw", final_flags={WED: True})

    assert [d.date() for d in store.ohlcv_frame(CN, *RANGE)["date"]] == [TUE]


def test_future_dated_rows_are_ignored_too(store, at):
    at(2024, 6, 5, 10, 30)
    store.upsert([make_row(CN, TUE, 1.0), make_row(CN, D(2024, 6, 20), 7.0)])
    assert [d for d, _ in store.close_series(CN, *RANGE)] == [TUE]


def test_after_close_the_complete_bar_is_used(store, at):
    store.upsert([make_row(CN, TUE, 1.0)], price_basis="raw", final_flags={TUE: True})
    store.upsert([make_row(CN, WED, 2.0)], price_basis="raw", final_flags={WED: True})
    at(2024, 6, 5, 15, 29)
    assert len(store.ohlcv_frame(CN, *RANGE)) == 1  # 缓冲内仍不算
    at(2024, 6, 5, 15, 31)
    frame = store.ohlcv_frame(CN, *RANGE)
    assert [d.date() for d in frame["date"]] == [TUE, WED]
    assert [c for _, c in store.close_series(CN, *RANGE)] == [1.0, 2.0]


def test_midnight_and_daytime_reads_return_the_same_series(store, at):
    store.upsert([make_row(CN, MON, 1.0), make_row(CN, TUE, 1.1)], price_basis="raw",
                 final_flags={MON: True, TUE: True})
    seed_unmarked_intraday(store, CN, WED)
    at(2024, 6, 5, 0, 5)
    midnight = store.close_series(CN, *RANGE)
    at(2024, 6, 5, 14, 0)
    assert store.close_series(CN, *RANGE) == midnight


def test_symbols_are_guarded_by_their_own_market_close(store, at):
    """同一时刻（北京 16:00）：A 股已收盘用当天，港股还没到 16:40 不用当天."""
    for symbol in (OTHER_CN, HK):
        store.upsert([make_row(symbol, TUE, 1.0), make_row(symbol, WED, 2.0)])
    at(2024, 6, 5, 16, 0)
    assert [d for d, _ in store.close_series(OTHER_CN, *RANGE)] == [TUE, WED]
    assert [d for d, _ in store.close_series(HK, *RANGE)] == [TUE]


def test_other_symbols_and_history_are_unaffected(store, at):
    at(2024, 6, 5, 10, 30)
    store.upsert([make_row(OTHER_CN, MON, 1.0), make_row(OTHER_CN, TUE, 1.1)],
                 price_basis="raw", final_flags={MON: True, TUE: True})
    frame = store.ohlcv_frame(OTHER_CN, *RANGE)
    assert [d.date() for d in frame["date"]] == [MON, TUE]
    # 请求结束日早于应有最后完成日时按请求来
    assert [d for d, _ in store.close_series(OTHER_CN, D(2024, 6, 1), MON)] == [MON]


# ------------------------------------------------------------------ 原生 SQL 的「最新一行」（回测结束日 / JSON dataEnd）
def test_query_latest_by_symbol_ignores_the_in_progress_bar(store, db_client, at):
    at(2024, 6, 5, 10, 30)
    store.upsert([make_row(CN, TUE, 1.0)], price_basis="raw", final_flags={TUE: True})
    seed_unmarked_intraday(store, CN, WED)

    latest = db_client.query_latest_by_symbol(CN)
    assert latest["date"] == TUE and latest["close_price"] == 1.0


def test_query_latest_by_symbol_uses_the_complete_bar_after_close(store, db_client, at):
    store.upsert([make_row(CN, TUE, 1.0), make_row(CN, WED, 2.0)])
    at(2024, 6, 5, 16, 0)
    assert db_client.query_latest_by_symbol(CN)["date"] == WED


def test_query_latest_by_symbol_without_any_completed_row_is_none(store, db_client, at):
    at(2024, 6, 5, 10, 30)
    seed_unmarked_intraday(store, CN, WED)
    assert db_client.query_latest_by_symbol(CN) is None


def test_backtest_cli_end_date_is_the_last_completed_day(store, db_client, at, monkeypatch):
    """trading-backtest 用库里最新日期当结束日：白天不能变成当天."""
    at(2024, 6, 5, 10, 30)
    store.upsert([make_row(CN, TUE, 1.0)])
    seed_unmarked_intraday(store, CN, WED)
    captured = {}

    monkeypatch.setattr(backtest_cli, "DBClient", lambda: db_client)
    monkeypatch.setattr(
        backtest_cli, "_run_single",
        lambda args, client: captured.setdefault("end_date", args.end_date) or {},
    )
    monkeypatch.setattr("sys.argv", ["trading-backtest", CN, "--start-date", "2024-01-01"])
    backtest_cli.main()
    assert captured["end_date"] == "2024-06-04"


def test_guard_does_not_touch_stored_rows(store, at):
    """只在读取时忽略，不改库."""
    at(2024, 6, 5, 10, 30)
    seed_unmarked_intraday(store, CN, WED)
    store.close_series(CN, *RANGE)
    with store.engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM trading_data WHERE symbol = :s"), {"s": CN}).scalar() == 1
