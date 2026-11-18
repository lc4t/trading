"""备用上游抓取器：东财 fqt=0（港美股）、新浪美股、腾讯前复权。全部 mock requests，不访问网络。"""
import datetime as dt
import json
from unittest import mock

import pytest
import requests

from trading.market import fetchers
from trading.market.errors import FetchError, RateLimited
from trading.market.fetchers import (
    BACKUP_MAX_ATTEMPTS,
    RangePriceFetcher,
    SYNC_RETRY,
    backup_sources,
    overseas_kind,
    root_cause_name,
)

D = dt.date


def ok(payload=None, text=None):
    response = mock.Mock(status_code=200, headers={})
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    response.text = text if text is not None else json.dumps(payload)
    return response


def disconnect():
    """requests 对 http.client.RemoteDisconnected 的真实包装形态."""
    from http.client import RemoteDisconnected
    from urllib3.exceptions import ProtocolError

    return requests.exceptions.ConnectionError(ProtocolError("Connection aborted.", RemoteDisconnected("closed")))


def test_overseas_kind_and_source_order():
    assert overseas_kind("0700.HK") == "hk" and overseas_kind("9626.HK") == "hk"
    assert overseas_kind("AAPL") == "us" and overseas_kind("NVDA") == "us"
    for other in ("^HSI", "000300.SS", "159915.SZ", "005698", "BTC-USD", "GC=F"):
        assert overseas_kind(other) is None and backup_sources(other) == []
    assert backup_sources("0700.HK") == ["eastmoney_fqt0", "tencent_qfq"]  # 新浪没有港股端点
    assert backup_sources("AAPL") == ["sina_us", "eastmoney_fqt0", "tencent_qfq"]  # 前复权源永远排在最后


# ------------------------------------------------------------------ 东财 fqt=0
EM_PAYLOAD = {"data": {"klines": [
    "2024-06-03,500.0,502.0,505.0,499.0,1234567,620000000.0",  # 日期,开,收,高,低,量,额
    "2024-06-04,502.0,498.4,503.0,497.0,2000000,999000000.0",
    "2024-05-01,1,1,1,1,1,1",  # 区间外
]}}


def test_eastmoney_hk_uses_116_secid_fqt0_and_maps_columns_open_close_high_low():
    with mock.patch("requests.get", return_value=ok(EM_PAYLOAD)) as get:
        result = RangePriceFetcher().fetch_backup("0700.HK", D(2024, 6, 3), D(2024, 6, 4), "eastmoney_fqt0")
    params = get.call_args.kwargs["params"]
    assert params["secid"] == "116.00700" and params["fqt"] == "0" and params["klt"] == "101"
    assert params["beg"] == "20240603" and params["end"] == "20240604"
    assert result.source == "eastmoney_fqt0" and result.price_basis == "unadjusted"
    first = result.rows[0]
    assert (first["open_price"], first["close_price"], first["high"], first["low"]) == (500.0, 502.0, 505.0, 499.0)
    assert first["volume"] == 1234567  # 港美股是股数，不再乘 100
    assert first["amount"] == 620000000.0 and len(result.rows) == 2
    assert "Mozilla" in get.call_args.kwargs["headers"]["User-Agent"]  # 探测里验证过的浏览器 UA


def test_eastmoney_us_tries_nasdaq_then_nyse_prefix_only_when_empty():
    empty = ok({"data": None})
    with mock.patch("requests.get", side_effect=[empty, ok(EM_PAYLOAD)]) as get:
        result = RangePriceFetcher().fetch_backup("IBM", D(2024, 6, 3), D(2024, 6, 4), "eastmoney_fqt0")
    assert [c.kwargs["params"]["secid"] for c in get.call_args_list] == ["105.IBM", "106.IBM"]
    assert len(result.rows) == 2
    with mock.patch("requests.get", return_value=ok(EM_PAYLOAD)) as get:
        RangePriceFetcher().fetch_backup("AAPL", D(2024, 6, 3), D(2024, 6, 4), "eastmoney_fqt0")
    assert get.call_count == 1  # 105 有数据就不再试 106


def test_remote_disconnected_is_retried_at_most_twice_with_an_explicit_error():
    with mock.patch("requests.get", side_effect=[disconnect(), disconnect(), disconnect()]) as get, mock.patch(
        "time.sleep"
    ) as sleep:
        with pytest.raises(FetchError) as exc:
            RangePriceFetcher().fetch_backup("AAPL", D(2024, 6, 3), D(2024, 6, 4), "eastmoney_fqt0")
    assert get.call_count == BACKUP_MAX_ATTEMPTS == 2  # 绝不无限重试，也不会因为换交易所前缀而多打
    assert sleep.call_count == 1 and all(0.4 <= c.args[0] <= 0.8 for c in sleep.call_args_list)
    message = str(exc.value)
    assert "eastmoney_fqt0" in message and "2 attempt" in message and "RemoteDisconnected" in message


def test_backup_retry_cap_cannot_be_raised_by_the_caller():
    with mock.patch("requests.get", side_effect=disconnect()) as get, mock.patch("time.sleep"):
        with pytest.raises(FetchError):
            RangePriceFetcher().fetch_backup(
                "0700.HK", D(2024, 6, 3), D(2024, 6, 4), "eastmoney_fqt0", retry=SYNC_RETRY  # 批量同步的 10 次策略
            )
    assert get.call_count == 2


def test_http_429_on_a_backup_source_is_never_retried():
    response = mock.Mock(status_code=429, headers={"Retry-After": "45"})
    response.raise_for_status.side_effect = requests.HTTPError(response=response)
    for source, symbol in (("eastmoney_fqt0", "0700.HK"), ("sina_us", "AAPL"), ("tencent_qfq", "0700.HK")):
        with mock.patch("requests.get", return_value=response) as get:
            with pytest.raises(RateLimited) as exc:
                RangePriceFetcher().fetch_backup(symbol, D(2024, 6, 3), D(2024, 6, 4), source)
        assert get.call_count == 1 and exc.value.retry_after == 45 and exc.value.hinted


def test_root_cause_name_unwraps_requests_wrapping():
    assert root_cause_name(disconnect()) == "RemoteDisconnected"
    assert root_cause_name(ValueError("x")) == "ValueError"


def test_unparsable_eastmoney_response_is_a_clear_error_not_a_retry_storm():
    bad = mock.Mock(status_code=200, headers={})
    bad.raise_for_status.return_value = None
    bad.json.side_effect = ValueError("not json")
    with mock.patch("requests.get", return_value=bad) as get:
        with pytest.raises(FetchError, match="unparsable"):
            RangePriceFetcher().fetch_backup("0700.HK", D(2024, 6, 3), D(2024, 6, 4), "eastmoney_fqt0")
    assert get.call_count == 1


# ------------------------------------------------------------------ 新浪美股
SINA_BODY = (
    'var _x=([{"d":"2024-06-03","o":"190.0","h":"192.0","l":"189.0","c":"191.5","v":"50000000"},'
    '{"d":"2024-06-04","o":"191.5","h":"193.0","l":"190.0","c":"192.0","v":"40000000"},'
    '{"d":"1984-09-07","o":"26.0","h":"26.9","l":"26.0","c":"26.5","v":"1000"}]);'
)


def test_sina_us_parses_jsonp_and_trims_the_full_history_to_the_window():
    with mock.patch("requests.get", return_value=ok(text=SINA_BODY)) as get:
        result = RangePriceFetcher().fetch_backup("AAPL", D(2024, 6, 3), D(2024, 6, 4), "sina_us")
    assert get.call_args.kwargs["params"] == {"symbol": "aapl", "type": "daily"}
    assert get.call_args.kwargs["headers"]["Referer"] == "https://finance.sina.com.cn/"
    assert result.source == "sina_us" and result.price_basis == "unadjusted"
    assert [r["date"] for r in result.rows] == [D(2024, 6, 3), D(2024, 6, 4)]
    assert result.rows[0]["close_price"] == 191.5 and result.rows[0]["volume"] == 50000000


def test_sina_us_is_not_offered_for_hk_and_garbage_is_a_clear_error():
    with pytest.raises(FetchError):
        RangePriceFetcher().fetch_backup("0700.HK", D(2024, 6, 3), D(2024, 6, 4), "sina_us")
    with mock.patch("requests.get", return_value=ok(text="<html>blocked</html>")):
        with pytest.raises(FetchError, match="unparsable"):
            RangePriceFetcher().fetch_backup("AAPL", D(2024, 6, 3), D(2024, 6, 4), "sina_us")


# ------------------------------------------------------------------ 腾讯前复权
def tx_payload(key, field="qfqday"):
    return {"code": 0, "data": {key: {field: [
        ["2024-06-03", "500.0", "502.0", "505.0", "499.0", "1000", {"nd": "x"}],  # 日期,开,收,高,低,量,(分红说明)
        ["2024-06-04", "502.0", "498.4", "503.0", "497.0", "2000"],
    ]}}}


def test_tencent_hk_is_labelled_qfq_tencent_and_maps_columns():
    with mock.patch("requests.get", return_value=ok(tx_payload("hk00700"))) as get:
        result = RangePriceFetcher().fetch_backup("0700.HK", D(2024, 6, 3), D(2024, 6, 4), "tencent_qfq")
    assert get.call_args.kwargs["params"]["param"].startswith("hk00700,day,,,") and get.call_args.kwargs["params"]["param"].endswith(",qfq")
    assert result.price_basis == "qfq_tencent" and result.source == "tencent_qfq"
    row = result.rows[1]
    assert (row["open_price"], row["close_price"], row["high"], row["low"]) == (502.0, 498.4, 503.0, 497.0)


def test_tencent_us_tries_oq_then_n_suffix_and_falls_back_to_day_field():
    empty = ok({"data": {}})
    with mock.patch("requests.get", side_effect=[empty, ok(tx_payload("usIBM.N", field="day"))]) as get:
        result = RangePriceFetcher().fetch_backup("IBM", D(2024, 6, 3), D(2024, 6, 4), "tencent_qfq")
    assert [c.kwargs["params"]["param"].split(",")[0] for c in get.call_args_list] == ["usIBM.OQ", "usIBM.N"]
    assert len(result.rows) == 2


def test_unknown_backup_source_or_unsupported_symbol_is_rejected():
    with pytest.raises(FetchError):
        RangePriceFetcher().fetch_backup("^HSI", D(2024, 6, 3), D(2024, 6, 4), "eastmoney_fqt0")
    with pytest.raises(FetchError):
        RangePriceFetcher().fetch_backup("0700.HK", D(2024, 6, 3), D(2024, 6, 4), "nope")


def test_default_route_labels_for_backup_modes():
    assert fetchers.upstream_route_for_symbol("0700.HK", "unadjusted") == "backup_unadjusted_market_price"
    assert fetchers.upstream_route_for_symbol("0700.HK", "qfq_tencent") == "backup_tencent_qfq_market_price"
