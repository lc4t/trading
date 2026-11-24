"""批量同步只用「已完成的日线」：白天不请求/不写入当天盘中那一根，收盘 + 缓冲之后才含当天（按标的所属市场）.

时间一律用北京时间写（conftest.cn_time）。周末按周六/周日判断；节假日没有交易日历，不单独识别。
"""
import datetime as dt
from types import SimpleNamespace

import pytest

from trading.data import fetcher as data_fetcher
from trading.data.fetcher import FetchStatus
from trading.market.clock import completed_session_end
from trading.market.fetchers import FetchResult

from .conftest import FakeFetcher, cn_time, make_row

D = dt.date
CN = "159915.SZ"
HK = "0700.HK"
HSI = "^HSI"
WED = D(2024, 6, 5)
TUE = D(2024, 6, 4)


# ------------------------------------------------------------------ 纯函数：谁在什么时间取到哪一天
@pytest.mark.parametrize(
    "symbol, when, expected",
    [
        # A 股：收盘 15:00 + 30 分钟缓冲 = 15:30
        (CN, (2024, 6, 5, 0, 0), TUE),     # 0 点：与前一晚一致
        (CN, (2024, 6, 4, 23, 59), TUE),   # 前一晚 23:59（周二已收盘）
        (CN, (2024, 6, 5, 9, 30), TUE),    # 开盘
        (CN, (2024, 6, 5, 14, 59), TUE),   # 收盘前
        (CN, (2024, 6, 5, 15, 0), TUE),    # 刚收盘，缓冲内
        (CN, (2024, 6, 5, 15, 29), TUE),
        (CN, (2024, 6, 5, 15, 30), WED),   # 缓冲结束 → 含当天
        (CN, (2024, 6, 5, 23, 59), WED),
        # 港股：收盘 16:10 + 30 分钟 = 16:40，晚于 A 股
        (HK, (2024, 6, 5, 15, 35), TUE),
        (HK, (2024, 6, 5, 16, 0), TUE),
        (HK, (2024, 6, 5, 16, 39), TUE),
        (HK, (2024, 6, 5, 16, 40), WED),
        (HSI, (2024, 6, 5, 16, 0), TUE),   # 恒指跟港股
        (HSI, (2024, 6, 5, 16, 40), WED),
        ("000300.SS", (2024, 6, 5, 10, 0), TUE),   # 沪深300 跟 A 股
        # 周末 / 周一早晨 → 最近的周五
        (CN, (2024, 6, 8, 10, 0), D(2024, 6, 7)),
        (CN, (2024, 6, 9, 23, 0), D(2024, 6, 7)),
        (CN, (2024, 6, 10, 9, 0), D(2024, 6, 7)),
        (CN, (2024, 6, 10, 15, 30), D(2024, 6, 10)),
        # 美股按纽约时间：北京 6/5 10:00 = 纽约 6/4 22:00，周二已收盘
        ("AAPL", (2024, 6, 5, 10, 0), TUE),
    ],
)
def test_completed_session_end(symbol, when, expected):
    assert completed_session_end(symbol, D(2024, 12, 31), cn_time(*when)) == expected


def test_requested_end_earlier_than_latest_completed_wins():
    assert completed_session_end(CN, D(2024, 6, 3), cn_time(2024, 6, 5, 20, 0)) == D(2024, 6, 3)


# ------------------------------------------------------------------ 同步：请求的结束日与落库
@pytest.fixture
def db_client(store):
    return SimpleNamespace(engine=store.engine, Session=store.Session)


@pytest.fixture
def manager(monkeypatch, db_client):
    monkeypatch.setattr(data_fetcher, "DBClient", lambda: db_client)
    mgr = data_fetcher.StockDataManager()
    mgr.market_fetcher = FakeFetcher()
    return mgr


def at(mgr, *when):
    mgr._now = lambda: cn_time(*when)


def sync(mgr, symbol, start=D(2024, 6, 1), end=WED):
    return mgr.sync_symbol(symbol, start_date=start, end_date=end, db_client=mgr.db_client)


def rows(symbol, *days):
    return [make_row(symbol, d, 1.0 + i) for i, d in enumerate(days)]


def test_daytime_does_not_request_or_write_today(manager, store):
    manager.market_fetcher.set_rows(CN, rows(CN, TUE, WED))  # 上游已带当天盘中那一根
    at(manager, 2024, 6, 5, 10, 0)
    assert sync(manager, CN).status is FetchStatus.OK
    assert manager.market_fetcher.calls[0][2] == TUE  # 请求的结束日 = 上一个已完成交易日
    assert [r["date"] for r in store.query_range(CN, D(2024, 6, 1), D(2024, 6, 30))] == [TUE]


def test_upstream_that_ignores_end_is_trimmed_to_completed_bars(manager, store):
    class FullHistory(FakeFetcher):  # 同花顺 ETF 口径：不看区间，全量历史（可能带当天盘中）
        def fetch(self, symbol, start, end, *, mode="raw", retry=None):
            self.calls.append((symbol, start, end, mode))
            return FetchResult([dict(r) for r in self.data[symbol]], "ths", "ths_qfq", full_history=True)

    manager.market_fetcher = FullHistory()
    manager.market_fetcher.set_rows(CN, rows(CN, TUE, WED))
    at(manager, 2024, 6, 5, 10, 0)
    assert sync(manager, CN).status is FetchStatus.OK
    assert [r["date"] for r in store.query_range(CN, D(2024, 6, 1), D(2024, 6, 30))] == [TUE]


def test_after_close_includes_today_and_marks_it_final(manager, store):
    manager.market_fetcher.set_rows(CN, rows(CN, TUE, WED))
    at(manager, 2024, 6, 5, 15, 30)
    assert sync(manager, CN).status is FetchStatus.OK
    assert manager.market_fetcher.calls[0][2] == WED
    stored = store.query_range(CN, D(2024, 6, 1), D(2024, 6, 30))
    assert [r["date"] for r in stored] == [TUE, WED] and all(r["meta"]["is_final"] for r in stored)


def test_cn_and_hk_use_their_own_close_time_at_the_same_instant(manager):
    manager.market_fetcher.set_rows(CN, rows(CN, TUE, WED))
    manager.market_fetcher.set_rows(HK, rows(HK, TUE, WED))
    at(manager, 2024, 6, 5, 15, 35)  # A 股已可定稿，港股还没到 16:40
    sync(manager, CN)
    sync(manager, HK)
    ends = {c[0]: c[2] for c in manager.market_fetcher.calls}
    assert ends == {CN: WED, HK: TUE}


def test_weekend_falls_back_to_friday(manager, store):
    manager.market_fetcher.set_rows(CN, rows(CN, D(2024, 6, 6), D(2024, 6, 7)))
    at(manager, 2024, 6, 8, 11, 0)  # 周六
    assert sync(manager, CN, end=D(2024, 6, 8)).status is FetchStatus.OK
    assert manager.market_fetcher.calls[0][2] == D(2024, 6, 7)
    assert store.query_range(CN, D(2024, 6, 1), D(2024, 6, 30))[-1]["date"] == D(2024, 6, 7)


def test_nothing_to_sync_when_start_is_after_the_last_completed_day(manager):
    at(manager, 2024, 6, 5, 10, 0)
    outcome = sync(manager, CN, start=WED)  # 起点是今天，今天还没收盘
    assert outcome.status is FetchStatus.OK and manager.market_fetcher.calls == []


def test_midnight_and_daytime_runs_see_the_same_window(manager):
    manager.market_fetcher.set_rows(CN, rows(CN, TUE))
    for when in [(2024, 6, 5, 0, 5), (2024, 6, 5, 10, 0), (2024, 6, 5, 15, 0)]:
        manager.market_fetcher.calls.clear()
        at(manager, *when)
        sync(manager, CN)
        assert manager.market_fetcher.calls[0][2] == TUE


def test_partial_cache_row_for_today_is_not_used_as_fallback_cache(manager, store):
    store.upsert([make_row("000300.SS", D(2024, 6, 3), 3900.0)], price_basis="raw")
    store.upsert([make_row("000300.SS", WED, 3999.0)], price_basis="raw", final_flags={WED: False})  # 盘中半根
    at(manager, 2024, 6, 5, 10, 0)
    outcome = sync(manager, "000300.SS")  # 上游空
    assert outcome.status is FetchStatus.DEGRADED and "2024-06-03" in outcome.reason


# ------------------------------------------------------------------ 库里的盘中半根：收盘后被完整日线覆盖，已定稿的不会被回退
def test_intraday_row_is_overwritten_by_the_complete_bar_after_close(manager, store):
    store.upsert([make_row(CN, WED, 9.99)], price_basis="raw", final_flags={WED: False})  # 白天写入的半根
    manager.market_fetcher.set_rows(CN, [make_row(CN, WED, 2.5)])
    at(manager, 2024, 6, 5, 16, 0)
    assert sync(manager, CN).status is FetchStatus.OK
    row = store.query_range(CN, WED, WED)[0]
    assert row["close_price"] == 2.5 and row["meta"]["is_final"] is True


def test_final_row_is_not_downgraded_by_a_later_partial_write(store):
    store.upsert([make_row(CN, WED, 2.5)], price_basis="raw", final_flags={WED: True})
    written = store.upsert([make_row(CN, WED, 9.99)], price_basis="raw", final_flags={WED: False})
    assert written == 0
    row = store.query_range(CN, WED, WED)[0]
    assert row["close_price"] == 2.5 and row["meta"]["is_final"] is True
    # 另一天的盘中行不受影响；定稿覆盖定稿照常
    assert store.upsert([make_row(CN, TUE, 1.0)], price_basis="raw", final_flags={TUE: False}) == 1
    assert store.upsert([make_row(CN, WED, 2.6)], price_basis="raw", final_flags={WED: True}) == 1
    assert store.query_range(CN, WED, WED)[0]["close_price"] == 2.6


def test_backtest_reads_skip_the_in_progress_session_bar(store, monkeypatch):
    monkeypatch.setattr("trading.market.store.utcnow", lambda: cn_time(2024, 6, 5, 10, 0))
    store.upsert([make_row(CN, TUE, 1.0)], price_basis="raw", final_flags={TUE: True})
    store.upsert([make_row(CN, WED, 9.0)], price_basis="raw", final_flags={WED: False})  # 盘中半根
    frame = store.ohlcv_frame(CN, D(2024, 6, 1), D(2024, 6, 30))
    assert [d.date() for d in frame["date"]] == [TUE]
    assert [d for d, _ in store.close_series(CN, D(2024, 6, 1), D(2024, 6, 30))] == [TUE]

    # 收盘 + 缓冲后，该日不再「等收盘」：读取口径回到原样（行由收盘后的同步覆盖为定稿）
    monkeypatch.setattr("trading.market.store.utcnow", lambda: cn_time(2024, 6, 5, 16, 0))
    assert len(store.ohlcv_frame(CN, D(2024, 6, 1), D(2024, 6, 30))) == 2
