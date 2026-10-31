"""Phase B：回测 MCP 工具（经真实 HTTP 栈 + SQLite 合成数据）。"""
import datetime as dt
import json
import subprocess
import sys

import numpy as np
import pytest
from starlette.testclient import TestClient

from trading.market.auth import hash_token
from trading.market.backtest_tools import BacktestTools, jsonable
from trading.market.config import load_config
from trading.market.server import create_app
from trading.market.service import MarketService

from .conftest import cn_time, make_row
from .test_server import call, env

BT_TOKEN = "backtest-token-dddddddddddddddddd"
READ_TOKEN = "read-token-aaaaaaaaaaaaaaaaaaaaaaaa"


def synthetic(store, symbols, start=dt.date(2022, 1, 3), end=dt.date(2023, 12, 29)):
    rng = np.random.default_rng(3)
    for sym in symbols:
        price, rows, day = 10.0, [], start
        while day <= end:
            if day.weekday() < 5:
                price *= 1 + rng.normal(0.0004, 0.012)
                rows.append(make_row(sym, day, round(price, 4)))
            day += dt.timedelta(days=1)
        # 虚构数据：这里只关心回测链路，用 raw 口径标记
        store.upsert(rows, price_basis="raw", fetched_at=cn_time(2024, 1, 2, 9), final_flags={r["date"]: True for r in rows})


@pytest.fixture
def client(store, cache, clock, monkeypatch):
    clock.now = cn_time(2024, 1, 2, 9, 0)
    synthetic(store, ["AAA.SZ", "BBB.SS", "CCC.SZ", "000300.SS"])
    tokens = json.dumps([
        {"name": "bt", "scopes": ["read", "backtest"], "sha256": hash_token(BT_TOKEN).hex()},
        {"name": "reader", "scopes": ["read"], "sha256": hash_token(READ_TOKEN).hex()},
    ])
    config = load_config(env(MCP_TOKENS=tokens, MCP_BACKTEST_MAX_SYMBOLS="3", MCP_BACKTEST_MAX_SPAN_DAYS="1000"))
    app = create_app(config, MarketService(cache), store.ping, BacktestTools(store, cache, config))
    with TestClient(app) as c:
        yield c


def payload(resp):
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert not result.get("isError"), result
    return result["structuredContent"]


def test_read_scope_cannot_run_backtests(client):
    args = {"symbol": "AAA.SZ", "start_date": "2022-06-01", "end_date": "2023-06-01"}
    resp = call(client, READ_TOKEN, "run_dual_ma_backtest", args)
    assert resp.status_code == 403 and resp.json()["required_scope"] == "backtest"


def test_dual_ma_backtest_returns_metrics_trades_signal_and_data_quality(client):
    out = payload(call(client, BT_TOKEN, "run_dual_ma_backtest", {
        "symbol": "AAA.SZ", "start_date": "2022-03-01", "end_date": "2023-12-28",
        "ma_short": 5, "ma_long": 20, "use_chandelier": True, "trade_limit": 5,
    }))
    assert out["strategy"] == "dual_ma"
    assert {"metrics", "trades", "trade_count", "trades_truncated", "next_signal", "data_quality"} <= set(out)
    assert len(out["trades"]) <= 5 and out["trade_count"] >= len(out["trades"])
    assert out["data_quality"]["AAA.SZ"]["price_basis"] == "raw"
    json.dumps(out)  # 必须是纯 JSON


def test_momentum_topn_and_basket(client):
    topn = payload(call(client, BT_TOKEN, "run_momentum_topn_backtest", {
        "universe": ["AAA.SZ", "BBB.SS", "CCC.SZ"], "start_date": "2022-03-01", "end_date": "2023-12-28",
        "top_n": 1, "lookback": 20,
    }))
    assert topn["strategy"] == "momentum_topn" and topn["next_signal"]["target_weights"]
    basket = payload(call(client, BT_TOKEN, "run_momentum_basket_backtest", {
        "weights": {"AAA.SZ": 0.5, "BBB.SS": 0.5}, "start_date": "2022-03-01", "end_date": "2023-12-28",
        "lookback": 20, "schedule": "weekly:1",
    }))
    assert basket["strategy"] == "momentum_basket"
    assert set(basket["data_quality"]) == {"AAA.SZ", "BBB.SS", "000300.SS"}


def test_limits_and_validation(client):
    def err(name, args):
        result = call(client, BT_TOKEN, name, args).json()["result"]
        assert result["isError"] is True
        return result["content"][0]["text"]

    base = {"start_date": "2022-03-01", "end_date": "2023-12-28"}
    assert "too long" in err("run_dual_ma_backtest", {"symbol": "AAA.SZ", "start_date": "2010-01-01", "end_date": "2023-12-28"})
    assert "at most 3" in err("run_momentum_topn_backtest", {"universe": ["A.SZ", "B.SZ", "C.SZ", "D.SZ"], **base})
    assert "Invalid symbol" in err("run_dual_ma_backtest", {"symbol": "../x", **base})
    assert "schedule" in err("run_momentum_topn_backtest", {"universe": ["AAA.SZ"], "schedule": "hourly", **base})
    assert "ma_short" in err("run_dual_ma_backtest", {"symbol": "AAA.SZ", "ma_short": 30, "ma_long": 20, **base})
    assert "No data available" in err("run_dual_ma_backtest", {"symbol": "ZZZ.SZ", **base})


def test_concurrency_limit_rejects_when_all_slots_busy(store, cache):
    config = load_config(env(MCP_BACKTEST_CONCURRENCY="1"))
    tools = BacktestTools(store, cache, config)
    assert tools._slots.acquire(blocking=False)
    with pytest.raises(Exception) as exc:
        tools._guarded(lambda: {})
    assert "concurrent" in str(exc.value)
    tools._slots.release()
    assert tools._guarded(lambda: {"ok": 1}) == {"ok": 1}


def test_jsonable_handles_numpy_nan_and_dates():
    out = jsonable({"a": np.float64(1.5), "b": float("nan"), "c": dt.date(2024, 1, 2), "d": [np.int64(3)]})
    assert out == {"a": 1.5, "b": None, "c": "2024-01-02", "d": [3]}


def test_server_startup_import_does_not_load_heavy_backtest_dependencies():
    code = (
        "import sys, trading.cli.serve, trading.market.server, trading.market.backtest_tools, "
        "trading.market.cache, trading.market.service\n"
        "bad = [m for m in ('backtrader', 'matplotlib', 'trading.engine', 'trading.api') if m in sys.modules]\n"
        "assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
