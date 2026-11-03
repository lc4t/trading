"""鉴权 / 权限 / 限流 / 健康检查 / 工具契约（经真实 Streamable HTTP 栈）。"""
import datetime as dt
import json
import logging

import pytest
from starlette.testclient import TestClient

from trading.market.auth import hash_token
from trading.market.config import load_config
from trading.market.server import create_app, required_scope
from trading.market.service import MarketService

from .conftest import cn_time, make_row

READ_TOKEN = "read-token-aaaaaaaaaaaaaaaaaaaaaaaa"
REFRESH_TOKEN = "refresh-token-bbbbbbbbbbbbbbbbbbbbbbb"
DB_PASSWORD = "s3cret-db-password"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def env(**overrides):
    tokens = [
        {"name": "reader", "scopes": ["read"], "sha256": hash_token(READ_TOKEN).hex()},
        {"name": "refresher", "scopes": ["refresh"], "token": REFRESH_TOKEN},
    ]
    base = {
        "DB_HOST": "db.invalid",
        "DB_USER": "u",
        "DB_PASSWORD": DB_PASSWORD,
        "DB_NAME": "trading",
        "MCP_TOKENS": json.dumps(tokens),
    }
    base.update(overrides)
    return base


@pytest.fixture
def make_client(cache, fetcher, store):
    def build(**overrides):
        config = load_config(env(**overrides))
        app = create_app(config, MarketService(cache), store.ping)
        return TestClient(app)

    return build


def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def call(client, token, name, arguments, id_=1):
    headers = dict(HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        "/mcp", headers=headers, content=json.dumps(rpc("tools/call", {"name": name, "arguments": arguments}, id_))
    )


def seed(fetcher, clock):
    clock.now = cn_time(2024, 6, 6, 9, 0)
    fetcher.set_rows("159915.SZ", [make_row("159915.SZ", dt.date(2024, 6, 4), 1.0)])
    fetcher.set_rows("005698", [make_row("005698", dt.date(2024, 6, 4), 2.0)])


ARGS = {"symbols": ["159915.SZ"], "start_date": "2024-06-04", "end_date": "2024-06-04"}


def test_missing_and_wrong_token_get_401(make_client, fetcher, clock):
    seed(fetcher, clock)
    with make_client() as client:
        assert call(client, None, "get_price_history", ARGS).status_code == 401
        assert call(client, "wrong-token-cccccccccccccccccccccc", "get_price_history", ARGS).status_code == 401
        bad_scheme = client.post("/mcp", headers={**HEADERS, "Authorization": "Basic abc"}, content="{}")
        assert bad_scheme.status_code == 401
        assert bad_scheme.headers["www-authenticate"] == "Bearer"
    assert fetcher.calls == []


def test_read_token_can_read_but_not_refresh(make_client, fetcher, clock):
    seed(fetcher, clock)
    with make_client() as client:
        ok = call(client, READ_TOKEN, "get_price_history", ARGS)
        assert ok.status_code == 200
        body = ok.json()["result"]["structuredContent"]
        assert body["count"] == 1 and body["data"][0]["price_basis"] == "raw"
        assert set(body) >= {"cache", "data_fingerprint", "count", "data"}
        row = body["data"][0]
        assert {"as_of", "fetched_at", "source", "is_final", "price_basis"} <= set(row)
        assert row["as_of"] == "2024-06-04T15:00:00+08:00"

        forced = call(client, READ_TOKEN, "get_price_history", {**ARGS, "refresh": "always"})
        assert forced.status_code == 403
        assert forced.json() == {"error": "forbidden", "required_scope": "refresh"}
        nav = call(client, READ_TOKEN, "refresh_fund_nav_cache",
                   {"fund_code": "005698", "start_date": "2024-06-04", "end_date": "2024-06-04"})
        assert nav.status_code == 403
        # refresh=missing / never 仍然允许
        assert call(client, READ_TOKEN, "get_price_history", {**ARGS, "refresh": "never"}).status_code == 200


def test_refresh_token_can_force_and_implies_read(make_client, fetcher, clock):
    seed(fetcher, clock)
    with make_client() as client:
        assert call(client, REFRESH_TOKEN, "get_price_history", {**ARGS, "refresh": "always"}).status_code == 200
        assert call(client, REFRESH_TOKEN, "get_price_history", ARGS).status_code == 200
        nav = call(client, REFRESH_TOKEN, "refresh_fund_nav_cache",
                   {"fund_code": "005698", "start_date": "2024-06-04", "end_date": "2024-06-04"})
        assert nav.status_code == 200


def test_batch_with_forced_refresh_needs_refresh_scope(make_client, fetcher, clock):
    seed(fetcher, clock)
    batch = [
        rpc("tools/call", {"name": "get_price_history", "arguments": ARGS}, 1),
        rpc("tools/call", {"name": "get_price_history", "arguments": {**ARGS, "refresh": "always"}}, 2),
    ]
    with make_client() as client:
        resp = client.post(
            "/mcp", headers={**HEADERS, "Authorization": f"Bearer {READ_TOKEN}"}, content=json.dumps(batch)
        )
        assert resp.status_code == 403


def test_refresh_calls_are_rate_limited_per_token(make_client, fetcher, clock):
    seed(fetcher, clock)
    with make_client(MCP_RATE_LIMIT_REFRESH_PER_MIN="2") as client:
        forced = {**ARGS, "refresh": "always"}
        assert call(client, REFRESH_TOKEN, "get_price_history", forced).status_code == 200
        assert call(client, REFRESH_TOKEN, "get_price_history", forced).status_code == 200
        limited = call(client, REFRESH_TOKEN, "get_price_history", forced)
        assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
        # 普通读不受 refresh 限流影响
        assert call(client, REFRESH_TOKEN, "get_price_history", ARGS).status_code == 200
        # 限流按 token，另一个 token 不受影响
        assert call(client, READ_TOKEN, "get_price_history", ARGS).status_code == 200


def test_health_needs_no_token_and_leaks_nothing(make_client, store, monkeypatch):
    with make_client() as client:
        ok = client.get("/healthz")
        assert ok.status_code == 200 and ok.json() == {"status": "ok"}
    monkeypatch.setattr(store, "ping", lambda: (_ for _ in ()).throw(RuntimeError(f"cannot connect {DB_PASSWORD}")))
    config = load_config(env())
    app = create_app(config, MarketService(None), lambda: store.ping())
    with TestClient(app) as client:
        bad = client.get("/healthz")
        assert bad.status_code == 503 and bad.json() == {"status": "unavailable"}
        assert DB_PASSWORD not in bad.text
        # 其它路径一律要 token
        assert client.get("/other").status_code == 401


def test_tools_list_is_market_only(make_client):
    with make_client() as client:
        resp = client.post(
            "/mcp", headers={**HEADERS, "Authorization": f"Bearer {READ_TOKEN}"},
            content=json.dumps(rpc("tools/list")),
        )
        names = {t["name"] for t in resp.json()["result"]["tools"]}
        assert {"get_price_history", "get_latest_price", "get_fund_nav_history",
                "get_latest_fund_nav", "refresh_fund_nav_cache"} <= names
        assert not any("discipline" in n or "holding" in n for n in names)


def test_latest_and_fund_tools(make_client, fetcher, clock):
    seed(fetcher, clock)
    with make_client() as client:
        latest = call(client, READ_TOKEN, "get_latest_price", {"symbol": "159915.SZ"}).json()
        data = latest["result"]["structuredContent"]["data"]
        assert data["close_price"] == 1.0 and data["is_final"] is True
        nav = call(client, READ_TOKEN, "get_latest_fund_nav", {"fund_code": "005698"}).json()
        assert nav["result"]["structuredContent"]["series_type"] == "fund_nav"


def test_invalid_symbol_is_rejected_by_whitelist(make_client):
    with make_client() as client:
        resp = call(client, READ_TOKEN, "get_latest_price", {"symbol": "../etc/passwd"}).json()
        assert resp["result"]["isError"] is True


def test_secrets_never_reach_logs(make_client, fetcher, clock, caplog):
    seed(fetcher, clock)
    caplog.set_level(logging.DEBUG)
    with make_client() as client:
        call(client, READ_TOKEN, "get_price_history", ARGS)
        call(client, "wrong-token-cccccccccccccccccccccc", "get_price_history", ARGS)
        call(client, READ_TOKEN, "get_price_history", {**ARGS, "refresh": "always"})
    text = caplog.text
    for secret in (READ_TOKEN, REFRESH_TOKEN, DB_PASSWORD, "wrong-token-cccccccccccccccccccccc"):
        assert secret not in text


def test_required_scope_rules():
    assert required_scope(rpc("initialize")) is None
    assert required_scope(rpc("tools/list")) is None
    assert required_scope(rpc("tools/call", {"name": "get_latest_price", "arguments": {}})) == "read"
    assert required_scope(
        rpc("tools/call", {"name": "get_latest_price", "arguments": {"refresh": "always"}})
    ) == "refresh"
    assert required_scope(rpc("tools/call", {"name": "refresh_fund_nav_cache", "arguments": {}})) == "refresh"
    assert required_scope(rpc("tools/call", {"name": "run_dual_ma_backtest", "arguments": {}})) == "backtest"
    assert required_scope("garbage") is None


def test_unexpected_tool_errors_return_generic_message_but_log_traceback(make_client, cache, caplog, monkeypatch):
    seed_ok = None  # noqa: F841
    monkeypatch.setattr(cache, "get_latest", lambda *a, **k: (_ for _ in ()).throw(KeyError("secret-detail")))
    caplog.set_level(logging.ERROR)
    with make_client() as client:
        result = call(client, READ_TOKEN, "get_latest_price", {"symbol": "159915.SZ"}).json()["result"]
    assert result["isError"] is True and "internal error (id=" in result["content"][0]["text"]
    assert "secret-detail" not in result["content"][0]["text"]
    record = next(r for r in caplog.records if "tool failed" in r.getMessage())
    assert record.exc_info and record.exc_info[0] is KeyError  # 服务端日志里有完整堆栈


def test_response_carries_server_timing_and_logs_it(make_client, fetcher, clock, caplog):
    seed(fetcher, clock)
    caplog.set_level(logging.INFO, logger="trading.market.server")
    with make_client() as client:
        out = call(client, READ_TOKEN, "get_price_history", {**ARGS, "symbols": ["159915.SZ"]}).json()["result"]
    timing_info = out["structuredContent"]["timing"]
    assert timing_info["server_ms"] > 0 and timing_info["db_ops"] >= 1 and timing_info["db_statements"] >= 1
    assert timing_info["upstream_calls"] == 1 and timing_info["upstream_ms"] >= 0  # 首次要回源
    assert timing_info["symbols"][0]["symbol"] == "159915.SZ" and timing_info["symbols"][0]["refreshed"] is True
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("tool=get_price_history"))
    assert "db_ms=" in line and "upstream_ms=" in line and READ_TOKEN not in caplog.text
    with make_client() as client:  # 第二次命中缓存：不再回源
        again = call(client, READ_TOKEN, "get_price_history", ARGS).json()["result"]["structuredContent"]["timing"]
    assert again["upstream_calls"] == 0


def test_text_content_is_compact_json_and_structured_content_is_primary(make_client, fetcher, clock):
    seed(fetcher, clock)
    with make_client() as client:
        result = call(client, READ_TOKEN, "get_price_history", ARGS).json()["result"]
    text = result["content"][0]["text"]
    assert "\n" not in text and ": " not in text.split('"data"')[0]  # 紧凑 JSON，没有缩进版
    assert json.loads(text) == result["structuredContent"]


def test_summary_mode_keeps_only_a_one_line_text(make_client, fetcher, clock):
    seed(fetcher, clock)
    with make_client(MCP_TEXT_CONTENT="summary") as client:
        result = call(client, READ_TOKEN, "get_price_history", ARGS).json()["result"]
    assert result["content"][0]["text"].startswith("ok; full result in structuredContent")
    assert result["structuredContent"]["count"] == 1


def test_invalid_text_content_mode_rejected():
    from trading.market.errors import ConfigError

    with pytest.raises(ConfigError):
        load_config(env(MCP_TEXT_CONTENT="xml"))


def test_internal_error_id_matches_the_logged_traceback(make_client, cache, caplog, monkeypatch):
    import re

    monkeypatch.setattr(cache, "get_latest", lambda *a, **k: (_ for _ in ()).throw(KeyError("boom")))
    caplog.set_level(logging.ERROR)
    with make_client() as client:
        text = call(client, READ_TOKEN, "get_latest_price", {"symbol": "159915.SZ"}).json()["result"]["content"][0]["text"]
    error_id = re.search(r"id=([0-9a-f]{8})", text).group(1)
    assert any(f"id={error_id}" in r.getMessage() and r.exc_info for r in caplog.records)


def test_tool_descriptions_tell_agents_how_to_handle_rate_limiting(make_client):
    with make_client() as client:
        tools = client.post(
            "/mcp", headers={**HEADERS, "Authorization": f"Bearer {READ_TOKEN}"}, content=json.dumps(rpc("tools/list"))
        ).json()["result"]["tools"]
    market = [t for t in tools if t["name"] in {"get_price_history", "get_latest_price", "get_fund_nav_history",
                                                "get_latest_fund_nav", "refresh_fund_nav_cache"}]
    assert len(market) == 5
    for tool in market:
        assert "rate_limited" in tool["description"] and "retry ONCE" in tool["description"] and "never loop" in tool["description"]
