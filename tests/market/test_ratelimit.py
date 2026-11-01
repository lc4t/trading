"""限流保护：冷却（熔断）、请求间隔、single-flight、对外契约（tail_refresh_error=rate_limited + retry_after）."""
import datetime as dt
import json
import threading
import time

import pytest
from starlette.testclient import TestClient

from trading.market.auth import hash_token
from trading.market.cache import MarketCache
from trading.market.config import load_config
from trading.market.errors import RateLimited
from trading.market.server import create_app
from trading.market.service import MarketService, RateLimitedError
from trading.market.upstream import GuardedFetcher, UpstreamGuard, UpstreamPolicy, source_for

from .conftest import Clock, FakeFetcher, cn_time, make_row

SYM = "AAPL"  # 走 Yahoo
D = dt.date
POLICY = {"yahoo": UpstreamPolicy(base_cooldown=100.0, max_cooldown=1000.0, min_interval=1.0)}


def make_guard(clock, sleeps=None, store=None, **kw):
    return UpstreamGuard(
        policies=POLICY,
        now=clock,
        monotonic=clock.monotonic,
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
        rng=lambda: 0.5,  # 抖动取中值：冷却 = 基准 × 1.0
        store=store,
        **kw,
    )


# ------------------------------------------------------------------- guard
def test_source_routing():
    assert source_for("AAPL") == "yahoo" and source_for("0700.HK") == "yahoo" and source_for("000300.SS") == "yahoo"
    assert source_for("159915.SZ") == "eastmoney" and source_for("159915.SZ", "legacy") == "ths"
    assert source_for("005698") == "eastmoney_fund"


def test_cooldown_blocks_all_upstream_requests_then_recovers(clock):
    guard = make_guard(clock)
    guard.acquire("yahoo")
    assert guard.trip("yahoo") == pytest.approx(100)
    with pytest.raises(RateLimited) as exc:
        guard.acquire("yahoo")
    assert 99 <= exc.value.retry_after <= 100 and exc.value.source == "yahoo"
    guard.acquire("eastmoney")  # 别的来源不受影响
    clock.advance(seconds=101)
    guard.acquire("yahoo")
    assert guard.remaining("yahoo") == 0


def test_cooldown_grows_exponentially_with_cap_and_honours_retry_after_and_resets_on_success(clock):
    guard = make_guard(clock)
    durations = [guard.trip("yahoo") for _ in range(5)]
    assert durations[:3] == pytest.approx([100, 200, 400]) and max(durations) <= 1200  # 上限（含抖动余量）
    guard.success("yahoo")
    assert guard.trip("yahoo") == pytest.approx(100)  # 成功一次清零
    assert guard.trip("yahoo", retry_after=5000) <= 1200  # 上游给的 Retry-After 也不能超过上限


def test_cooldown_has_jitter():
    values = set()
    for r in (0.0, 0.5, 1.0):
        clock = Clock(cn_time(2024, 6, 5, 10))
        g = UpstreamGuard(policies=POLICY, now=clock, monotonic=clock.monotonic, rng=lambda r=r: r)
        values.add(round(g.trip("yahoo"), 1))
    assert values == {80.0, 100.0, 120.0}  # ±20%


def test_requests_are_spaced_and_queue_is_bounded(clock):
    sleeps = []
    guard = make_guard(clock, sleeps)
    guard.acquire("yahoo")  # 第一个立刻放行
    guard.acquire("yahoo")  # 第二个要等一个间隔
    assert sleeps and 1.0 <= sleeps[0] <= 1.3
    with pytest.raises(RateLimited) as exc:  # 排队等待超过上限：不再排队，直接让调用方稍后再来
        for _ in range(10):
            guard.acquire("yahoo")
    assert "queue" in str(exc.value) and max(sleeps) <= 5.0  # 请求路径上的 sleep 有上限


def test_cooldown_persists_across_instances_via_store(store, clock):
    first = make_guard(clock, store=store)
    first.trip("yahoo")
    restarted = make_guard(clock, store=store)  # 模拟重启 / 另一个实例
    with pytest.raises(RateLimited):
        restarted.acquire("yahoo")
    restarted.success("yahoo")
    clock.advance(seconds=10)  # 越过同步间隔
    third = make_guard(clock, store=store)
    third.acquire("yahoo")  # 成功清零后别的实例也恢复


def test_persistence_failure_falls_back_to_in_memory(clock):
    class BrokenStore:
        read_only = False

        def load_cooldowns(self):
            raise RuntimeError("db down")

        def save_cooldown(self, *a):
            raise RuntimeError("db down")

    guard = make_guard(clock, store=BrokenStore())
    guard.trip("yahoo")
    with pytest.raises(RateLimited):
        guard.acquire("yahoo")


# ------------------------------------------------------ cache + guarded fetcher
@pytest.fixture
def stack(store, clock):
    fetcher = FakeFetcher()
    guard = make_guard(clock)
    cache = MarketCache(store, GuardedFetcher(fetcher, guard), now=clock, monotonic=clock.monotonic,
                        tail_check_ttl=600, intraday_ttl=300, failure_backoff=60)
    return cache, fetcher, guard


def seed(fetcher, clock):
    clock.now = cn_time(2024, 6, 6, 9, 0)
    fetcher.set_rows(SYM, [make_row(SYM, D(2024, 6, 3), 1.0), make_row(SYM, D(2024, 6, 4), 1.1)])


def test_rate_limited_tail_refresh_returns_cache_with_error_and_retry_after_and_stops_calling_upstream(stack, clock):
    cache, fetcher, guard = stack
    seed(fetcher, clock)
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 4))
    calls = len(fetcher.calls)

    fetcher.fail = RateLimited("yahoo", 30)
    clock.advance(seconds=700)  # 尾部检查到期
    first = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))
    assert first["count"] == 2 and first["tail_refresh_error"] == "rate_limited"
    assert first["retry_after"] >= 1
    assert len(fetcher.calls) == calls + 1

    for refresh in ("missing", "always"):  # 冷却期内：任何刷新都不再碰上游，但缓存照常返回并继续带 retry_after
        clock.advance(seconds=5)
        again = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5), refresh=refresh)
        assert again["count"] == 2 and again["tail_refresh_error"] == "rate_limited" and again["retry_after"] >= 1
    assert len(fetcher.calls) == calls + 1
    assert cache.get_latest(SYM, refresh="always")["tail_refresh_error"] == "rate_limited"

    fetcher.fail = None
    fetcher.set_rows(SYM, [make_row(SYM, D(2024, 6, 3), 1.0), make_row(SYM, D(2024, 6, 4), 1.1), make_row(SYM, D(2024, 6, 5), 1.2)])
    clock.advance(seconds=200)  # 冷却结束
    clock.now = cn_time(2024, 6, 6, 18, 0)
    recovered = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5), refresh="always")
    assert recovered["tail_refresh_error"] is None and recovered["count"] == 3


def test_retry_after_counts_down(stack, clock):
    cache, fetcher, _ = stack
    seed(fetcher, clock)
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 4))
    fetcher.fail = RateLimited("yahoo", 30)
    clock.advance(seconds=700)
    a = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5))["retry_after"]
    clock.advance(seconds=40)
    b = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 5), refresh="always")["retry_after"]
    assert b < a


def test_nothing_cached_and_rate_limited_gives_structured_error(stack, clock):
    cache, fetcher, _ = stack
    fetcher.fail = RateLimited("yahoo", 45)
    clock.now = cn_time(2024, 6, 6, 9, 0)
    payload = cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 4))  # 缓存层本身不抛
    assert payload["count"] == 0 and payload["tail_refresh_error"] == "rate_limited"
    with pytest.raises(RateLimitedError) as exc:
        MarketService(cache).price_history([SYM], D(2024, 6, 3), D(2024, 6, 4))
    body = exc.value.body()
    assert body["error"]["code"] == "rate_limited" and body["retry_after"] >= 1 and body["error"]["symbols"] == [SYM]
    with pytest.raises(RateLimitedError):
        MarketService(cache).latest_price(SYM)


def test_mixed_request_returns_cached_symbol_and_flags_the_limited_one(stack, clock, fetcher):
    cache, inner, guard = stack
    seed(inner, clock)
    cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 4))
    guard.trip("yahoo")
    out = MarketService(cache).price_history([SYM, "MSFT"], D(2024, 6, 3), D(2024, 6, 4), refresh="always")
    assert out["count"] == 2 and out["retry_after"] >= 1
    assert out["cache"]["MSFT"]["tail_refresh_error"] == "rate_limited" and out["cache"]["MSFT"]["cache_status"] == "empty"
    assert out["cache"][SYM]["retry_after"] >= 1


# ------------------------------------------------------------ single-flight
def test_concurrent_refreshes_of_one_symbol_are_coalesced(store, clock):
    gate = threading.Event()

    class SlowFetcher(FakeFetcher):
        def fetch(self, *a, **k):
            gate.wait(5)
            return super().fetch(*a, **k)

    fetcher = SlowFetcher()
    cache = MarketCache(store, fetcher, now=clock, monotonic=clock.monotonic)
    seed(fetcher, clock)
    results = []

    def worker():
        results.append(cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 4), refresh="always"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    time.sleep(0.3)  # 让所有线程都挂到同一个 flight 上
    gate.set()
    for t in threads:
        t.join(10)
    assert len(results) == 8 and all(r["count"] == 2 for r in results)
    assert len(fetcher.calls) == 1  # 8 个并发的 always 只打了一次上游


def test_followers_share_leader_failure_without_extra_upstream_calls(store, clock):
    gate = threading.Event()

    class FailingFetcher(FakeFetcher):
        def fetch(self, *a, **k):
            self.calls.append(a)
            gate.wait(5)
            raise RateLimited("yahoo", 20)

    fetcher = FailingFetcher()
    cache = MarketCache(store, fetcher, now=clock, monotonic=clock.monotonic)
    clock.now = cn_time(2024, 6, 6, 9, 0)
    out = []
    threads = [threading.Thread(target=lambda: out.append(cache.get_range(SYM, D(2024, 6, 3), D(2024, 6, 4)))) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in threads:
        t.join(10)
    assert len(fetcher.calls) == 1 and all(o["tail_refresh_error"] == "rate_limited" for o in out)


# --------------------------------------------------------------------- HTTP
def test_http_contract_for_rate_limited_responses(stack, clock):
    cache, fetcher, guard = stack
    token = "rl-token-aaaaaaaaaaaaaaaaaaaaaaaa"
    config = load_config({
        "DB_HOST": "x", "DB_USER": "x", "DB_PASSWORD": "x", "DB_NAME": "x", "MCP_CONFIG_DIR": "/nonexistent",
        "MCP_TOKENS": json.dumps([{"name": "r", "scopes": ["read", "refresh"], "sha256": hash_token(token).hex()}]),
    })
    app = create_app(config, MarketService(cache), lambda: None)
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "Authorization": f"Bearer {token}"}

    def call(args, name="get_price_history"):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
        return client.post("/mcp", headers=headers, content=json.dumps(body)).json()["result"]

    seed(fetcher, clock)
    with TestClient(app) as client:
        ok = call({"symbols": [SYM], "start_date": "2024-06-03", "end_date": "2024-06-04"})
        assert not ok["isError"]
        guard.trip("yahoo")
        # 有缓存：正常结果，带 tail_refresh_error / retry_after
        cached = call({"symbols": [SYM], "start_date": "2024-06-03", "end_date": "2024-06-04", "refresh": "always"})
        sc = cached["structuredContent"]
        assert not cached["isError"] and sc["retry_after"] >= 1
        assert sc["cache"][SYM]["tail_refresh_error"] == "rate_limited" and sc["count"] == 2
        # 完全没有缓存：结构化错误（isError=true，error.code=rate_limited，带 retry_after），不是泛化异常
        empty = call({"symbols": ["MSFT"], "start_date": "2024-06-03", "end_date": "2024-06-04"})
        assert empty["isError"] is True
        err = empty["structuredContent"]["error"]
        assert err["code"] == "rate_limited" and err["retry_after"] >= 1 and err["symbols"] == ["MSFT"]
        assert json.loads(empty["content"][0]["text"])["error"]["code"] == "rate_limited"
        latest = call({"symbol": "MSFT"}, name="get_latest_price")
        assert latest["isError"] and latest["structuredContent"]["error"]["code"] == "rate_limited"
