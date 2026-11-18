"""收盘前不回源（awaiting_close）、intraday 显式选项、已定稿行永不回源、周末/跨时区边界."""
import datetime as dt
import json

import pytest
from starlette.testclient import TestClient

from trading.market.auth import hash_token
from trading.market.config import load_config
from trading.market.server import create_app
from trading.market.service import AwaitingCloseError, MarketService

from .conftest import cn_time, make_row

SYM = "159915.SZ"  # A 股：15:00 收盘，+30 分钟缓冲 → 15:30（北京时间）可定稿
D = dt.date


def seed_history(fetcher, symbol=SYM, days=(D(2024, 6, 3), D(2024, 6, 4)), close=1.0):
    fetcher.set_rows(symbol, [make_row(symbol, d, close + i / 10) for i, d in enumerate(days)])


def warm(cache, fetcher, clock, tail_ttl_elapsed=True):
    """周三 6/5 早晨之前，库里已有 6/3、6/4 的定稿行."""
    clock.now = cn_time(2024, 6, 5, 9, 0)
    seed_history(fetcher)
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 4))
    fetcher.calls.clear()
    if tail_ttl_elapsed:
        clock.mono += 3600  # 之后的请求距这次回源已经过了尾部检查间隔（测试里直接改 now 不会推进单调时钟）


# ---------------------------------------------------------------- 收盘前不回源
def test_before_close_default_request_for_today_makes_no_upstream_call_and_reports_awaiting_close(cache, fetcher, clock):
    warm(cache, fetcher, clock)
    clock.now = cn_time(2024, 6, 5, 14, 0)  # 周三 14:00，距可定稿 90 分钟
    payload = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert fetcher.calls == []  # 不回源，也就不会触发限流冷却
    assert payload["count"] == 2 and [r["date"] for r in payload["data"]] == [D(2024, 6, 3), D(2024, 6, 4)]
    assert payload["awaiting_close"] == {"date": "2024-06-05", "final_ready_at": "2024-06-05T15:30:00+08:00", "wait_seconds": 5400}
    assert payload["tail_refresh_error"] is None and payload["retry_after"] is None  # 这不是错误


def test_wait_seconds_counts_down(cache, fetcher, clock):
    warm(cache, fetcher, clock)
    clock.now = cn_time(2024, 6, 5, 15, 0)
    a = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))["awaiting_close"]["wait_seconds"]
    clock.advance(minutes=10)
    b = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))["awaiting_close"]["wait_seconds"]
    assert (a, b) == (1800, 1200) and fetcher.calls == []


def test_request_not_including_today_has_no_awaiting_close(cache, fetcher, clock):
    warm(cache, fetcher, clock)
    clock.now = cn_time(2024, 6, 5, 14, 0)
    payload = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 4))
    assert payload["awaiting_close"] is None and fetcher.calls == []


def test_empty_cache_before_close_fetches_history_only_up_to_yesterday(cache, fetcher, clock):
    clock.now = cn_time(2024, 6, 5, 14, 0)
    seed_history(fetcher, days=(D(2024, 6, 3), D(2024, 6, 4), D(2024, 6, 5)))
    payload = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert len(fetcher.calls) == 1 and fetcher.calls[0][2] == D(2024, 6, 4)  # 回源窗口的 end 是前一天，不是今天
    assert [r["date"] for r in payload["data"]] == [D(2024, 6, 3), D(2024, 6, 4)] and payload["awaiting_close"]


def test_only_today_requested_and_nothing_cached_gives_structured_awaiting_error_without_upstream(cache, fetcher, clock):
    clock.now = cn_time(2024, 6, 5, 14, 0)
    seed_history(fetcher, days=(D(2024, 6, 5),))
    with pytest.raises(AwaitingCloseError) as exc:
        MarketService(cache).price_history([SYM], D(2024, 6, 5), D(2024, 6, 5))
    assert fetcher.calls == []
    body = exc.value.body()["error"]
    assert body["code"] == "awaiting_close" and body["wait_seconds"] == 5400 and body["symbols"] == [SYM]
    with pytest.raises(AwaitingCloseError):
        MarketService(cache).latest_price(SYM)  # 库里也没有更早的行


def test_default_hides_a_cached_intraday_snapshot_of_today_and_never_refetches_it(cache, fetcher, clock):
    warm(cache, fetcher, clock)
    clock.now = cn_time(2024, 6, 5, 10, 0)
    fetcher.set_rows(SYM, [make_row(SYM, D(2024, 6, 5), 1.55)])
    cache.get_range(SYM, D(2024, 6, 5), D(2024, 6, 5), intraday=True)  # 库里有一行盘中快照
    assert len(fetcher.calls) == 1
    clock.advance(hours=3)  # 超过盘中 TTL
    payload = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert [r["date"] for r in payload["data"]] == [D(2024, 6, 3), D(2024, 6, 4)]  # 盘中快照不返回
    assert len(fetcher.calls) == 1 and payload["awaiting_close"]  # 也没有再去上游刷新它


def test_refresh_always_before_close_does_not_pull_the_intraday_row(cache, fetcher, clock):
    warm(cache, fetcher, clock)
    clock.now = cn_time(2024, 6, 5, 14, 0)
    seed_history(fetcher, days=(D(2024, 6, 3), D(2024, 6, 4), D(2024, 6, 5)))
    payload = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5), refresh="always")
    assert fetcher.calls[-1][2] == D(2024, 6, 4) and payload["count"] == 2


def test_refresh_never_also_hides_today_snapshot(cache, fetcher, clock):
    warm(cache, fetcher, clock)
    clock.now = cn_time(2024, 6, 5, 10, 0)
    fetcher.set_rows(SYM, [make_row(SYM, D(2024, 6, 5), 1.55)])
    cache.get_range(SYM, D(2024, 6, 5), D(2024, 6, 5), intraday=True)
    payload = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5), refresh="never")
    assert payload["count"] == 2 and payload["awaiting_close"]


# ---------------------------------------------------------------------- latest
def test_latest_before_close_returns_previous_final_row_with_message(cache, fetcher, clock):
    warm(cache, fetcher, clock)
    clock.now = cn_time(2024, 6, 5, 14, 0)
    payload = cache.get_latest(SYM)
    assert fetcher.calls == [] and payload["data"]["date"] == D(2024, 6, 4) and payload["data"]["is_final"] is True
    assert payload["awaiting_close"]["wait_seconds"] == 5400 and payload["tail_refresh_error"] is None
    msg = payload["awaiting_close_message"]
    assert "2024-06-05" in msg and "15:30" in msg and "1 小时 30 分钟" in msg and "2024-06-04" in msg and "intraday=true" in msg


def test_latest_intraday_true_keeps_the_old_behaviour(cache, fetcher, clock):
    warm(cache, fetcher, clock)
    clock.now = cn_time(2024, 6, 5, 10, 0)
    fetcher.set_rows(SYM, [make_row(SYM, D(2024, 6, 4), 1.1), make_row(SYM, D(2024, 6, 5), 1.55)])
    payload = cache.get_latest(SYM, intraday=True)
    assert payload["data"]["date"] == D(2024, 6, 5) and payload["data"]["is_final"] is False
    assert payload["awaiting_close"] is None and len(fetcher.calls) == 1


# ------------------------------------------------------------------- intraday=true
def test_intraday_true_is_exactly_the_previous_behaviour(cache, fetcher, clock):
    warm(cache, fetcher, clock)
    clock.now = cn_time(2024, 6, 5, 10, 0)
    fetcher.set_rows(SYM, [make_row(SYM, D(2024, 6, 3), 1.0), make_row(SYM, D(2024, 6, 4), 1.1), make_row(SYM, D(2024, 6, 5), 1.55)])
    first = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5), intraday=True)
    assert first["count"] == 3 and first["data"][-1]["is_final"] is False and first["awaiting_close"] is None
    assert len(fetcher.calls) == 1 and fetcher.calls[0][2] == D(2024, 6, 5)  # 窗口一直到今天
    clock.advance(seconds=100)
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5), intraday=True)
    assert len(fetcher.calls) == 1  # TTL 内不重抓
    clock.advance(seconds=400)
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5), intraday=True)
    assert len(fetcher.calls) == 2  # 超过 TTL 重抓


# ---------------------------------------------------------- 过了可定稿时刻 / 已定稿
def test_after_final_ready_it_fetches_once_then_never_again(cache, fetcher, clock):
    warm(cache, fetcher, clock, tail_ttl_elapsed=False)  # 上一次回源在早上，距现在不到 10 分钟的 TTL 也不重要：刚过 15:30 就该去取
    clock.now = cn_time(2024, 6, 5, 15, 29)
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert fetcher.calls == []  # 还差 1 分钟，仍然不回源（TTL 也没到）
    clock.now = cn_time(2024, 6, 5, 15, 31)
    fetcher.set_rows(SYM, [make_row(SYM, D(2024, 6, 4), 1.1), make_row(SYM, D(2024, 6, 5), 1.2)])
    payload = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert len(fetcher.calls) == 1 and payload["awaiting_close"] is None
    assert payload["data"][-1]["date"] == D(2024, 6, 5) and payload["data"][-1]["is_final"] is True
    for refresh in ("missing", "missing", "never"):  # 已定稿的当日行：永远不回源
        clock.advance(hours=2)
        cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5), refresh=refresh)
        cache.get_latest(SYM, refresh=refresh)
    assert len(fetcher.calls) == 1


def test_finalized_today_row_in_the_store_means_zero_upstream_requests(cache, store, fetcher, clock):
    clock.now = cn_time(2024, 6, 5, 20, 0)
    store.upsert([make_row(SYM, D(2024, 6, 5), 1.2)], price_basis="ths_qfq", fetched_at=cn_time(2024, 6, 5, 16, 0),
                 final_flags={D(2024, 6, 5): True})
    clock.advance(days=30)
    clock.now = cn_time(2024, 6, 5, 20, 5)
    payload = cache.get_range(SYM, D(2024, 6, 5), D(2024, 6, 5))
    assert fetcher.calls == [] and payload["count"] == 1 and payload["awaiting_close"] is None


# ---------------------------------------------------------------------- 边界
def test_weekend_is_not_a_trading_day_so_there_is_no_waiting(cache, fetcher, clock):
    clock.now = cn_time(2024, 6, 7, 20, 0)  # 周五晚，已收盘
    seed_history(fetcher, days=(D(2024, 6, 6), D(2024, 6, 7)))
    cache.get_range(SYM, D(2024, 6, 6), D(2024, 6, 7))
    fetcher.calls.clear()
    clock.now = cn_time(2024, 6, 8, 10, 0)  # 周六 10:00：不是交易日，没有「等收盘」
    payload = cache.get_range(SYM, D(2024, 6, 6), D(2024, 6, 8))
    assert payload["awaiting_close"] is None
    assert cache.get_latest(SYM)["awaiting_close"] is None


def test_us_symbol_uses_the_new_york_trading_day_not_the_shanghai_date(cache, fetcher, clock):
    us = "AAPL"
    # 北京时间周三 02:00 = 纽约周二 13:00（盘中）：「当日」是周二，可定稿 = 纽约 16:30 = 北京 05:30
    clock.now = cn_time(2024, 6, 5, 2, 0)
    fetcher.set_rows(us, [make_row(us, D(2024, 6, 3), 190.0), make_row(us, D(2024, 6, 4), 191.0)])
    cache.get_range(us, D(2024, 5, 31), D(2024, 6, 4), intraday=True)  # 先备一份历史（周二那行是盘中快照）
    fetcher.calls.clear()
    payload = cache.get_range(us, D(2024, 6, 3), D(2024, 6, 5))
    assert payload["awaiting_close"]["date"] == "2024-06-04"
    assert payload["awaiting_close"]["final_ready_at"] == "2024-06-05T04:30:00+08:00"  # 夏令时：纽约 EDT = UTC-4
    assert payload["awaiting_close"]["wait_seconds"] == 9000
    assert [r["date"] for r in payload["data"]] == [D(2024, 6, 3)]  # 周二盘中快照不返回
    clock.now = cn_time(2024, 6, 5, 5, 0)  # 过了纽约 16:30
    assert cache.get_range(us, D(2024, 6, 3), D(2024, 6, 5))["awaiting_close"] is None


def test_weekday_holiday_is_a_known_boundary_it_is_treated_like_a_trading_day(cache, fetcher, clock):
    """没有交易日历：工作日的节假日会被当成「等收盘」——只是 awaiting_close 多说一句，不影响数据."""
    warm(cache, fetcher, clock)
    clock.now = cn_time(2024, 6, 5, 11, 0)
    payload = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert payload["awaiting_close"] and payload["count"] == 2


# --------------------------------------------------------------------- HTTP
@pytest.fixture
def client(cache, store, fetcher, clock):
    token = "awaiting-token-aaaaaaaaaaaaaaaaaaaa"
    config = load_config({
        "DB_HOST": "x", "DB_USER": "x", "DB_PASSWORD": "x", "DB_NAME": "x", "MCP_CONFIG_DIR": "/nonexistent",
        "MCP_TOKENS": json.dumps([{"name": "r", "scopes": ["read"], "sha256": hash_token(token).hex()}]),
    })
    app = create_app(config, MarketService(cache), lambda: None)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "Authorization": f"Bearer {token}"}

    def call(name, args):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
        return c.post("/mcp", headers=headers, content=json.dumps(body)).json()["result"]

    with TestClient(app) as c:
        c.call, c.headers_ = call, headers
        yield c


def test_http_contract(client, cache, fetcher, clock):
    warm(cache, fetcher, clock)
    clock.now = cn_time(2024, 6, 5, 14, 0)
    out = client.call("get_price_history", {"symbols": [SYM], "start_date": "2024-06-03", "end_date": "2024-06-05"})
    entry = out["structuredContent"]["cache"][SYM]
    assert not out["isError"] and entry["awaiting_close"]["wait_seconds"] == 5400 and entry["tail_refresh_error"] is None
    latest = client.call("get_latest_price", {"symbol": SYM})["structuredContent"]
    assert latest["awaiting_close"]["final_ready_at"] == "2024-06-05T15:30:00+08:00" and "1 小时 30 分钟" in latest["awaiting_close_message"]
    assert fetcher.calls == []
    # A 股只请求当日、库里没有：结构化的 awaiting_close 错误
    none_cached = client.call("get_price_history", {"symbols": ["588000.SS"], "start_date": "2024-06-05", "end_date": "2024-06-05"})
    assert none_cached["isError"] is True and none_cached["structuredContent"]["error"]["code"] == "awaiting_close"


def test_tool_descriptions_and_schema_mention_intraday(client):
    tools = client.post("/mcp", headers=client.headers_, content=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})).json()["result"]["tools"]
    by_name = {t["name"]: t for t in tools}
    for name in ("get_price_history", "get_latest_price"):
        assert "intraday" in by_name[name]["inputSchema"]["properties"] and "intraday" in by_name[name]["description"]
        assert by_name[name]["inputSchema"]["properties"]["intraday"].get("default") is False


def test_first_request_right_after_the_close_buffer_bypasses_the_tail_ttl_but_only_once(cache, fetcher, clock):
    warm(cache, fetcher, clock, tail_ttl_elapsed=False)
    clock.now = cn_time(2024, 6, 5, 15, 31)
    seed_history(fetcher, days=(D(2024, 6, 4),))  # 上游暂时没有当日行（例如节假日、或还没入库）
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert len(fetcher.calls) == 1
    clock.advance(seconds=30)  # 同一个时间窗口里不会反复回源，之后只按普通的 TTL 走
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert len(fetcher.calls) == 1
