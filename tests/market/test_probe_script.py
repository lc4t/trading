"""探测脚本：请求次数上限与重试规则（不访问网络）。"""
import importlib.util
import pathlib

SCRIPT = pathlib.Path(__file__).parents[2] / "scripts" / "probe" / "upstream_probe.py"
spec = importlib.util.spec_from_file_location("upstream_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def with_fetch(monkeypatch, fn):
    monkeypatch.setitem(probe.CANDIDATES, "fake", (fn, "测试口径", "备注"))
    return probe.run_one("fake", "AAPL", sleep=lambda s: None)


def test_success_is_a_single_request(monkeypatch):
    calls = []
    out = with_fetch(monkeypatch, lambda s, i: (calls.append(1) or (200, [("2026-11-17", 1, 2, 0.5, 1.5, 10)])))
    assert out["http"] == 200 and out["rows"] == 1 and out["last_date"] == "2026-11-17" and out["requests"] == 1 and len(calls) == 1


def test_rate_limit_is_never_retried(monkeypatch):
    calls = []

    def blocked(s, i):
        calls.append(1)
        raise probe.Blocked("HTTP 429")

    out = with_fetch(monkeypatch, blocked)
    assert out["http"] == 429 and len(calls) == 1 and "被拦" in out["error"]


def test_network_errors_and_5xx_are_retried_once_then_stop(monkeypatch):
    calls = []

    def flaky(s, i):
        calls.append(1)
        raise OSError("connection reset")

    out = with_fetch(monkeypatch, flaky)
    assert len(calls) == 2 and out["requests"] == 2 and out["http"] == "ERR"
    calls.clear()
    out = with_fetch(monkeypatch, lambda s, i: (calls.append(1) or (503, [])))
    assert len(calls) == 2 and out["http"] == 503  # 5xx 重试一次，最多 2 次


def test_unsupported_market_sends_no_request(monkeypatch):
    out = with_fetch(monkeypatch, lambda s, i: (_ for _ in ()).throw(probe.Unsupported("没有港股端点")))
    assert out["requests"] == 0 and "不支持" in out["error"]


def test_parsers_on_canned_payloads(monkeypatch):
    import datetime as dt
    import json

    monkeypatch.setattr(probe, "START", dt.date(2026, 1, 1))  # 解析器会按「近 30 天」过滤，固定窗口让测试不随日期漂移
    kline = {"data": {"klines": ["2026-11-17,386.6,376.0,386.6,376.0,31401095,1.1e10,2.7"]}}
    monkeypatch.setattr(probe, "http_get", lambda *a, **k: (200, json.dumps(kline)))
    status, rows = probe.eastmoney(0)("0700.HK", probe.SYMBOLS["0700.HK"])
    assert rows == [("2026-11-17", 386.6, 386.6, 376.0, 376.0, 31401095.0)]  # 东财的列顺序是 开、收、高、低
    tencent = {"data": {"hk00700": {"qfqday": [["2026-11-17", "386.6", "376.0", "386.6", "376.0", "31401095.0", {}]]}}}
    monkeypatch.setattr(probe, "http_get", lambda *a, **k: (200, json.dumps(tencent)))
    status, rows = probe.tencent_qfq("0700.HK", probe.SYMBOLS["0700.HK"])
    assert rows == [("2026-11-17", 386.6, 386.6, 376.0, 376.0, 31401095.0)]  # 腾讯的列顺序是 开、收、高、低、量
