"""Yahoo 不可达时的备用上游：口径锁、重叠日校验、限流切换、失败形态。全部用假 fetcher / mock，不访问网络。"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from trading.market.basis import (
    FREE,
    MISMATCH,
    OVERLAP,
    OVERLAP_TOLERANCE,
    append_rule,
    check_overlap_rows,
    overlap_check,
)
from trading.market.cache import MarketCache
from trading.market.errors import FetchError, RateLimited
from trading.market.fetchers import FetchResult
from trading.market.service import MarketService, RateLimitedError
from trading.market.store import MarketStore
from trading.market.upstream import GuardedFetcher, UpstreamGuard

from .conftest import Clock, cn_time, make_row

D = dt.date
HK = "0700.HK"
US = "AAPL"
EM, SINA, TX = "eastmoney_fqt0", "sina_us", "tencent_qfq"


# ------------------------------------------------------------------ 假上游
@dataclass
class BackupFetcher:
    """主上游（Yahoo）可以设为限流；备用源按来源名各给一份数据或一个异常."""

    yahoo_rows: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    yahoo_error: Optional[Exception] = None
    backup_rows: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)  # source -> rows
    backup_errors: Dict[str, Exception] = field(default_factory=dict)
    yahoo_calls: List[tuple] = field(default_factory=list)
    backup_calls: List[tuple] = field(default_factory=list)
    yahoo_basis: str = "split_adjusted"

    def fetch(self, symbol, start, end, *, mode="raw", retry=None) -> FetchResult:
        self.yahoo_calls.append((symbol, start, end, mode))
        if self.yahoo_error is not None:
            raise self.yahoo_error
        rows = [dict(r) for r in self.yahoo_rows.get(symbol, []) if start <= r["date"] <= end]
        return FetchResult(rows, "yahoo_unadjusted", "yahoo_adjusted" if mode == "legacy" else self.yahoo_basis)

    def fetch_backup(self, symbol, start, end, source, *, retry=None) -> FetchResult:
        self.backup_calls.append((symbol, source, start, end))
        if source in self.backup_errors:
            raise self.backup_errors[source]
        rows = [dict(r) for r in self.backup_rows.get(source, []) if start <= r["date"] <= end]
        basis = "qfq_tencent" if source == TX else "unadjusted"
        return FetchResult(rows, source, basis)


def rows_for(symbol, closes: Dict[dt.date, float]):
    return [make_row(symbol, d, c) for d, c in closes.items()]


@pytest.fixture
def store():
    s = MarketStore(create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}))
    s.init_schema()
    return s


@pytest.fixture
def clock():
    return Clock(cn_time(2024, 6, 6, 9, 0))  # 周四早晨：6/3~6/5 的港股 / 美股（北京时间）都已可定稿


@pytest.fixture
def up():
    return BackupFetcher(yahoo_error=RateLimited("yahoo", 120.0))


def make_cache(store, up, clock, **kw):
    return MarketCache(
        store, up, now=clock, monotonic=clock.monotonic, tail_check_ttl=600, intraday_ttl=300, failure_backoff=60, **kw
    )


@pytest.fixture
def cache(store, up, clock):
    return make_cache(store, up, clock)


def seed(store, symbol, closes, *, basis: Optional[str], final=True, fetched=None):
    """直接写库：basis=None 表示遗留行（没有旁表记录）."""
    fetched = fetched or cn_time(2024, 6, 6, 3, 0)
    rows = rows_for(symbol, closes)
    store.upsert(
        rows,
        price_basis=basis,
        source="seeded" if basis else None,
        fetched_at=fetched,
        final_flags={r["date"]: final for r in rows},
        as_of={r["date"]: fetched for r in rows},
    )


def stored(store, symbol):
    return {r["date"]: r for r in store.query_range(symbol, D(2024, 1, 1), D(2024, 12, 31))}


# =============================================================== 口径锁（纯函数）
def test_append_rule_table():
    assert append_rule(None, "unadjusted") == FREE and append_rule(None, "qfq_tencent") == FREE  # 空序列
    assert append_rule("unadjusted", "unadjusted") == FREE
    assert append_rule("qfq_tencent", "qfq_tencent") == OVERLAP  # 前复权同口径也要证明没被改写
    # 唯一的例外：Yahoo 调整价（含遗留行）接不复权行，必须过重叠日校验
    assert append_rule("yahoo_adjusted", "unadjusted") == OVERLAP
    assert append_rule("legacy_unknown", "unadjusted") == OVERLAP
    # 其余一律不追加
    assert append_rule("split_adjusted", "unadjusted") == MISMATCH
    assert append_rule("raw", "unadjusted") == MISMATCH
    assert append_rule("unadjusted", "qfq_tencent") == MISMATCH  # 不复权行与腾讯前复权绝不混写
    assert append_rule("qfq_tencent", "unadjusted") == MISMATCH
    assert append_rule("yahoo_adjusted", "qfq_tencent") == MISMATCH
    assert append_rule("ths_qfq", "unadjusted") == MISMATCH


# =============================================================== 重叠日校验（纯函数）
def test_overlap_check_passes_within_tolerance_and_fails_beyond():
    assert OVERLAP_TOLERANCE == 1e-4
    ok = overlap_check(500.0, 500.04)  # 0.008%
    assert ok.ok and ok.rel_diff == pytest.approx(8e-5) and ok.reason is None
    assert overlap_check(500.0, 500.0).ok
    bad = overlap_check(500.0, 500.06)  # 0.012%
    assert not bad.ok and bad.reason == "close_differs"
    # 一次典型的分红复权：约 0.8%
    assert not overlap_check(495.0, 500.0).ok


def test_overlap_check_tolerates_four_decimal_storage_rounding_on_penny_stocks():
    # 库里只存 4 位小数：0.12344 存成 0.1234，相对差 3.2e-4 > 0.01%，但只是舍入，不是复权
    assert overlap_check(0.1234, 0.12344).ok
    assert not overlap_check(0.1234, 0.1250).ok


def test_overlap_check_missing_or_invalid_closes_never_pass():
    assert overlap_check(100.0, None).reason == "overlap_row_missing"
    assert overlap_check(None, 100.0).reason == "overlap_row_missing"
    assert overlap_check(0.0, 100.0).reason == "invalid_close"
    assert not overlap_check(-1.0, -1.0).ok


def test_check_overlap_rows_compares_the_same_day_only():
    anchor = {"date": D(2024, 6, 5), "close_price": 100.0}
    candidate = rows_for(HK, {D(2024, 6, 4): 50.0, D(2024, 6, 5): 100.0, D(2024, 6, 6): 101.0})
    res = check_overlap_rows(anchor, candidate)
    assert res.ok and res.date == D(2024, 6, 5)
    missing = check_overlap_rows(anchor, rows_for(HK, {D(2024, 6, 6): 100.0}))  # 候选没有锚点那一天
    assert not missing.ok and missing.reason == "overlap_row_missing"
    assert res.as_dict()["date"] == "2024-06-05" and res.as_dict()["tolerance"] == OVERLAP_TOLERANCE


# =============================================================== Yahoo 限流时切换
def test_yahoo_rate_limited_switches_to_unadjusted_backup_for_a_new_symbol(cache, up, store):
    up.backup_rows[EM] = rows_for(HK, {D(2024, 6, 3): 500.0, D(2024, 6, 4): 501.0, D(2024, 6, 5): 502.0})
    out = cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5))
    assert out["count"] == 3 and out["tail_refresh_error"] is None
    assert out["fallback"] is True and out["upstream_source"] == EM
    detail = out["fallback_detail"]
    assert detail["reason"] == "yahoo_rate_limited" and detail["source"] == EM and detail["price_basis"] == "unadjusted"
    row = out["data"][-1]
    assert row["price_basis"] == "unadjusted" and row["source"] == EM and row["is_final"] is True
    assert out["price_basis"] == "unadjusted" and out["upstream_route"] == "backup_unadjusted_market_price"
    assert len(up.yahoo_calls) == 1 and len(up.backup_calls) == 1  # Yahoo 先试，限流后才切换，备用源只请求一次


def test_healthy_yahoo_stays_the_first_choice(cache, up):
    up.yahoo_error = None
    up.yahoo_rows[HK] = rows_for(HK, {D(2024, 6, 3): 500.0, D(2024, 6, 4): 501.0})
    out = cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 4))
    assert out["fallback"] is False and out["fallback_detail"] is None
    assert out["data"][0]["price_basis"] == "split_adjusted" and not up.backup_calls


def test_us_symbol_prefers_sina_then_eastmoney_then_tencent(cache, up):
    up.backup_errors[SINA] = FetchError("sina_us request failed after 2 attempt(s) (RemoteDisconnected)")
    up.backup_rows[EM] = rows_for(US, {D(2024, 6, 4): 190.0, D(2024, 6, 5): 191.0})
    out = cache.get_range(US, D(2024, 6, 4), D(2024, 6, 5))
    assert [c[1] for c in up.backup_calls] == [SINA, EM]  # 腾讯前复权不会在不复权源可用时被碰
    assert out["fallback_detail"]["source"] == EM
    attempts = {a["source"]: a["result"] for a in out["fallback_detail"]["attempts"]}
    assert attempts == {SINA: "fetch_failed", EM: "ok"}


def test_tencent_qfq_is_only_the_last_resort_and_is_labelled_qfq_tencent(cache, up, store):
    up.backup_errors[EM] = FetchError("eastmoney_fqt0 request failed after 2 attempt(s) (RemoteDisconnected)")
    up.backup_rows[TX] = rows_for(HK, {D(2024, 6, 4): 500.0, D(2024, 6, 5): 501.0})
    out = cache.get_range(HK, D(2024, 6, 4), D(2024, 6, 5))
    assert [c[1] for c in up.backup_calls] == [EM, TX]
    assert out["fallback_detail"]["price_basis"] == "qfq_tencent"
    assert {r["price_basis"] for r in out["data"]} == {"qfq_tencent"}
    assert {r["source"] for r in out["data"]} == {TX}


def test_symbols_without_backup_sources_keep_the_plain_rate_limited_contract(cache, up):
    for symbol in ("^HSI", "000300.SS", "BTC-USD"):
        out = cache.get_range(symbol, D(2024, 6, 3), D(2024, 6, 5), refresh="always")
        assert out["tail_refresh_error"] == "rate_limited" and out["retry_after"] == 120
        assert out["fallback"] is False and out["fallback_attempts"] is None
    assert not up.backup_calls


# =============================================================== 序列级口径锁
def test_split_adjusted_series_is_not_extended_by_backup_and_reports_basis_mismatch(cache, up, store):
    seed(store, US, {D(2024, 6, 3): 190.0, D(2024, 6, 4): 191.0}, basis="split_adjusted")
    up.backup_rows[SINA] = rows_for(US, {D(2024, 6, 4): 191.0, D(2024, 6, 5): 192.0})
    out = cache.get_range(US, D(2024, 6, 3), D(2024, 6, 5))
    assert out["count"] == 2 and not up.backup_calls  # 口径不同：连请求都不发
    # 对外仍是 rate_limited（Yahoo 恢复后序列照常续上），口径不符作为附带说明
    assert out["tail_refresh_error"] == "rate_limited" and out["retry_after"] >= 1
    assert out["basis_mismatch"]["reason"] == "series_basis_differs"
    assert out["basis_mismatch"]["series_basis"] == "split_adjusted" and out["basis_mismatch"]["candidate_basis"] == "unadjusted"
    assert {r["price_basis"] for r in out["data"]} == {"split_adjusted"}
    assert set(stored(store, US)) == {D(2024, 6, 3), D(2024, 6, 4)}


def test_unadjusted_and_qfq_never_mix_in_one_series(cache, up, store, clock):
    seed(store, HK, {D(2024, 6, 3): 500.0, D(2024, 6, 4): 501.0}, basis="unadjusted")
    # 不复权源全部失败，腾讯前复权有数据——也不能写进这条不复权序列
    up.backup_errors[EM] = FetchError("eastmoney_fqt0 request failed after 2 attempt(s) (RemoteDisconnected)")
    up.backup_rows[TX] = rows_for(HK, {D(2024, 6, 4): 501.0, D(2024, 6, 5): 502.0})
    out = cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5))
    assert [c[1] for c in up.backup_calls] == [EM]  # 腾讯被口径锁挡在请求之前
    assert set(stored(store, HK)) == {D(2024, 6, 3), D(2024, 6, 4)}
    assert {r["price_basis"] for r in out["data"]} == {"unadjusted"}
    attempts = {a["source"]: a["result"] for a in out["fallback_attempts"]}
    assert attempts == {EM: "fetch_failed", TX: "skipped_basis_mismatch"}


def test_qfq_tencent_series_is_not_extended_by_unadjusted_rows(cache, up, store):
    seed(store, HK, {D(2024, 6, 3): 500.0, D(2024, 6, 4): 501.0}, basis="qfq_tencent")
    up.backup_rows[EM] = rows_for(HK, {D(2024, 6, 4): 501.0, D(2024, 6, 5): 502.0})
    up.backup_rows[TX] = rows_for(HK, {D(2024, 6, 4): 501.0, D(2024, 6, 5): 502.0})
    out = cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5))
    assert [c[1] for c in up.backup_calls] == [TX]  # 序列是前复权：只有腾讯这一个同口径源会被请求
    assert out["fallback_detail"]["source"] == TX and out["fallback_detail"]["overlap"]["ok"] is True
    assert {r["price_basis"] for r in stored_views(cache, HK)} == {"qfq_tencent"}


def stored_views(cache, symbol):
    return [cache.view(symbol, r) for r in cache.store.query_range(symbol, D(2024, 1, 1), D(2024, 12, 31))]


def test_qfq_tencent_same_basis_append_still_needs_the_overlap_check(cache, up, store):
    seed(store, HK, {D(2024, 6, 3): 500.0, D(2024, 6, 4): 501.0}, basis="qfq_tencent")
    # 腾讯在这期间做了除息复权：同一天的前复权价变了，新旧不能接
    up.backup_rows[TX] = rows_for(HK, {D(2024, 6, 4): 496.0, D(2024, 6, 5): 497.0})
    out = cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5))
    assert out["tail_refresh_error"] == "basis_mismatch" and out["basis_mismatch"]["overlap"]["reason"] == "close_differs"
    assert set(stored(store, HK)) == {D(2024, 6, 3), D(2024, 6, 4)}


def test_series_already_on_unadjusted_basis_keeps_using_backup_and_skips_yahoo(store, clock):
    up = BackupFetcher()  # Yahoo 现在是好的
    up.yahoo_rows[HK] = rows_for(HK, {D(2024, 6, 5): 999.0})
    cache = make_cache(store, up, clock)
    seed(store, HK, {D(2024, 6, 3): 500.0, D(2024, 6, 4): 501.0}, basis="unadjusted")
    up.backup_rows[EM] = rows_for(HK, {D(2024, 6, 4): 501.0, D(2024, 6, 5): 502.0})
    out = cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5))
    # Yahoo 的行（拆股调整 / 分红复权）接不到不复权序列上：序列锁在备用口径，不去碰 Yahoo
    assert not up.yahoo_calls
    assert out["fallback"] is True and out["fallback_detail"]["reason"] == "series_basis_locked"
    assert out["data"][-1]["close_price"] == 502.0 and out["data"][-1]["price_basis"] == "unadjusted"


# =============================================================== 对 yahoo_adjusted 序列的尾部补充 + 重叠日校验
@pytest.mark.parametrize("basis", ["yahoo_adjusted", None])  # None = 遗留行（0700.HK / 9626.HK 的现状）
def test_overlap_check_pass_appends_unadjusted_rows_after_the_anchor(cache, up, store, basis):
    seed(store, HK, {D(2024, 6, 3): 500.0, D(2024, 6, 4): 501.0}, basis=basis)
    up.backup_rows[EM] = rows_for(HK, {D(2024, 6, 3): 500.0, D(2024, 6, 4): 501.02, D(2024, 6, 5): 503.0})
    out = cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5))
    assert out["tail_refresh_error"] is None and out["fallback"] is True
    assert out["fallback_detail"]["overlap"]["ok"] is True and out["fallback_detail"]["overlap"]["date"] == "2024-06-04"
    # 请求窗口向前补到了最后一个已定稿行（重叠日）
    assert up.backup_calls[0][2] == D(2024, 6, 4)
    by_date = stored(store, HK)
    assert set(by_date) == {D(2024, 6, 3), D(2024, 6, 4), D(2024, 6, 5)}
    # 锚点行和更早的行原样不动（价位还是 Yahoo 口径的 501.0，不被 501.02 覆盖）；追加的行记自己的口径
    assert by_date[D(2024, 6, 4)]["close_price"] == 501.0
    views = {v["date"]: v for v in stored_views(cache, HK)}
    assert views[D(2024, 6, 4)]["price_basis"] == (basis or "legacy_unknown")
    assert views[D(2024, 6, 5)]["price_basis"] == "unadjusted" and views[D(2024, 6, 5)]["source"] == EM
    assert out["price_basis"] == "mixed"  # 返回里如实标混合，而不是假装一个口径


def test_overlap_check_failure_appends_nothing_and_reports_basis_mismatch(cache, up, store):
    seed(store, HK, {D(2024, 6, 3): 495.0, D(2024, 6, 4): 496.0}, basis="yahoo_adjusted")  # 已被 Yahoo 按分红下调
    up.backup_rows[EM] = rows_for(HK, {D(2024, 6, 4): 501.0, D(2024, 6, 5): 503.0})  # 不复权价
    up.backup_rows[TX] = rows_for(HK, {D(2024, 6, 4): 496.0, D(2024, 6, 5): 498.0})
    out = cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5))
    assert out["tail_refresh_error"] == "basis_mismatch"
    mm = out["basis_mismatch"]
    assert mm["reason"] == "overlap_check_failed" and mm["series_basis"] == "yahoo_adjusted"
    assert mm["candidate_basis"] == "unadjusted"
    assert mm["overlap"]["ok"] is False and mm["overlap"]["reason"] == "close_differs"
    assert mm["overlap"]["stored_close"] == 496.0 and mm["overlap"]["candidate_close"] == 501.0
    assert out["fallback"] is False and out["count"] == 2
    assert set(stored(store, HK)) == {D(2024, 6, 3), D(2024, 6, 4)}  # 没有追加任何行
    # 腾讯前复权看起来对得上（496.0），但它与 Yahoo 序列同样不是同一口径：被口径锁挡住，不会被拿来「凑」
    assert [c[1] for c in up.backup_calls] == [EM]
    assert {a["source"]: a["result"] for a in out["fallback_attempts"]} == {
        EM: "overlap_check_failed", TX: "skipped_basis_mismatch"
    }


def test_overlap_check_requires_the_anchor_day_to_be_present_in_the_backup_data(cache, up, store):
    seed(store, HK, {D(2024, 6, 3): 500.0, D(2024, 6, 4): 501.0}, basis="yahoo_adjusted")
    up.backup_rows[EM] = rows_for(HK, {D(2024, 6, 5): 503.0})  # 备用源没有 6/4
    out = cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5))
    assert out["tail_refresh_error"] == "basis_mismatch"
    assert out["basis_mismatch"]["overlap"]["reason"] == "overlap_row_missing"
    assert set(stored(store, HK)) == {D(2024, 6, 3), D(2024, 6, 4)}


def test_basis_mismatch_is_remembered_during_the_quiet_period_without_new_requests(cache, up, store, clock):
    seed(store, HK, {D(2024, 6, 3): 495.0, D(2024, 6, 4): 496.0}, basis="yahoo_adjusted")
    up.backup_rows[EM] = rows_for(HK, {D(2024, 6, 4): 501.0, D(2024, 6, 5): 503.0})
    cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5))
    calls = len(up.backup_calls)
    clock.advance(seconds=20)
    again = cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5))
    assert len(up.backup_calls) == calls and again["tail_refresh_error"] == "basis_mismatch"
    assert again["basis_mismatch"]["overlap"]["reason"] == "close_differs"


# =============================================================== 备用源失败的返回形态
def test_all_backups_failing_keeps_rate_limited_contract_and_lists_each_attempt(cache, up, store):
    seed(store, HK, {D(2024, 6, 3): 500.0, D(2024, 6, 4): 501.0}, basis=None)
    for source in (EM, TX):
        up.backup_errors[source] = FetchError(f"{source} request failed after 2 attempt(s) (RemoteDisconnected)")
    out = cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5))
    assert out["count"] == 2  # 缓存照常返回
    assert out["tail_refresh_error"] == "rate_limited" and out["fallback"] is False
    # 有备用源只是临时失败：retry_after 不必等到 Yahoo 的冷却结束（120s），按失败静默期给
    assert 1 <= out["retry_after"] <= 60
    attempts = out["fallback_attempts"]
    assert attempts[0]["source"] == EM and attempts[0]["result"] == "fetch_failed"
    assert "RemoteDisconnected" in attempts[0]["detail"] and "2 attempt" in attempts[0]["detail"]
    assert attempts[1]["source"] == TX and attempts[1]["result"] == "skipped_basis_mismatch"


def test_failed_backups_are_not_hammered_on_every_read(cache, up, store, clock):
    seed(store, HK, {D(2024, 6, 3): 500.0, D(2024, 6, 4): 501.0}, basis=None)
    up.backup_errors[EM] = FetchError("eastmoney_fqt0 request failed after 2 attempt(s) (RemoteDisconnected)")
    cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5))
    calls = len(up.backup_calls)
    for _ in range(5):
        clock.advance(seconds=5)
        again = cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5))
    assert len(up.backup_calls) == calls and again["tail_refresh_error"] == "rate_limited"
    assert again["retry_after"] >= 1 and again["fallback_attempts"][0]["result"] == "fetch_failed"


def test_empty_cache_and_every_source_down_gives_structured_rate_limited_with_attempts(store, clock):
    up = BackupFetcher(yahoo_error=RateLimited("yahoo", 300.0))
    for source in (SINA, EM, TX):
        up.backup_errors[source] = FetchError(f"{source} down")
    service = MarketService(make_cache(store, up, clock))
    with pytest.raises(RateLimitedError) as exc:
        service.price_history([US], D(2024, 6, 3), D(2024, 6, 5))
    body = exc.value.body()
    assert body["error"]["code"] == "rate_limited" and 1 <= body["retry_after"] <= 300
    assert [a["source"] for a in body["error"]["fallback_attempts"][US]] == [SINA, EM, TX]
    assert {a["result"] for a in body["error"]["fallback_attempts"][US]} == {"fetch_failed"}


def test_service_payload_exposes_fallback_fields(store, up, clock):
    up.backup_rows[EM] = rows_for(HK, {D(2024, 6, 4): 500.0, D(2024, 6, 5): 501.0})
    service = MarketService(make_cache(store, up, clock))
    out = service.price_history([HK], D(2024, 6, 4), D(2024, 6, 5))
    info = out["cache"][HK]
    assert info["fallback"] is True and info["fallback_detail"]["source"] == EM
    assert info["basis_mismatch"] is None and info["price_basis"] == "unadjusted"
    assert out["data"][0]["source"] == EM and out["data"][0]["price_basis"] == "unadjusted"


def test_backup_can_be_disabled(store, up, clock):
    cache = make_cache(store, up, clock, backup=False)
    out = cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5), refresh="always")
    assert out["tail_refresh_error"] == "rate_limited" and out["fallback"] is False
    assert not up.backup_calls


# =============================================================== 与 awaiting_close 兼容
def test_before_close_there_is_no_upstream_request_at_all_even_with_backup(store, up):
    clock = Clock(cn_time(2024, 6, 5, 10, 0))  # 港股盘中
    cache = make_cache(store, up, clock)
    seed(store, HK, {D(2024, 6, 3): 500.0, D(2024, 6, 4): 501.0}, basis="unadjusted")
    out = cache.get_range(HK, D(2024, 6, 5), D(2024, 6, 5))  # 只要当日
    assert not up.yahoo_calls and not up.backup_calls
    assert out["awaiting_close"] is not None and out["count"] == 0


def test_backup_never_writes_rows_that_are_not_final_yet(store, up):
    # 港股 16:10 收盘 + 30 分钟缓冲；16:00 时当日行还不能定稿
    clock = Clock(cn_time(2024, 6, 5, 16, 0))
    cache = make_cache(store, up, clock)
    up.backup_rows[EM] = rows_for(HK, {D(2024, 6, 4): 501.0, D(2024, 6, 5): 502.0})
    out = cache.get_range(HK, D(2024, 6, 4), D(2024, 6, 5), intraday=True)  # 显式要盘中，Yahoo 又限流
    assert out["fallback"] is True
    assert [r["date"] for r in out["data"]] == [D(2024, 6, 4)]  # 备用源不提供盘中价：当日未定稿的行不写
    assert set(stored(store, HK)) == {D(2024, 6, 4)}


def test_after_the_final_window_opens_the_first_request_uses_backup_and_stores_final_rows(store, up):
    clock = Clock(cn_time(2024, 6, 5, 16, 45))  # 16:40 之后
    cache = make_cache(store, up, clock)
    seed(store, HK, {D(2024, 6, 4): 501.0}, basis="unadjusted", fetched=cn_time(2024, 6, 4, 17, 0))
    up.backup_rows[EM] = rows_for(HK, {D(2024, 6, 4): 501.0, D(2024, 6, 5): 502.0})
    out = cache.get_range(HK, D(2024, 6, 4), D(2024, 6, 5))
    assert out["fallback"] is True and out["data"][-1]["is_final"] is True and out["nonfinal_count"] == 0


def test_finalized_rows_are_not_refetched_from_backup_either(cache, up, store):
    up.backup_rows[EM] = rows_for(HK, {D(2024, 6, 3): 500.0, D(2024, 6, 4): 501.0, D(2024, 6, 5): 502.0})
    cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5))
    calls = len(up.backup_calls)
    up.backup_rows[EM] = rows_for(HK, {D(2024, 6, 3): 9.0, D(2024, 6, 4): 9.0, D(2024, 6, 5): 9.0})
    again = cache.get_range(HK, D(2024, 6, 3), D(2024, 6, 5))
    assert len(up.backup_calls) == calls and [r["close_price"] for r in again["data"]] == [500.0, 501.0, 502.0]


# =============================================================== 与限流冷却 / 间隔保护的配合
class _Inner:
    def __init__(self):
        self.backup_calls = []
        self.fetch_calls = []
        self.backup_error: Optional[Exception] = None

    def fetch(self, symbol, start, end, *, mode="raw", retry=None):
        self.fetch_calls.append(symbol)
        raise RateLimited("yahoo", 120.0)

    def fetch_backup(self, symbol, start, end, source, *, retry=None):
        self.backup_calls.append(source)
        if self.backup_error is not None:
            raise self.backup_error
        return FetchResult(rows_for(symbol, {D(2024, 6, 4): 500.0}), source, "unadjusted")


def test_yahoo_cooldown_routes_to_backup_without_touching_yahoo_again(store, clock):
    inner = _Inner()
    guard = UpstreamGuard(sleep=lambda s: None)
    cache = make_cache(store, GuardedFetcher(inner, guard), clock)
    cache.get_range(HK, D(2024, 6, 4), D(2024, 6, 4))
    assert inner.fetch_calls == [HK] and guard.remaining("yahoo") > 0  # Yahoo 命中限流 → 进入冷却
    cache.get_range(US, D(2024, 6, 4), D(2024, 6, 4))
    assert inner.fetch_calls == [HK]  # 冷却期内不再请求 Yahoo
    assert inner.backup_calls == [EM, SINA]


def test_backup_source_rate_limit_trips_its_own_cooldown_and_is_not_hit_again(store, clock):
    inner = _Inner()
    inner.backup_error = RateLimited("upstream", 60.0)
    guard = UpstreamGuard(sleep=lambda s: None)
    cache = make_cache(store, GuardedFetcher(inner, guard), clock)
    out = cache.get_range(HK, D(2024, 6, 4), D(2024, 6, 4))
    assert out["tail_refresh_error"] == "rate_limited"
    assert inner.backup_calls == [EM, TX]  # 东财限流 → 腾讯接着试（它自己的冷却）
    assert guard.remaining("eastmoney") > 0 and guard.remaining("tencent") > 0
    attempts = out["fallback_attempts"] if "fallback_attempts" in out else []
    assert {a["result"] for a in attempts} == {"rate_limited"} and all(a["retry_after"] >= 1 for a in attempts)
    # 冷却中的备用源直接被跳过，不会再发请求
    calls = len(inner.backup_calls)
    guard2_out = cache.get_range(US, D(2024, 6, 4), D(2024, 6, 4))
    assert len(inner.backup_calls) == calls + 1  # 美股多一个新浪（没在冷却中），东财 / 腾讯不再被请求
    assert guard2_out["tail_refresh_error"] == "rate_limited"


def test_concurrent_reads_share_one_backup_refresh(store, clock):
    import os
    import tempfile
    import threading
    import time

    from sqlalchemy import create_engine as ce

    class SlowInner(_Inner):
        def fetch_backup(self, *a, **k):
            time.sleep(0.15)
            return super().fetch_backup(*a, **k)

    path = os.path.join(tempfile.mkdtemp(), "t.db")
    file_store = MarketStore(ce(f"sqlite:///{path}", connect_args={"check_same_thread": False}))
    file_store.init_schema()
    inner = SlowInner()
    cache = make_cache(file_store, GuardedFetcher(inner, UpstreamGuard(sleep=lambda s: None)), clock)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(cache.get_range(HK, D(2024, 6, 4), D(2024, 6, 4))))
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert inner.backup_calls == [EM] and len(results) == 4  # single-flight：4 个并发读只打一次备用源


def test_adjust_qfq_is_refused_for_backup_rows_instead_of_pretending_to_adjust(cache, up):
    from trading.market.errors import RequestError

    up.backup_rows[EM] = rows_for(HK, {D(2024, 6, 4): 500.0, D(2024, 6, 5): 501.0})
    cache.get_range(HK, D(2024, 6, 4), D(2024, 6, 5))
    with pytest.raises(RequestError, match="备用上游"):
        cache.get_range(HK, D(2024, 6, 4), D(2024, 6, 5), adjust="qfq")


def test_backup_rows_do_not_create_the_corporate_action_baseline(cache, up, store):
    """备用源没有公司行动信息：不能建基线，否则 Yahoo 恢复后第一次同步会把存量分红当成新事件、整段重写历史."""
    up.backup_rows[EM] = rows_for(HK, {D(2024, 6, 4): 500.0, D(2024, 6, 5): 501.0})
    cache.get_range(HK, D(2024, 6, 4), D(2024, 6, 5))
    assert store.list_actions(HK) == []


def test_retry_after_keeps_counting_down_during_yahoo_cooldown_without_backoff_extension(store, clock):
    """回归：Yahoo 冷却 + 备用源口径不符（没有真正请求）时，retry_after 必须随冷却递减，不能被静默期拉长."""
    inner = _Inner()
    guard = UpstreamGuard(sleep=lambda s: None, rng=lambda: 0.5)
    cache = make_cache(store, GuardedFetcher(inner, guard), clock)
    seed(store, US, {D(2024, 6, 3): 190.0, D(2024, 6, 4): 191.0}, basis="split_adjusted")
    seen = []
    for _ in range(6):
        out = cache.get_range(US, D(2024, 6, 3), D(2024, 6, 5))
        seen.append(out["retry_after"])
        clock.advance(seconds=7)
    assert all(a >= b for a, b in zip(seen, seen[1:])), seen
    assert inner.backup_calls == []
