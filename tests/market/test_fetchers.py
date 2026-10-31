"""上游适配器测试：全部用 mock，不访问网络。"""
import datetime as dt
import json
from unittest import mock

import adata
import pandas as pd
import pytest

from trading.market import fetchers
from trading.market.errors import FetchError
from trading.market.fetchers import (
    REQUEST_RETRY,
    SYNC_RETRY,
    RangePriceFetcher,
    is_china_exchange_symbol,
    upstream_route_for_symbol,
)

D = dt.date


def yahoo_frame(with_actions=True):
    index = pd.DatetimeIndex(["2024-06-03", "2024-06-04", "2024-06-05"], tz="America/New_York")
    data = {
        "Open": [10.0, 10.5, float("nan")],
        "High": [11.0, 11.0, float("nan")],
        "Low": [9.5, 10.0, float("nan")],
        "Close": [10.5, 10.8, float("nan")],
        "Volume": [100, 200, 0],
    }
    if with_actions:
        data["Dividends"] = [0.0, 0.25, 0.0]
        data["Stock Splits"] = [0.0, 0.0, 0.0]
    return pd.DataFrame(data, index=index)


class FakeTicker:
    calls = []
    frame = None
    errors = []

    def __init__(self, symbol):
        self.symbol = symbol

    def history(self, **kwargs):
        FakeTicker.calls.append(kwargs)
        if FakeTicker.errors:
            raise FakeTicker.errors.pop(0)
        return FakeTicker.frame


@pytest.fixture(autouse=True)
def reset_ticker(monkeypatch):
    FakeTicker.calls, FakeTicker.errors, FakeTicker.frame = [], [], yahoo_frame()
    monkeypatch.setattr("yfinance.Ticker", FakeTicker)


def test_routing():
    assert is_china_exchange_symbol("159915.SZ") and is_china_exchange_symbol("513100.SS")
    assert not is_china_exchange_symbol("000300.SS")  # 沪深300指数走 Yahoo
    assert not is_china_exchange_symbol("0700.HK")
    assert upstream_route_for_symbol("159915.SZ") == "eastmoney_exchange_price"
    assert upstream_route_for_symbol("159915.SZ", "legacy") == "ths_etf_forward_adjusted"
    assert upstream_route_for_symbol("0700.HK") == "yahoo_unadjusted_market_price"
    assert upstream_route_for_symbol("005698").startswith("eastmoney_off_market_fund_nav")


def test_yahoo_raw_requests_unadjusted_prices_and_collects_actions():
    result = RangePriceFetcher().fetch("0700.HK", D(2024, 6, 3), D(2024, 6, 5))
    call = FakeTicker.calls[0]
    assert call["auto_adjust"] is False and call["actions"] is True
    assert call["end"] == D(2024, 6, 6)  # Yahoo 的 end 不含当日
    assert result.price_basis == "split_adjusted" and result.source == "yahoo_unadjusted"
    assert [r["date"] for r in result.rows] == [D(2024, 6, 3), D(2024, 6, 4)]  # NaN 行被丢弃
    assert result.actions == [{"ex_date": D(2024, 6, 4), "kind": "dividend", "value": 0.25}]
    # 绝不再用分红额扣减历史价
    assert [r["close_price"] for r in result.rows] == [10.5, 10.8]


def test_yahoo_legacy_matches_historical_call_signature():
    FakeTicker.frame = yahoo_frame(with_actions=False)
    result = RangePriceFetcher().fetch_yahoo(
        "^HSI", D(2024, 6, 3), D(2024, 6, 5), retry=SYNC_RETRY, legacy=True
    )
    assert FakeTicker.calls[0] == {"start": D(2024, 6, 3), "end": D(2024, 6, 5), "actions": False}
    assert result.price_basis == "yahoo_adjusted" and result.actions == []


def test_rate_limit_retry_is_short_and_bounded_on_request_path():
    from yfinance.exceptions import YFRateLimitError

    FakeTicker.errors = [YFRateLimitError(), YFRateLimitError(), YFRateLimitError()]
    with mock.patch("time.sleep") as sleep:
        with pytest.raises(FetchError) as exc:
            RangePriceFetcher().fetch("AAPL", D(2024, 6, 3), D(2024, 6, 5))
    assert "YFRateLimitError" in str(exc.value)
    assert len(FakeTicker.calls) == REQUEST_RETRY.attempts == 2
    assert sum(c.args[0] for c in sleep.call_args_list) <= 1.0  # 请求路径不会长时间 sleep


def test_rate_limit_then_success():
    from yfinance.exceptions import YFRateLimitError

    FakeTicker.errors = [YFRateLimitError()]
    with mock.patch("time.sleep"):
        result = RangePriceFetcher().fetch("AAPL", D(2024, 6, 3), D(2024, 6, 5))
    assert len(result.rows) == 2


def test_batch_sync_keeps_historical_long_backoff_policy():
    assert SYNC_RETRY.attempts == 10
    assert [SYNC_RETRY.delay(n) for n in range(3)] == [30, 90, 150]


def test_ths_legacy_rows_match_historical_adata_fetcher():
    df = pd.DataFrame(
        [
            {"trade_date": "2024-06-03", "open": "1.0", "close": "1.1", "high": "1.2", "low": "0.9",
             "volume": "1000", "amount": float("nan"), "change": 0.1, "change_pct": float("nan")},
        ]
    )
    with mock.patch.object(adata.fund.market, "get_market_etf", return_value=df):
        result = RangePriceFetcher().fetch("159915.SZ", D(2024, 6, 1), D(2024, 6, 5), mode="legacy")
    assert result.price_basis == "ths_qfq" and result.full_history is True
    row = result.rows[0]
    assert row["date"] == D(2024, 6, 3) and row["close_price"] == 1.1 and row["volume"] == 1000
    assert row["amount"] is None and row["change"] == 0.1 and row["change_pct"] is None


def test_ths_ip_limit_exception_object_becomes_fetch_error():
    with mock.patch.object(adata.fund.market, "get_market_etf", return_value=Exception("limit")):
        with pytest.raises(FetchError):
            RangePriceFetcher().fetch("159915.SZ", D(2024, 6, 1), D(2024, 6, 5), mode="legacy")


def test_eastmoney_exchange_parses_raw_klines_and_asks_for_fqt0():
    payload = {"data": {"klines": [
        "2024-06-03,1.00,1.10,1.20,0.90,500,550000,0,10.0,0.10,1.5",
        "2024-06-04,1.10,1.12,1.15,1.05,300,330000,0,1.8,0.02,1.0",
        "2024-05-01,9,9,9,9,1,1,0,0,0,0",  # 区间外，丢弃
    ]}}
    response = mock.Mock(status_code=200)
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    with mock.patch("requests.get", return_value=response) as get:
        result = RangePriceFetcher().fetch("513100.SS", D(2024, 6, 3), D(2024, 6, 4))
    assert get.call_args.kwargs["params"]["fqt"] == "0"
    assert get.call_args.kwargs["params"]["secid"] == "1.513100"
    assert result.price_basis == "raw" and result.source == "eastmoney_exchange_price"
    assert [r["volume"] for r in result.rows] == [50000, 30000]  # 手 → 股


def test_fund_nav_trend_fallback_uses_beijing_dates():
    # 2024-06-04 00:00 北京时间 = 2024-06-03 16:00 UTC
    ts = int(dt.datetime(2024, 6, 3, 16, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    script = "var x=1; Data_netWorthTrend = " + json.dumps(
        [{"x": ts, "y": 1.2345, "equityReturn": 0.5}]
    ) + ";var y=2;"

    def fake_get(url, **kwargs):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        if "lsjz" in url:
            response.json.return_value = {"ErrCode": 0, "Data": {"LSJZList": []}}
        else:
            response.text = script
        return response

    with mock.patch("requests.get", side_effect=fake_get):
        result = RangePriceFetcher().fetch("005698", D(2024, 6, 1), D(2024, 6, 10))
    assert [r["date"] for r in result.rows] == [D(2024, 6, 4)]
    assert result.source == "eastmoney_fund_nav_trend_fallback"


def _fund_get(trend_points, lsjz_pages):
    """lsjz 按页返回 lsjz_pages[page-1]；trend 脚本返回 trend_points。记录请求顺序。"""
    calls = []

    def fake_get(url, **kwargs):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        if "lsjz" in url:
            page = kwargs["params"]["pageIndex"]
            calls.append(f"lsjz{page}")
            items = lsjz_pages[page - 1] if page <= len(lsjz_pages) else []
            response.json.return_value = {"ErrCode": 0, "Data": {"LSJZList": items}}
        else:
            calls.append("trend")
            response.text = "Data_netWorthTrend = " + json.dumps(trend_points) + ";"
        return response

    return fake_get, calls


def _ts(day):
    return int(dt.datetime(day.year, day.month, day.day, tzinfo=fetchers._SHANGHAI).timestamp() * 1000)


def test_long_fund_range_goes_straight_to_the_single_request_trend_script():
    points = [{"x": _ts(D(2010, 1, 4) + dt.timedelta(days=i)), "y": 1.0 + i / 1000, "equityReturn": 0.1} for i in range(5)]
    fake_get, calls = _fund_get(points, [])
    with mock.patch("requests.get", side_effect=fake_get):
        result = RangePriceFetcher().fetch("005698", D(2004, 6, 16), D(2026, 10, 30))
    assert calls == ["trend"] and len(result.rows) == 5


def test_truncated_lsjz_is_never_returned_as_complete():
    # 短区间，但接口一直有数据却翻不到 start（模拟页数上限）：必须回退 trend，而不是返回被截断的结果
    one_page = [{"FSRQ": "2026-10-30", "DWJZ": "2.0", "JZZZL": "0.1"}]
    fake_get, calls = _fund_get(
        [{"x": _ts(D(2026, 9, 1)), "y": 1.5, "equityReturn": 0}], [one_page] * 1000
    )
    with mock.patch("requests.get", side_effect=fake_get), mock.patch.object(fetchers, "MAX_NAV_PAGES", 3):
        result = RangePriceFetcher().fetch("005698", D(2026, 8, 1), D(2026, 10, 30))
    assert calls[:3] == ["lsjz1", "lsjz2", "lsjz3"] and calls[-1] == "trend"
    assert result.source == "eastmoney_fund_nav_trend_fallback"
