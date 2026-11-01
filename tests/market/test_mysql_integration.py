"""真实 MySQL 上的端到端冒烟（伪造 fetcher）。默认跳过；用临时 MySQL 跑：

    TT_TEST_MYSQL_URL='mysql+pymysql://user:pw@127.0.0.1:3306/dbname' uv run pytest tests/market/test_mysql_integration.py

测试会 DROP 并重建 trading_data / symbol_info / 旁表，务必只指向一次性数据库。
"""
import datetime as dt
import json
import os

import pytest
from sqlalchemy import create_engine, inspect

from trading.data.repository import Base
from trading.market.auth import hash_token
from trading.market.cache import MarketCache
from trading.market.clock import to_iso
from trading.market.config import load_config
from trading.market.server import create_app
from trading.market.service import MarketService
from trading.market.store import MarketStore

from .conftest import Clock, FakeFetcher, cn_time, make_row

URL = os.environ.get("TT_TEST_MYSQL_URL")
pytestmark = pytest.mark.skipif(not URL, reason="需要 TT_TEST_MYSQL_URL 指向一次性 MySQL")

SYM = "159915.SZ"
D = dt.date
TOKEN = "mysql-e2e-token-aaaaaaaaaaaaaaaa"


@pytest.fixture
def mysql_stack():
    engine = create_engine(URL, pool_pre_ping=True)
    Base.metadata.drop_all(engine)
    store = MarketStore(engine)
    store.init_schema()
    clock = Clock(cn_time(2024, 6, 5, 10, 0))
    fetcher = FakeFetcher()
    cache = MarketCache(store, fetcher, now=clock, monotonic=clock.monotonic,
                        tail_check_ttl=600, intraday_ttl=300)
    yield store, cache, fetcher, clock
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_schema_includes_meta_tables(mysql_stack):
    store, *_ = mysql_stack
    tables = set(inspect(store.engine).get_table_names())
    assert {"trading_data", "symbol_info", "market_data_meta", "market_corporate_actions"} <= tables


def test_cache_hit_miss_and_intraday_repair_on_mysql(mysql_stack):
    store, cache, fetcher, clock = mysql_stack
    fetcher.set_rows(SYM, [make_row(SYM, D(2024, 6, 4), 1.1), make_row(SYM, D(2024, 6, 5), 1.15)])
    first = cache.get_range(SYM, D(2024, 6, 4), D(2024, 6, 5))
    assert first["count"] == 2 and first["data"][0]["is_final"] and not first["data"][1]["is_final"]
    cache.get_range(SYM, D(2024, 6, 4), D(2024, 6, 4))
    assert len(fetcher.calls) == 1  # 定稿行命中缓存

    clock.now = cn_time(2024, 6, 6, 9, 0)
    fetcher.set_rows(SYM, [make_row(SYM, D(2024, 6, 4), 9.9), make_row(SYM, D(2024, 6, 5), 1.2)])
    fixed = cache.get_range(SYM, D(2024, 6, 4), D(2024, 6, 5))  # end == 缓存最新日
    assert [r["close_price"] for r in fixed["data"]] == [1.1, 1.2]  # 6/4 不被改写，6/5 被修正
    assert fixed["data"][1]["is_final"] and to_iso(fixed["data"][1]["as_of"]) == "2024-06-05T15:00:00+08:00"
    assert store.series_mode(SYM) == "raw"


def test_token_auth_and_tools_over_http_on_mysql(mysql_stack):
    from starlette.testclient import TestClient

    store, cache, fetcher, clock = mysql_stack
    clock.now = cn_time(2024, 6, 6, 9, 0)
    fetcher.set_rows(SYM, [make_row(SYM, D(2024, 6, 4), 1.1)])
    env = {"DB_HOST": "x", "DB_USER": "x", "DB_PASSWORD": "x", "DB_NAME": "x",
           "MCP_TOKENS": json.dumps([{"name": "r", "scopes": ["read"], "sha256": hash_token(TOKEN).hex()}])}
    app = create_app(load_config(env), MarketService(cache), store.ping)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "get_price_history",
        "arguments": {"symbols": [SYM], "start_date": "2024-06-04", "end_date": "2024-06-04"}}}
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.post("/mcp", headers=headers, content=json.dumps(call)).status_code == 401
        ok = client.post("/mcp", headers={**headers, "Authorization": f"Bearer {TOKEN}"}, content=json.dumps(call))
        assert ok.status_code == 200 and ok.json()["result"]["structuredContent"]["count"] == 1


def test_a_read_costs_exactly_one_round_trip_on_mysql(mysql_stack):
    """读连接池：AUTOCOMMIT、归还不回滚、不 ping —— 一次读只向 MySQL 发一条命令."""
    import pymysql.connections as pc
    from sqlalchemy.engine import make_url

    from trading.market.config import DatabaseConfig

    store, cache, fetcher, clock = mysql_stack
    store.upsert([make_row(SYM, D(2024, 6, 4), 1.1)], price_basis="raw")
    u = make_url(URL)
    cfg = DatabaseConfig(u.host, u.port or 3306, u.username, u.password or "", u.database)
    fast = MarketStore(cfg.create_engine(), cfg.create_read_engine())
    fast.query_range(SYM, D(2024, 6, 1), D(2024, 6, 30))  # 预热建连
    sent = []
    original = pc.Connection._execute_command

    def counting(self, command, sql):
        sent.append(sql)
        return original(self, command, sql)

    pc.Connection._execute_command = counting
    try:
        rows = fast.query_range(SYM, D(2024, 6, 1), D(2024, 6, 30))
    finally:
        pc.Connection._execute_command = original
    assert len(rows) == 1 and len(sent) == 1
