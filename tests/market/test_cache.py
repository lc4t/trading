import datetime as dt

import pytest

from trading.market.cache import RequestError, apply_dividend_adjustment, coverage_info
from trading.market.clock import to_iso
from trading.market.errors import FetchError
from trading.market.models import MarketDataMeta

from .conftest import cn_time, make_row

SYM = "159915.SZ"
D = dt.date


def seed_upstream(fetcher, closes):
    fetcher.set_rows(SYM, [make_row(SYM, d, c) for d, c in closes.items()])


# ----------------------------------------------------------------- 已定稿历史
def test_finalized_history_is_never_refetched(cache, fetcher, clock):
    clock.now = cn_time(2024, 6, 6, 9, 0)  # 次日早晨，6/3~6/5 都已收盘
    seed_upstream(fetcher, {D(2024, 6, 3): 1.0, D(2024, 6, 4): 1.1, D(2024, 6, 5): 1.2})

    first = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert first["count"] == 3 and all(r["is_final"] for r in first["data"])
    assert len(fetcher.calls) == 1

    # 上游悄悄改了历史价：已定稿的缓存不应受影响，也不应回源
    seed_upstream(fetcher, {D(2024, 6, 3): 9.0, D(2024, 6, 4): 9.0, D(2024, 6, 5): 9.0})
    clock.advance(hours=3)
    again = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert len(fetcher.calls) == 1
    assert [r["close_price"] for r in again["data"]] == [1.0, 1.1, 1.2]
    assert again["refreshed"] is False


def test_head_gap_is_reported_not_silently_filled(cache, fetcher, clock):
    clock.now = cn_time(2024, 6, 6, 9, 0)
    seed_upstream(fetcher, {D(2024, 6, 3): 1.0, D(2024, 6, 4): 1.1})
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 4))
    calls = len(fetcher.calls)

    payload = cache.get_range(SYM, D(2024, 1, 2), D(2024, 6, 4))
    assert len(fetcher.calls) == calls  # 没有静默补抓
    assert payload["coverage"]["complete"] is False
    assert payload["coverage"]["gaps"][0]["kind"] == "head"
    assert payload["coverage"]["first_date"] == "2024-06-03"


def test_coverage_info_internal_and_tail_gaps():
    info = coverage_info([D(2024, 1, 2), D(2024, 3, 1)], D(2024, 1, 1), D(2024, 5, 1))
    kinds = [g["kind"] for g in info["gaps"]]
    assert kinds == ["internal", "tail"]
    assert coverage_info([], D(2024, 1, 1), D(2024, 1, 5))["gaps"][0]["kind"] == "all"
    # 长假不算缺口
    assert coverage_info([D(2024, 9, 30), D(2024, 10, 8)], D(2024, 9, 30), D(2024, 10, 8))["complete"]


# ------------------------------------------------------- 盘中行 / as_of / is_final
def test_intraday_row_is_marked_nonfinal_with_snapshot_as_of(cache, fetcher, clock):
    seed_upstream(fetcher, {D(2024, 6, 4): 1.1, D(2024, 6, 5): 1.15})
    payload = cache.get_range(SYM, D(2024, 6, 4), D(2024, 6, 5))
    done, today = payload["data"]
    assert done["is_final"] is True and today["is_final"] is False
    assert today["as_of"] == clock.now  # 盘中：数据时点 = 抓取时刻
    assert today["fetched_at"] == clock.now
    assert today["source"] == "fake_upstream" and today["price_basis"] == "raw"
    # 定稿行的数据时点是当日收盘 15:00（北京时间）
    assert to_iso(done["as_of"]) == "2024-06-04T15:00:00+08:00"
    assert payload["nonfinal_count"] == 1


def test_intraday_row_corrected_next_day_even_when_end_equals_cached_latest(cache, fetcher, clock):
    """回归：盘中抓到的当日行，次日请求 end=昨天时必须被修正成收盘价."""
    seed_upstream(fetcher, {D(2024, 6, 4): 1.1, D(2024, 6, 5): 1.15})  # 15 = 盘中价
    cache.get_range(SYM, D(2024, 6, 4), D(2024, 6, 5))
    assert len(fetcher.calls) == 1

    clock.now = cn_time(2024, 6, 6, 9, 0)  # 次日
    seed_upstream(fetcher, {D(2024, 6, 4): 1.1, D(2024, 6, 5): 1.20})  # 收盘价
    payload = cache.get_range(SYM, D(2024, 6, 4), D(2024, 6, 5))  # end == 缓存最新日

    assert len(fetcher.calls) == 2
    row = payload["data"][-1]
    assert row["close_price"] == 1.20 and row["is_final"] is True
    assert to_iso(row["as_of"]) == "2024-06-05T15:00:00+08:00"
    assert payload["nonfinal_count"] == 0

    # 修正后不再回源
    cache.get_range(SYM, D(2024, 6, 4), D(2024, 6, 5))
    assert len(fetcher.calls) == 2


def test_intraday_row_respects_ttl_while_market_is_open(cache, fetcher, clock):
    seed_upstream(fetcher, {D(2024, 6, 5): 1.0})
    cache.get_range(SYM, D(2024, 6, 5), D(2024, 6, 5))
    clock.advance(seconds=120)
    cache.get_range(SYM, D(2024, 6, 5), D(2024, 6, 5))
    assert len(fetcher.calls) == 1  # TTL 内不重抓
    clock.advance(seconds=300)
    cache.get_range(SYM, D(2024, 6, 5), D(2024, 6, 5))
    assert len(fetcher.calls) == 2  # 超过 TTL 重抓


def test_fetch_just_after_close_inside_buffer_is_not_final(cache, fetcher, clock):
    clock.now = cn_time(2024, 6, 5, 15, 10)  # 收盘后 10 分钟，缓冲期内
    seed_upstream(fetcher, {D(2024, 6, 5): 1.0})
    payload = cache.get_range(SYM, D(2024, 6, 5), D(2024, 6, 5))
    assert payload["data"][0]["is_final"] is False
    clock.now = cn_time(2024, 6, 5, 15, 40)
    cache.get_range(SYM, D(2024, 6, 5), D(2024, 6, 5))
    assert len(fetcher.calls) == 2  # 过了缓冲期必修复
    assert cache.get_range(SYM, D(2024, 6, 5), D(2024, 6, 5))["data"][0]["is_final"] is True


def test_refresh_never_does_not_fetch_even_for_nonfinal(cache, fetcher, clock):
    seed_upstream(fetcher, {D(2024, 6, 5): 1.0})
    cache.get_range(SYM, D(2024, 6, 5), D(2024, 6, 5))
    clock.now = cn_time(2024, 6, 7, 9, 0)
    payload = cache.get_range(SYM, D(2024, 6, 5), D(2024, 6, 5), refresh="never")
    assert len(fetcher.calls) == 1
    assert payload["nonfinal_count"] == 1  # 如实暴露，而不是装作定稿


def test_refresh_always_refetches_window(cache, fetcher, clock):
    clock.now = cn_time(2024, 6, 6, 9, 0)
    seed_upstream(fetcher, {D(2024, 6, 4): 1.0})
    cache.get_range(SYM, D(2024, 6, 4), D(2024, 6, 4))
    seed_upstream(fetcher, {D(2024, 6, 4): 2.0})
    payload = cache.get_range(SYM, D(2024, 6, 4), D(2024, 6, 4), refresh="always")
    assert payload["refreshed"] is True and payload["data"][0]["close_price"] == 2.0


def test_latest_repairs_nonfinal_row(cache, fetcher, clock):
    seed_upstream(fetcher, {D(2024, 6, 5): 1.0})
    first = cache.get_latest(SYM)
    assert first["data"]["is_final"] is False
    clock.now = cn_time(2024, 6, 6, 8, 0)
    seed_upstream(fetcher, {D(2024, 6, 5): 1.3})
    again = cache.get_latest(SYM)
    assert again["data"]["close_price"] == 1.3 and again["data"]["is_final"] is True


# ------------------------------------------------------------ 上游失败 / 限流
def test_tail_failure_returns_cache_with_error_and_backs_off(cache, fetcher, clock):
    clock.now = cn_time(2024, 6, 6, 9, 0)
    seed_upstream(fetcher, {D(2024, 6, 3): 1.0})
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 3))

    fetcher.fail = FetchError("Yahoo Finance range request failed (YFRateLimitError)")
    clock.advance(seconds=700)  # 超过尾部检查 TTL
    payload = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert payload["count"] == 1  # 仍返回缓存
    assert "YFRateLimitError" in payload["tail_refresh_error"]
    failed_calls = len(fetcher.calls)

    clock.advance(seconds=10)  # 静默期内不再打上游，但仍报告错误
    again = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert len(fetcher.calls) == failed_calls
    assert again["tail_refresh_error"]

    fetcher.fail = None
    seed_upstream(fetcher, {D(2024, 6, 3): 1.0, D(2024, 6, 4): 1.1})
    clock.advance(seconds=700)  # 静默期与尾部检查间隔都过了
    recovered = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert recovered["tail_refresh_error"] is None and recovered["count"] == 2


def test_empty_cache_fetch_failure_raises(cache, fetcher):
    fetcher.fail = FetchError("boom")
    with pytest.raises(FetchError):
        cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))


# ------------------------------------------------------------- 遗留行（无旁表）
def insert_legacy(store, symbol, day, close, update_time):
    from decimal import Decimal

    from trading.data.repository import TradingData

    with store.Session() as s:
        s.add(
            TradingData(
                symbol=symbol, date=day, open_price=Decimal(str(close)), close_price=Decimal(str(close)),
                high=Decimal(str(close)), low=Decimal(str(close)), volume=1,
                create_time=update_time, update_time=update_time,
            )
        )
        s.commit()


def test_legacy_rows_are_labelled_and_final_ones_not_refetched(cache, store, fetcher, clock):
    clock.now = cn_time(2024, 6, 6, 9, 0)
    # update_time 是业务时区的 naive 时间：6/4 收盘后同步 → 定稿
    insert_legacy(store, SYM, D(2024, 6, 4), 1.0, dt.datetime(2024, 6, 5, 0, 0))
    payload = cache.get_range(SYM, D(2024, 6, 4), D(2024, 6, 4))
    row = payload["data"][0]
    assert row["price_basis"] == "legacy_unknown" and row["is_final"] is True
    assert payload["price_basis"] == "legacy_unknown"
    assert fetcher.calls == []


def test_legacy_intraday_row_is_detected_via_update_time_and_refetched_in_legacy_mode(
    cache, store, fetcher, clock
):
    clock.now = cn_time(2024, 6, 6, 9, 0)
    insert_legacy(store, SYM, D(2024, 6, 5), 1.0, dt.datetime(2024, 6, 5, 11, 0))  # 盘中同步
    seed_upstream(fetcher, {D(2024, 6, 5): 1.25})
    payload = cache.get_range(SYM, D(2024, 6, 5), D(2024, 6, 5))
    assert fetcher.calls[-1][3] == "legacy"  # 遗留序列沿用历史口径抓取，不混入 raw
    row = payload["data"][0]
    assert row["close_price"] == 1.25 and row["is_final"] is True
    assert row["price_basis"] == "ths_qfq"


def test_legacy_adjusted_series_rejects_adjust_qfq(cache, store, clock):
    clock.now = cn_time(2024, 6, 6, 9, 0)
    insert_legacy(store, SYM, D(2024, 6, 4), 1.0, dt.datetime(2024, 6, 5, 0, 0))
    with pytest.raises(RequestError):
        cache.get_range(SYM, D(2024, 6, 4), D(2024, 6, 4), adjust="qfq")


def test_series_mode_new_symbol_raw_existing_legacy(cache, store, fetcher, clock):
    clock.now = cn_time(2024, 6, 6, 9, 0)
    assert store.series_mode("NEW.SZ") == "raw"
    seed_upstream(fetcher, {D(2024, 6, 4): 1.0})
    cache.get_range(SYM, D(2024, 6, 4), D(2024, 6, 4))
    assert store.series_mode(SYM) == "raw"  # 新写入的行是 raw
    insert_legacy(store, "OLD.SZ", D(2024, 6, 4), 1.0, dt.datetime(2024, 6, 5, 0, 0))
    assert store.series_mode("OLD.SZ") == "legacy"


# ------------------------------------------------------------ 复权口径 / 拆分
def test_corporate_split_invalidates_and_refetches_history(cache, fetcher, clock):
    clock.now = cn_time(2024, 6, 6, 9, 0)
    sym = "AAPL"
    fetcher.set_rows(sym, [make_row(sym, D(2024, 6, 3), 100.0), make_row(sym, D(2024, 6, 4), 102.0)])
    cache.get_range(sym, D(2024, 6, 3), D(2024, 6, 4))

    # 之后发生 2:1 拆分：上游历史整体减半，6/6 出现拆分事件
    clock.now = cn_time(2024, 6, 7, 9, 0)
    fetcher.set_rows(
        sym,
        [make_row(sym, D(2024, 6, 3), 50.0), make_row(sym, D(2024, 6, 4), 51.0),
         make_row(sym, D(2024, 6, 6), 52.0)],
    )
    fetcher.actions[sym] = [{"ex_date": D(2024, 6, 6), "kind": "split", "value": 2.0}]
    clock.advance(seconds=700)
    payload = cache.get_range(sym, D(2024, 6, 3), D(2024, 6, 6))
    assert [r["close_price"] for r in payload["data"]] == [50.0, 51.0, 52.0]
    assert payload["corrected_rows"] >= 1
    # 补抓窗口覆盖了最早的缓存日
    assert any(call[1] == D(2024, 6, 3) for call in fetcher.calls[1:])


def test_split_refetch_failure_leaves_rows_invalid_and_retried(cache, fetcher, clock):
    clock.now = cn_time(2024, 6, 6, 9, 0)
    sym = "AAPL"
    fetcher.set_rows(sym, [make_row(sym, D(2024, 6, 3), 100.0), make_row(sym, D(2024, 6, 4), 102.0)])
    cache.get_range(sym, D(2024, 6, 3), D(2024, 6, 4))

    clock.now = cn_time(2024, 6, 7, 9, 0)
    fetcher.set_rows(sym, [make_row(sym, D(2024, 6, 4), 51.0), make_row(sym, D(2024, 6, 6), 52.0)])  # 历史窗口取不到 6/3
    fetcher.actions[sym] = [{"ex_date": D(2024, 6, 6), "kind": "split", "value": 2.0}]
    clock.advance(seconds=700)
    payload = cache.get_range(sym, D(2024, 6, 3), D(2024, 6, 6))
    # 旧行被标为非终值（仍可读但如实暴露），下次会重试
    assert payload["data"][0]["is_final"] is False


def test_dividend_adjustment_is_computed_view_not_stored(cache, fetcher, store, clock):
    clock.now = cn_time(2024, 6, 10, 9, 0)
    sym = "MSFT"
    fetcher.basis = "split_adjusted"
    fetcher.set_rows(
        sym, [make_row(sym, D(2024, 6, 3), 100.0), make_row(sym, D(2024, 6, 4), 100.0),
              make_row(sym, D(2024, 6, 5), 98.0)],
    )
    fetcher.actions[sym] = [{"ex_date": D(2024, 6, 5), "kind": "dividend", "value": 2.0}]
    raw = cache.get_range(sym, D(2024, 6, 3), D(2024, 6, 5))
    assert raw["price_view"] == "as_stored" and raw["price_basis"] == "split_adjusted"
    assert [r["close_price"] for r in raw["data"]] == [100.0, 100.0, 98.0]

    adj = cache.get_range(sym, D(2024, 6, 3), D(2024, 6, 5), adjust="qfq")
    assert adj["price_view"] == "adjusted_qfq"
    assert [r["close_price"] for r in adj["data"]] == [98.0, 98.0, 98.0]  # 1 - 2/100
    # 库里仍是原始价
    assert store.query_range(sym, D(2024, 6, 3), D(2024, 6, 5))[0]["close_price"] == 100.0


def test_apply_dividend_adjustment_without_events_is_identity():
    rows = [{"date": D(2024, 1, 1), "open_price": 1.0, "close_price": 1.0, "high": 1.0, "low": 1.0}]
    assert apply_dividend_adjustment(rows, []) == rows


# ------------------------------------------------------------------- 其它
def test_invalid_inputs(cache):
    with pytest.raises(RequestError):
        cache.get_range("bad symbol!", D(2024, 6, 1), D(2024, 6, 2))
    with pytest.raises(RequestError):
        cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 2))
    with pytest.raises(RequestError):
        cache.get_range(SYM, D(2024, 6, 3), D(2030, 1, 1))
    with pytest.raises(RequestError):
        cache.get_range(SYM, D(2024, 6, 1), D(2024, 6, 2), refresh="sometimes")


def test_fund_nav_rows_are_always_final(cache, fetcher, clock):
    code = "005698"
    fetcher.set_rows(code, [make_row(code, D(2024, 6, 5), 1.5)])
    payload = cache.get_range(code, D(2024, 6, 5), D(2024, 6, 5))
    assert payload["data"][0]["is_final"] is True
    assert payload["series_type"] == "fund_nav"
    assert payload["upstream_route"].startswith("eastmoney_off_market_fund_nav")


def test_meta_table_records_basis_and_source(cache, store, fetcher, clock):
    clock.now = cn_time(2024, 6, 6, 9, 0)
    seed_upstream(fetcher, {D(2024, 6, 4): 1.0})
    cache.get_range(SYM, D(2024, 6, 4), D(2024, 6, 4))
    with store.Session() as s:
        meta = s.query(MarketDataMeta).one()
        assert (meta.symbol, meta.date, meta.price_basis, meta.source, bool(meta.is_final)) == (
            SYM, D(2024, 6, 4), "raw", "fake_upstream", True,
        )


def test_immediate_second_read_after_first_fetch_does_not_hit_upstream_again(cache, fetcher, clock):
    clock.now = cn_time(2024, 6, 8, 9, 0)  # 周六，最新交易日是 6/7
    seed_upstream(fetcher, {D(2024, 6, 6): 1.0, D(2024, 6, 7): 1.1})
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 8))
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 8))
    assert len(fetcher.calls) == 1  # 尾部检查间隔内不重复回源


def test_non_fetch_errors_are_reported_by_type_only(cache, fetcher, clock):
    clock.now = cn_time(2024, 6, 6, 9, 0)
    seed_upstream(fetcher, {D(2024, 6, 3): 1.0})
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 3))
    fetcher.fail = RuntimeError("Native library not available at /app/.venv/secret/path.so")
    clock.advance(seconds=700)
    payload = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert payload["tail_refresh_error"] == "RuntimeError"


# ------------------------------------------------------------------- 只读数据库
def test_read_only_store_returns_cached_rows_and_reports_read_only_without_upstream_calls(
    cache, store, fetcher, clock
):
    clock.now = cn_time(2024, 6, 6, 9, 0)
    seed_upstream(fetcher, {D(2024, 6, 3): 1.0, D(2024, 6, 4): 1.1})
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 4))
    calls = len(fetcher.calls)

    store.configure_read_only()
    for refresh in ("always", "missing"):
        clock.advance(seconds=900)  # 尾部检查也到期
        payload = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 6), refresh=refresh)
        assert payload["count"] == 2 and payload["tail_refresh_error"] == "read_only"
        assert payload["refreshed"] is False
    latest = cache.get_latest(SYM, refresh="always")
    assert latest["data"]["close_price"] == 1.1 and latest["tail_refresh_error"] == "read_only"
    assert len(fetcher.calls) == calls  # 不写库就不回源


def test_read_only_with_empty_cache_does_not_raise_in_cache_layer(cache, store):
    store.configure_read_only()
    payload = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 4), refresh="always")
    assert payload["count"] == 0 and payload["tail_refresh_error"] == "read_only"


def test_privilege_error_at_runtime_switches_to_read_only(cache, store, fetcher, clock, monkeypatch):
    from sqlalchemy.exc import OperationalError

    clock.now = cn_time(2024, 6, 6, 9, 0)
    seed_upstream(fetcher, {D(2024, 6, 3): 1.0})
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 3))

    from sqlalchemy.orm import Session, sessionmaker

    class DeniedSession(Session):  # 模拟只读账号：任何写语句都被 MySQL 以 1142 拒绝
        def execute(self, statement, *args, **kwargs):
            if getattr(statement, "is_insert", False) or getattr(statement, "is_update", False):
                raise OperationalError("INSERT", {}, Exception(1142, "INSERT command denied to user"))
            return super().execute(statement, *args, **kwargs)

    monkeypatch.setattr(store, "Session", sessionmaker(bind=store.engine, class_=DeniedSession))
    seed_upstream(fetcher, {D(2024, 6, 3): 1.0, D(2024, 6, 4): 1.1})
    clock.advance(seconds=900)
    payload = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert payload["tail_refresh_error"] == "read_only" and payload["count"] == 1
    assert store.read_only is True  # 之后不再尝试回源
    n = len(fetcher.calls)
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5), refresh="always")
    assert len(fetcher.calls) == n


def test_read_only_service_raises_clear_no_data_error(cache, store):
    from trading.market.service import MarketService, NoDataError

    store.configure_read_only()
    with pytest.raises(NoDataError) as exc:
        MarketService(cache).price_history([SYM], D(2024, 6, 3), D(2024, 6, 4))
    assert "read-only" in str(exc.value)
