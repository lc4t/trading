"""指数（000300.SS / ^HSI）取数顺序、对账、回补：全部 mock，不访问外网.

- 000300.SS / 000300.SH：腾讯 → 东财（专用 6 次短退避）→ 缓存降级；Yahoo 不参与；
- ^HSI：Yahoo → 腾讯 hkHSI；东财 ^HSI 不依赖；
- 新抓到的收盘价与库里已完成交易日对账：>0.05% 警告、>0.5% 不覆盖；
- 回补 CLI 默认 dry-run。
"""
import datetime as dt
from types import SimpleNamespace

import pytest
import requests
from loguru import logger

from trading.cli import backfill_benchmark as backfill
from trading.data import fetcher as data_fetcher
from trading.data.fetcher import FetchStatus
from trading.market.errors import FetchError
from trading.market.fetchers import INDEX_EASTMONEY, INDEX_TENCENT, FetchResult, RangePriceFetcher
from trading.market.reconcile import reconcile_rows

from .conftest import cn_time, make_row

D = dt.date
BENCH = "000300.SS"
HSI = "^HSI"
TUE, WED = D(2024, 6, 4), D(2024, 6, 5)


# ------------------------------------------------------------------ 假 HTTP
class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self._payload

    headers = {}


def tencent_payload(code, bars):
    return {"code": 0, "data": {code: {"day": [[str(d), str(c - 1), str(c), str(c + 1), str(c - 2), "100"] for d, c in bars]}}}


def eastmoney_payload(bars):
    return {"data": {"klines": [f"{d},{c - 1},{c},{c + 1},{c - 2},100,1000" for d, c in bars]}}


class FakeHttp:
    """按 URL 里的主机分发；每个主机一个「剧本」：异常 / 响应 / 可调用，按调用次序消费，用完后重复最后一个."""

    def __init__(self):
        self.script = {"gtimg": [], "eastmoney": []}
        self.calls = {"gtimg": [], "eastmoney": []}

    def __call__(self, url, params=None, **_kwargs):
        host = "gtimg" if "gtimg" in url else "eastmoney"
        self.calls[host].append(params)
        steps = self.script[host]
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        if isinstance(step, Exception):
            raise step
        return step(params) if callable(step) else step


@pytest.fixture
def http(monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr("requests.get", fake)
    monkeypatch.setattr("trading.market.fetchers.time.sleep", lambda _s: None)  # 重试间隔不真睡
    return fake


def down(n=1):
    return [requests.ConnectionError("RemoteDisconnected")] * n


# ------------------------------------------------------------------ 取数顺序
def test_tencent_is_primary_and_the_in_progress_bar_is_trimmed(http):
    http.script["gtimg"] = [FakeResponse(tencent_payload("sh000300", [(TUE, 3900), (WED, 3950)]))]
    result = RangePriceFetcher().fetch(BENCH, D(2024, 6, 1), TUE)  # 结束日 = 上一个已完成交易日
    assert result.source == INDEX_TENCENT and result.price_basis == "raw"
    assert [r["date"] for r in result.rows] == [TUE]  # 腾讯带出的当天盘中那根被裁掉
    assert result.rows[0]["close_price"] == 3900.0
    assert not http.calls["eastmoney"]


def test_sh_alias_uses_the_same_route(http):
    http.script["gtimg"] = [FakeResponse(tencent_payload("sh000300", [(TUE, 3900)]))]
    assert RangePriceFetcher().fetch("000300.SH", D(2024, 6, 1), TUE).source == INDEX_TENCENT


def test_tencent_failure_falls_back_to_eastmoney(http):
    http.script["gtimg"] = down()
    http.script["eastmoney"] = [FakeResponse(eastmoney_payload([(TUE, 3900)]))]
    result = RangePriceFetcher().fetch(BENCH, D(2024, 6, 1), TUE)
    assert result.source == INDEX_EASTMONEY and result.rows[0]["close_price"] == 3900.0
    assert len(http.calls["gtimg"]) == 3  # 腾讯 3 次短重试后才换源


def test_tencent_param_error_and_rate_limit_also_fall_back(http):
    http.script["gtimg"] = [FakeResponse({"code": 0, "msg": "param error", "data": []})]
    http.script["eastmoney"] = [FakeResponse(eastmoney_payload([(TUE, 3900)]))]
    assert RangePriceFetcher().fetch(BENCH, D(2024, 6, 1), TUE).source == INDEX_EASTMONEY
    http.script["gtimg"] = [FakeResponse({}, status=429)]
    assert RangePriceFetcher().fetch(BENCH, D(2024, 6, 1), TUE).source == INDEX_EASTMONEY


def test_eastmoney_disconnects_are_retried_with_the_dedicated_policy(http):
    http.script["gtimg"] = down()
    http.script["eastmoney"] = down(4) + [FakeResponse(eastmoney_payload([(TUE, 3900)]))]
    result = RangePriceFetcher().fetch(BENCH, D(2024, 6, 1), TUE)
    assert result.source == INDEX_EASTMONEY
    assert len(http.calls["eastmoney"]) == 5  # 断连 4 次后第 5 次成功（上限 6 次）


def test_all_sources_failing_raises_and_eastmoney_gets_exactly_six_attempts(http):
    http.script["gtimg"] = down()
    http.script["eastmoney"] = down()
    with pytest.raises(FetchError, match="all index sources failed"):
        RangePriceFetcher().fetch(BENCH, D(2024, 6, 1), TUE)
    assert len(http.calls["gtimg"]) == 3 and len(http.calls["eastmoney"]) == 6


def test_sources_that_answer_with_no_data_return_an_empty_result_not_an_error(http):
    http.script["gtimg"] = [FakeResponse(tencent_payload("sh000300", []))]
    http.script["eastmoney"] = [FakeResponse(eastmoney_payload([]))]
    assert RangePriceFetcher().fetch(BENCH, D(2024, 6, 1), TUE).rows == []


def test_long_ranges_are_split_into_tencent_sized_windows(http):
    def respond(params):
        _, _, start, end, count, _ = params["param"].split(",")
        assert int(count) <= 2000 and (D.fromisoformat(end) - D.fromisoformat(start)).days < 2000
        return FakeResponse(tencent_payload("sh000300", [(D.fromisoformat(end), 3000)]))

    http.script["gtimg"] = [respond]
    result = RangePriceFetcher().fetch_index_tencent(BENCH, D(2015, 1, 1), D(2024, 6, 4))
    assert len(http.calls["gtimg"]) >= 2 and len({r["date"] for r in result.rows}) == len(result.rows)


def test_hang_seng_uses_yahoo_first_then_tencent_and_never_eastmoney(http, monkeypatch):
    import pandas as pd

    class YahooDown:
        def __init__(self, symbol):
            pass

        def history(self, **_kw):
            raise RuntimeError("yahoo down")

    monkeypatch.setattr("yfinance.Ticker", YahooDown)
    http.script["gtimg"] = [FakeResponse(tencent_payload("hkHSI", [(TUE, 22000)]))]
    result = RangePriceFetcher().fetch(HSI, D(2024, 6, 1), TUE)
    assert result.source == INDEX_TENCENT and http.calls["gtimg"][0]["param"].startswith("hkHSI,")
    assert not http.calls["eastmoney"]

    class YahooEmpty(YahooDown):
        def history(self, **_kw):
            return pd.DataFrame()

    monkeypatch.setattr("yfinance.Ticker", YahooEmpty)
    assert RangePriceFetcher().fetch(HSI, D(2024, 6, 1), TUE).source == INDEX_TENCENT

    monkeypatch.setattr("yfinance.Ticker", YahooDown)
    http.script["gtimg"] = down()
    with pytest.raises(FetchError):  # Yahoo 与腾讯都报错：不去碰东财
        RangePriceFetcher().fetch(HSI, D(2024, 6, 1), TUE)
    assert not http.calls["eastmoney"]


def test_hang_seng_prefers_yahoo_when_it_works(http, monkeypatch):
    import pandas as pd

    class YahooOk:
        def __init__(self, symbol):
            pass

        def history(self, **_kw):
            return pd.DataFrame(
                [{"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 1, "Dividends": 0.0, "Stock Splits": 0.0}],
                index=pd.DatetimeIndex([pd.Timestamp(TUE)], tz="Asia/Hong_Kong"),
            )

    monkeypatch.setattr("yfinance.Ticker", YahooOk)
    result = RangePriceFetcher().fetch(HSI, D(2024, 6, 1), TUE)
    assert result.source == "yahoo_unadjusted" and not http.calls["gtimg"]


# ------------------------------------------------------------------ 对账（纯函数）
def r(day, close):
    return make_row(BENCH, day, close)


def stored(day, close, final=True):
    row = r(day, close)
    row["meta"] = {"is_final": final}
    return row


@pytest.mark.parametrize(
    "new_close, kept, level",
    [(3900.0 * 1.0004, True, None), (3900.0 * 1.0006, True, "warning"), (3900.0 * 1.004, True, "warning"),
     (3900.0 * 1.006, False, "error"), (3900.0 * 0.994, False, "error")],
)
def test_reconcile_thresholds(new_close, kept, level):
    rows, findings = reconcile_rows([r(TUE, new_close)], [stored(TUE, 3900.0)])
    assert bool(rows) is kept
    assert (findings[0].level if findings else None) == level


def test_reconcile_skips_unfinished_and_unknown_days_and_treats_legacy_rows_as_complete():
    assert reconcile_rows([r(TUE, 9999.0)], [stored(TUE, 3900.0, final=False)])[1] == []  # 库里是盘中半根
    assert reconcile_rows([r(TUE, 9999.0)], [])[1] == []  # 库里没有这一天
    legacy = r(TUE, 3900.0)  # 遗留行没有 meta
    legacy["meta"] = None
    assert reconcile_rows([r(TUE, 3990.0)], [legacy])[1][0].level == "error"


# ------------------------------------------------------------------ 同步路径：对账 / 降级 / 当天盘中
@pytest.fixture
def db_client(store):
    return SimpleNamespace(engine=store.engine, Session=store.Session)


@pytest.fixture
def manager(monkeypatch, db_client):
    monkeypatch.setattr(data_fetcher, "DBClient", lambda: db_client)
    mgr = data_fetcher.StockDataManager(now=lambda: cn_time(2024, 6, 6, 9, 0))  # 周四早晨，最近已完成 = 周三
    mgr.market_fetcher = RangePriceFetcher()
    return mgr


def sync(mgr, symbol=BENCH, end=WED):
    return mgr.sync_symbol(symbol, start_date=D(2024, 6, 1), end_date=end, db_client=mgr.db_client)


def seed(store, day, close, final=True):
    store.upsert([make_row(BENCH, day, close)], price_basis="raw", final_flags={day: final})


def test_sync_writes_new_index_rows_from_tencent(http, manager, store):
    http.script["gtimg"] = [FakeResponse(tencent_payload("sh000300", [(TUE, 3900), (WED, 3950)]))]
    assert sync(manager).status is FetchStatus.OK
    rows = store.query_range(BENCH, D(2024, 6, 1), D(2024, 6, 30))
    assert [x["close_price"] for x in rows] == [3900.0, 3950.0]
    assert {x["meta"]["source"] for x in rows} == {INDEX_TENCENT} and all(x["meta"]["is_final"] for x in rows)


def test_daytime_sync_does_not_request_or_store_todays_bar(http, manager, store):
    manager._now = lambda: cn_time(2024, 6, 5, 10, 0)  # 周三盘中：结束日 = 周二
    http.script["gtimg"] = [FakeResponse(tencent_payload("sh000300", [(TUE, 3900), (WED, 3950)]))]
    assert sync(manager).status is FetchStatus.OK
    assert http.calls["gtimg"][0]["param"].split(",")[3] == "2024-06-04"
    assert [x["date"] for x in store.query_range(BENCH, D(2024, 6, 1), D(2024, 6, 30))] == [TUE]


def test_reconcile_error_keeps_stored_close_and_degrades_the_outcome(http, manager, store):
    seed(store, TUE, 3900.0)
    http.script["gtimg"] = [FakeResponse(tencent_payload("sh000300", [(TUE, 3900 * 1.01), (WED, 3950)]))]
    outcome = sync(manager)
    assert outcome.status is FetchStatus.DEGRADED and "对账未通过" in outcome.reason
    rows = {x["date"]: x["close_price"] for x in store.query_range(BENCH, D(2024, 6, 1), D(2024, 6, 30))}
    assert rows == {TUE: 3900.0, WED: 3950.0}  # 周二没被覆盖，周三正常写入


def test_reconcile_warning_still_overwrites_and_logs(http, manager, store):
    seed(store, TUE, 3900.0)
    messages = []
    sink = logger.add(lambda m: messages.append(m.record["message"]), level="WARNING")
    try:
        http.script["gtimg"] = [FakeResponse(tencent_payload("sh000300", [(TUE, 3902.4)]))]  # +0.0615%
        assert sync(manager).status is FetchStatus.OK
    finally:
        logger.remove(sink)
    assert store.query_range(BENCH, TUE, TUE)[0]["close_price"] == 3902.4
    assert any("对账差异" in m for m in messages)


def test_all_index_sources_down_falls_back_to_cache_degraded(http, manager, store):
    seed(store, TUE, 3900.0)
    http.script["gtimg"], http.script["eastmoney"] = down(), down()
    outcome = sync(manager)
    assert outcome.status is FetchStatus.DEGRADED and "沿用数据库缓存" in outcome.reason
    assert [x["close_price"] for x in store.query_range(BENCH, D(2024, 6, 1), D(2024, 6, 30))] == [3900.0]


def test_all_index_sources_down_without_cache_is_still_not_fatal_for_a_benchmark(http, manager):
    http.script["gtimg"], http.script["eastmoney"] = down(), down()
    assert sync(manager).status is FetchStatus.DEGRADED


# ------------------------------------------------------------------ 回补 CLI
class FakeIndexFetcher:
    def __init__(self, rows=None, error=None, source=INDEX_TENCENT):
        self.rows, self.error, self.source, self.calls = rows or [], error, source, []

    def fetch_index(self, symbol, start, end, *, sources=None, **_kw):
        self.calls.append((symbol, start, end, sources))
        if self.error:
            raise self.error
        return FetchResult([x for x in self.rows if start <= x["date"] <= end], self.source, "raw")


NOW = lambda: cn_time(2024, 6, 6, 9, 0)  # noqa: E731


def bars(*pairs):
    return [make_row(BENCH, d, c) for d, c in pairs]


def test_plan_backfill_computes_missing_incomplete_extra_and_findings():
    src = bars((D(2024, 6, 3), 3800.0), (TUE, 3900.0), (WED, 3950.0))
    db = [stored(D(2024, 6, 3), 3800.0), stored(TUE, 3900.0 * 1.01), {**r(D(2024, 6, 2), 1.0), "meta": None}]
    db[1]["meta"] = {"is_final": True}
    plan = backfill.plan_backfill(BENCH, INDEX_TENCENT, D(2024, 6, 1), WED, src, db)
    assert plan.missing == [WED] and plan.extra == [D(2024, 6, 2)] and plan.incomplete == []
    assert [f.level for f in plan.findings] == ["error"] and [x["date"] for x in plan.rows_to_write] == [WED]


def test_dry_run_reports_without_writing_and_exposes_a_step_summary(store, tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    seed(store, D(2024, 6, 3), 3800.0)
    fetcher = FakeIndexFetcher(bars((D(2024, 6, 3), 3800.0), (TUE, 3900.0), (WED, 3950.0)))
    code = backfill.run([BENCH], D(2024, 6, 1), None, apply=False, store=store, fetcher=fetcher, now=NOW)
    assert code == 0
    assert [x["date"] for x in store.query_range(BENCH, D(2024, 6, 1), D(2024, 6, 30))] == [D(2024, 6, 3)]  # 没写
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "缺失 **2** 个" in out and "2024-06-04" in out and "--apply" in out
    assert "DRY-RUN" in summary.read_text(encoding="utf-8")
    assert fetcher.calls[0][2] == WED and fetcher.calls[0][3] == [INDEX_TENCENT, INDEX_EASTMONEY]


def test_apply_writes_missing_and_unfinished_rows_but_never_overwrites_mismatches(store):
    seed(store, D(2024, 6, 3), 3800.0)                 # 完整且一致
    seed(store, TUE, 3000.0)                          # 完整但差异 >0.5%：只报告，不覆盖
    store.upsert([make_row(BENCH, D(2024, 6, 2), 3700.0)], price_basis="raw", final_flags={D(2024, 6, 2): False})  # 盘中半根
    fetcher = FakeIndexFetcher(bars((D(2024, 6, 2), 3705.0), (D(2024, 6, 3), 3800.0), (TUE, 3900.0), (WED, 3950.0)))
    assert backfill.run([BENCH], D(2024, 6, 1), None, apply=True, store=store, fetcher=fetcher, now=NOW) == 0
    rows = {x["date"]: x for x in store.query_range(BENCH, D(2024, 6, 1), D(2024, 6, 30))}
    assert rows[TUE]["close_price"] == 3000.0                                # 差异行保持原值
    assert rows[WED]["close_price"] == 3950.0 and rows[WED]["meta"]["is_final"] is True
    assert rows[D(2024, 6, 2)]["close_price"] == 3705.0 and rows[D(2024, 6, 2)]["meta"]["is_final"] is True
    assert {rows[d]["meta"]["source"] for d in (WED, D(2024, 6, 2))} == {INDEX_TENCENT}
    # 再跑一次：没有需要写入的行（幂等）
    assert backfill.run([BENCH], D(2024, 6, 1), None, apply=True, store=store, fetcher=fetcher, now=NOW) == 0


def test_backfill_source_failure_exits_nonzero_without_writing(store, capsys):
    fetcher = FakeIndexFetcher(error=FetchError("all index sources failed: tencent_index=down"))
    assert backfill.run([BENCH], D(2024, 6, 1), None, apply=True, store=store, fetcher=fetcher, now=NOW) == 1
    assert "取数失败" in capsys.readouterr().out
    assert store.query_range(BENCH, D(2024, 6, 1), D(2024, 6, 30)) == []


def test_backfill_start_defaults_to_the_first_stored_date(store):
    seed(store, D(2024, 6, 3), 3800.0)
    fetcher = FakeIndexFetcher(bars((D(2024, 6, 3), 3800.0)))
    backfill.run([BENCH], None, None, apply=False, store=store, fetcher=fetcher, now=NOW)
    assert fetcher.calls[0][1] == D(2024, 6, 3)


def test_main_is_dry_run_by_default_and_rejects_unknown_symbols(store, capsys):
    fetcher = FakeIndexFetcher(bars((TUE, 3900.0)))
    backfill.main(["--start-date", "2024-06-01", "--end-date", "2024-06-04"], store=store, fetcher=fetcher)
    assert store.query_range(BENCH, D(2024, 6, 1), D(2024, 6, 30)) == []
    assert "DRY-RUN" in capsys.readouterr().out
    backfill.main(["--start-date", "2024-06-01", "--end-date", "2024-06-04", "--apply"], store=store, fetcher=fetcher)
    assert len(store.query_range(BENCH, D(2024, 6, 1), D(2024, 6, 30))) == 1
    with pytest.raises(SystemExit) as exc:
        backfill.main(["--symbol", "AAPL"], store=store, fetcher=fetcher)
    assert exc.value.code == 2
