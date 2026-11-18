#!/usr/bin/env python3
"""限流冷却演练：真实起 trading-mcp 进程 + 临时 MySQL + 本地假上游，逐步观察冷却行为.

    uv run python scripts/regression/ratelimit_drill.py [--client-src PATH] [--real-yahoo-probes 3] [--keep]

覆盖：命中限流后进入冷却且冷却期内假上游收到 0 个请求；有缓存 / 没缓存两种返回形态；冷却时长指数增长、
带抖动、有上限，成功后清零；``market_upstream_state`` 在重启后保持冷却；两个实例共享冷却；同一标的并发请求
合并成一次；（可选）AI 侧客户端按 ``retry_after`` 等待、次数和总时长有上限、不无限重试。

需要 docker（临时 MySQL，随机临时凭据，结束即删）。不访问行情网络，除非显式加 ``--real-yahoo-probes N``
（N 次很小量的真实 Yahoo 请求，用来确认真实路径可用）。每一步都打印观察到的数值。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_upstream import FakeUpstream  # noqa: E402
from run import Mysql, free_port  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
TODAY = dt.date.today()
START, END = (TODAY - dt.timedelta(days=30)).isoformat(), (TODAY - dt.timedelta(days=1)).isoformat()
SYMBOLS = {  # 来源 -> (有缓存的标的, 没缓存的标的)
    "yahoo": ("AAPL", "MSFT"),
    "eastmoney": ("588000.SS", "513100.SS"),
    "ths": ("159915.SZ", None),  # 同花顺只用于已有遗留行的标的，所以没有「没缓存」的形态
    "eastmoney_fund": ("005698", "110011"),
}
RESULTS: List[tuple] = []
RESULTS_PRINTED = False
HTTP = httpx.Client(trust_env=False)  # 本机回环请求不走代理


def say(msg: str = "") -> None:
    print(msg, flush=True)


def check(name: str, observed: Any, expected: str, ok: bool) -> None:
    RESULTS.append((name, observed, expected, ok))
    say(f"    {'OK ' if ok else 'FAIL'} {name}: 观察={observed}  期望={expected}")


class Instance:
    def __init__(self, label: str, port: int, mysql: Mysql, upstream_url: str, token: str, workdir: Path, extra_env: Optional[Dict[str, str]] = None, **policy: Any):
        self.label, self.port, self.token = label, port, token
        self.url = f"http://127.0.0.1:{port}"
        self.log = workdir / f"{label}.log"
        self.env = {
            **mysql.env("drill"),
            "MCP_HOST": "127.0.0.1",
            "MCP_PORT": str(port),
            "MCP_CONFIG_DIR": str(workdir / f"cfg-{label}"),
            "MCP_TOKENS": json.dumps([{"name": "drill", "scopes": ["read", "refresh"], "token": token}]),
            "MCP_TEST_UPSTREAM_URL": upstream_url,
            "MCP_UPSTREAM_COOLDOWN_SECONDS": str(policy.get("base", 3)),
            "MCP_UPSTREAM_COOLDOWN_MAX_SECONDS": str(policy.get("cap", 12)),
            "MCP_UPSTREAM_MIN_INTERVAL_SECONDS": str(policy.get("gap", 0.2)),
            "MCP_RATE_LIMIT_READ_PER_MIN": "100000",
            "MCP_RATE_LIMIT_REFRESH_PER_MIN": "100000",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            **(extra_env or {}),
        }
        self.proc: Optional[subprocess.Popen] = None

    def start(self) -> None:
        handle = self.log.open("a")
        self.proc = subprocess.Popen([sys.executable, "-m", "trading.cli.serve"], cwd=REPO, env=self.env, stdout=handle, stderr=subprocess.STDOUT)
        for _ in range(120):
            try:
                if HTTP.get(f"{self.url}/healthz", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise SystemExit(f"实例 {self.label} 启动失败，见 {self.log}")

    def start_and_stop_to_create_tables(self, instances: List["Instance"]) -> None:
        """只想建表：起一个实例再停掉（init_schema 在启动时执行）。"""
        self.start()
        self.stop()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def call(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        r = HTTP.post(
            f"{self.url}/mcp", timeout=60,
            headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": args}},
        )
        result = r.json()["result"]
        return {"isError": bool(result.get("isError")), "sc": result.get("structuredContent") or {}, "text": (result.get("content") or [{}])[0].get("text", "")}

    def history(self, symbol: str, refresh: str) -> Dict[str, Any]:
        if symbol.isdigit():
            return self.call("get_fund_nav_history", {"fund_code": symbol, "start_date": START, "end_date": END, "refresh": refresh})
        return self.call("get_price_history", {"symbols": [symbol], "start_date": START, "end_date": END, "refresh": refresh})


def observe(resp: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    sc = resp["sc"]
    if resp["isError"]:
        err = sc.get("error") or {}
        return {"form": "结构化错误", "code": err.get("code"), "retry_after": err.get("retry_after"), "symbols": err.get("symbols"), "count": 0}
    info = (sc.get("cache") or {}).get(symbol, {})
    return {"form": "正常结果", "count": sc.get("count"), "tail_refresh_error": info.get("tail_refresh_error"),
            "retry_after": info.get("retry_after"), "refreshed": info.get("refreshed")}


def hits_delta(up: FakeUpstream, before) -> Dict[str, int]:
    now = up.snapshot()
    return {k: now[k] - before.get(k, 0) for k in now if now[k] != before.get(k, 0)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--client-src", help="AI 侧客户端所在的 src 目录（含 quant_engine 包）；给了才跑客户端步骤（只读运行，不写字节码）")
    ap.add_argument("--real-yahoo-probes", type=int, default=0, help="真实 Yahoo 探测次数（很小量，默认 0）")
    ap.add_argument("--only-awaiting-close", action="store_true", help="只演练「收盘前不回源」一段（S11），约 1 分钟")
    ap.add_argument("--only-fallback", action="store_true", help="只演练「Yahoo 不可达时的备用上游」一段（S12），约 1 分钟")
    ap.add_argument("--keep", action="store_true", help="结束后保留临时目录")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="trading-drill-"))
    up = FakeUpstream()
    mysql = Mysql()
    token = "drill-" + secrets.token_urlsafe(24)
    instances: List[Instance] = []
    try:
        say(f"== 限流冷却演练 · 假上游 {up.url} · 临时目录 {work} ==")
        mysql.start()
        mysql.create_db("drill")
        from sqlalchemy import create_engine, text
        engine = create_engine(f"mysql+pymysql://reguser:{mysql.user_pw}@127.0.0.1:{mysql.port}/drill")

        def sql(q: str, **p: Any):
            with engine.begin() as conn:
                return conn.execute(text(q), p).fetchall() if q.lstrip().lower().startswith("select") else conn.execute(text(q), p)

        if args.only_fallback:
            Instance("A0", free_port(), mysql, up.url, token, work, base=3, cap=12, gap=0.2).start_and_stop_to_create_tables(instances)
            phase_fallback(mysql, up, token, work, instances, sql)
            return finish(instances, up, mysql, work, args)

        if args.only_awaiting_close:
            Instance("A0", free_port(), mysql, up.url, token, work, base=3, cap=12, gap=0.2).start_and_stop_to_create_tables(instances)
            phase_awaiting_close(mysql, up, token, work, instances, sql)
            return finish(instances, up, mysql, work, args)

        # ---------------------------------------------------------------- 阶段一：短冷却（基准 3s，上限 12s）
        a = Instance("A1", free_port(), mysql, up.url, token, work, base=3, cap=12, gap=0.2)
        instances.append(a)
        a.start()
        say("\n[S0] 准备：实例 A1（冷却基准 3s / 上限 12s / 请求间隔 0.2s），用假上游（正常模式）给四个来源各灌一个有缓存的标的")
        # 同花顺只服务于已有遗留行的标的：直接写入遗留行（没有旁表记录）
        for i in range(25):
            day = TODAY - dt.timedelta(days=40 - i)
            if day.weekday() < 5:
                sql("INSERT IGNORE INTO trading_data (symbol,date,open_price,close_price,high,low,volume,create_time,update_time) "
                    "VALUES ('159915.SZ',:d,1,1,1,1,1,'2020-01-01 00:00:00','2020-01-01 00:00:00')", d=day)
        for source, (cached, _) in SYMBOLS.items():
            before = up.snapshot()
            o = observe(a.history(cached, "always"), cached)
            say(f"    {source:15s} {cached:10s} {o}  假上游新增请求={hits_delta(up, before)}")

        say("\n[S1] 命中限流：假上游对四个来源都返回限流（Yahoo 429 / 东财 429+Retry-After / 同花顺 IP 限制）")
        for s in up.mode:
            up.mode[s] = "limit"
        up.retry_after_header["eastmoney"] = "2"
        first_retry: Dict[str, int] = {}
        for source, (cached, _) in SYMBOLS.items():
            before = up.snapshot()
            o = observe(a.history(cached, "always"), cached)
            d = hits_delta(up, before)
            first_retry[source] = o.get("retry_after") or 0
            say(f"    {source:15s} {cached:10s} {o}  假上游新增请求={d}")
            check(f"{source}: 有缓存时返回正常结果+rate_limited+retry_after", (o["form"], o.get("tail_refresh_error"), o.get("retry_after")),
                  "(正常结果, rate_limited, ≥1)", o["form"] == "正常结果" and o.get("tail_refresh_error") == "rate_limited" and (o.get("retry_after") or 0) >= 1 and (o.get("count") or 0) > 0)
            check(f"{source}: 触发冷却的那次请求打到上游", d.get(source, 0), "1", d.get(source, 0) == 1)

        say("\n[S2] 冷却期内：反复刷新（always / missing，含同来源的其它标的），假上游收到的请求数必须为 0")
        for source, (cached, uncached) in SYMBOLS.items():
            before = up.snapshot()
            seqs: Dict[str, List[int]] = {}
            for i in range(12):
                sym = cached if i % 3 else (uncached or cached)
                o = observe(a.history(sym, "always" if i % 2 else "missing"), sym)
                if o.get("retry_after") is not None:
                    seqs.setdefault(sym, []).append(o["retry_after"])
            d = hits_delta(up, before)
            check(f"{source}: 12 次冷却期刷新的上游请求数", d.get(source, 0), "0", d.get(source, 0) == 0)
            # 逐标的看：港美股的没缓存标的会取「Yahoo 冷却 / 各备用源冷却」里最早恢复的那个，所以不同标的之间可以差 1 秒
            for sym, seq in seqs.items():
                say(f"      {sym} retry_after 序列={seq}")
                check(f"{source}/{sym}: retry_after 随时间不增", seq == sorted(seq, reverse=True), "非递增", seq == sorted(seq, reverse=True))

        say("\n[S3] 没缓存时的返回形态（冷却期内，refresh=missing）")
        for source, (_, uncached) in SYMBOLS.items():
            if uncached is None:
                say(f"    {source:15s} 无（同花顺只用于已有遗留行的标的，没有「没缓存」的形态）")
                continue
            before = up.snapshot()
            resp = a.history(uncached, "missing")
            o = observe(resp, uncached)
            d = hits_delta(up, before)
            say(f"    {source:15s} {uncached:10s} isError={resp['isError']} {o}  假上游新增请求={d}")
            check(f"{source}: 没缓存→结构化错误 rate_limited+retry_after", (resp["isError"], o.get("code"), o.get("retry_after")),
                  "(True, rate_limited, ≥1)", resp["isError"] and o.get("code") == "rate_limited" and (o.get("retry_after") or 0) >= 1)
            check(f"{source}: 没缓存时冷却期内的上游请求数", d.get(source, 0), "0", d.get(source, 0) == 0)
        # 部分有缓存的多标的请求
        resp = a.call("get_price_history", {"symbols": ["AAPL", "MSFT"], "start_date": START, "end_date": END, "refresh": "missing"})
        sc = resp["sc"]
        say(f"    混合请求 AAPL(有缓存)+MSFT(没缓存): isError={resp['isError']} count={sc.get('count')} 顶层retry_after={sc.get('retry_after')} "
            f"MSFT.cache_status={sc['cache']['MSFT']['cache_status']} MSFT.tail_refresh_error={sc['cache']['MSFT']['tail_refresh_error']}")
        check("混合请求：有缓存的照常返回、没缓存的标记 rate_limited", (resp["isError"], sc["cache"]["MSFT"]["tail_refresh_error"]), "(False, rate_limited)",
              (not resp["isError"]) and sc["cache"]["MSFT"]["tail_refresh_error"] == "rate_limited" and (sc.get("count") or 0) > 0)

        say("\n[S4] 冷却时长：指数增长、带抖动、有上限（Yahoo：基准 3s、上限 12s；名义值 3,6,12,12,12，允许 ±20% 抖动 + 向上取整）")
        durations = [first_retry["yahoo"]]
        for rnd in range(2, 6):
            time.sleep(durations[-1] + 0.4)
            before = up.snapshot()
            o = observe(a.history("AAPL", "always"), "AAPL")
            d = hits_delta(up, before)
            durations.append(o.get("retry_after") or 0)
            say(f"    第 {rnd} 次触发：retry_after={o.get('retry_after')}  假上游新增请求={d}")
        nominal = [3, 6, 12, 12, 12]
        ratios = [round(x / n, 2) for x, n in zip(durations, nominal)]
        say(f"    观察到的冷却时长={durations}  名义值={nominal}  比值={ratios}")
        check("冷却时长在 名义值×[0.8,1.2] 内（含向上取整的 1s 余量）", durations,
              str([f"{n*0.8:.1f}~{n*1.2+1:.1f}" for n in nominal]), all(n * 0.8 - 0.01 <= x <= n * 1.2 + 1.01 for x, n in zip(durations, nominal)))
        check("增长：第 3 次 > 第 1 次，且到上限后不再增长", (durations[2] > durations[0], max(durations[2:]) <= 12 * 1.2 + 1.01), "(True, True)",
              durations[2] > durations[0] and max(durations[2:]) <= 12 * 1.2 + 1.01)
        others = [first_retry["eastmoney"], first_retry["eastmoney_fund"], first_retry["ths"]]
        say(f"    抖动：另外三个来源首次冷却（基准同为 3s，名义 3）的观察值={others}；东财还带 Retry-After=2 头（下限，不超过基准×抖动）")

        say("\n[S5] 成功后清零：恢复假上游 → 冷却结束后的第一次请求成功 → 再次限流时冷却回到基准")
        up.mode["yahoo"] = "ok"
        time.sleep(durations[-1] + 0.4)
        before = up.snapshot()
        o = observe(a.history("AAPL", "always"), "AAPL")
        say(f"    恢复后: {o}  假上游新增请求={hits_delta(up, before)}")
        check("恢复后刷新成功", (o.get("tail_refresh_error"), o.get("refreshed")), "(None, True)", o.get("tail_refresh_error") is None and bool(o.get("refreshed")))
        up.mode["yahoo"] = "limit"
        time.sleep(0.4)
        o = observe(a.history("AAPL", "always"), "AAPL")
        say(f"    再次限流: retry_after={o.get('retry_after')}（上一轮已增长到 {durations[-1]}）")
        check("清零后冷却回到基准 3s 量级", o.get("retry_after"), "2.4~4.6", 2.4 <= (o.get("retry_after") or 99) <= 4.61)

        if args.client_src:
            run_client_steps(args.client_src, a, up, token, work)

        # ---------------------------------------------------------------- 阶段二：长冷却，持久化 / 共享 / single-flight
        a.stop()
        instances.remove(a)
        for s in up.mode:
            up.mode[s] = "ok"
        sql("DELETE FROM market_upstream_state")
        say("\n[S6] 阶段二：清空冷却状态表；实例 A2 与 B 同时在线（冷却基准 20s / 上限 60s）")
        a2 = Instance("A2", free_port(), mysql, up.url, token, work, base=20, cap=60, gap=0.2)
        b = Instance("B", free_port(), mysql, up.url, token, work, base=20, cap=60, gap=0.2)
        instances += [a2, b]
        a2.start()
        b.start()
        o = observe(b.history("AAPL", "always"), "AAPL")  # 让 B 先有一次正常的上游访问（建立它的同步时间点）
        say(f"    B 先做一次正常刷新: {o}")
        up.mode["yahoo"] = "limit"
        before = up.snapshot()
        o = observe(a2.history("AAPL", "always"), "AAPL")
        t_trip = time.time()
        say(f"    A2 触发限流: {o}  假上游新增请求={hits_delta(up, before)}")
        rows = sql("SELECT source, cooldown_until, failures FROM market_upstream_state")
        say(f"    market_upstream_state: {[(r[0], str(r[1]), r[2]) for r in rows]}")
        check("触发后冷却状态写入旁表", [r[0] for r in rows], "['yahoo']", [r[0] for r in rows] == ["yahoo"])

        say("\n[S7] 两个实例共享冷却：A2 触发后，B（没有自己的内存状态）多久开始遵守冷却、期间向上游多发了几个请求")
        before = up.snapshot()
        lag, polls = None, []
        while time.time() - t_trip < 15:
            o = observe(b.history("AAPL", "always"), "AAPL")
            polls.append((round(time.time() - t_trip, 1), o.get("tail_refresh_error"), o.get("retry_after")))
            if o.get("tail_refresh_error") == "rate_limited":
                lag = round(time.time() - t_trip, 1)
                break
            time.sleep(0.5)
        extra = hits_delta(up, before).get("yahoo", 0)
        say(f"    B 轮询(秒, 结果, retry_after)={polls}")
        say(f"    B 开始遵守冷却的延迟={lag}s；这段时间 B 向上游多发的请求数={extra}（实现上每 5s 才同步一次旁表）")
        check("B 在一个同步周期（≤5s）内遵守 A2 触发的冷却", lag, "≤ 5.5s", lag is not None and lag <= 5.5)
        check("同步延迟内 B 多发的上游请求数", extra, "≤ 1", extra <= 1)
        before = up.snapshot()
        for _ in range(6):
            b.history("AAPL", "always")
        check("B 遵守冷却后的上游请求数", hits_delta(up, before).get("yahoo", 0), "0", hits_delta(up, before).get("yahoo", 0) == 0)

        say("\n[S8] 重启后冷却仍然生效：停掉 A2，起 A3（同一数据库），立刻请求")
        a2.stop()
        instances.remove(a2)
        a3 = Instance("A3", free_port(), mysql, up.url, token, work, base=20, cap=60, gap=0.2)
        instances.append(a3)
        a3.start()
        before = up.snapshot()
        o = observe(a3.history("AAPL", "always"), "AAPL")
        remaining = round(float(sql("SELECT TIMESTAMPDIFF(MICROSECOND, UTC_TIMESTAMP(), cooldown_until)/1e6 FROM market_upstream_state")[0][0]), 1)
        say(f"    A3 首次请求: {o}  假上游新增请求={hits_delta(up, before)}  旁表里剩余冷却={remaining}s")
        check("重启后立刻请求：返回 rate_limited 且不触达上游", (o.get("tail_refresh_error"), hits_delta(up, before).get("yahoo", 0)), "(rate_limited, 0)",
              o.get("tail_refresh_error") == "rate_limited" and hits_delta(up, before).get("yahoo", 0) == 0)

        say("\n[S9] 同一标的并发请求合并成一次（single-flight）：等冷却结束，上游设 1.5s 延迟，10 路并发 refresh=always")
        time.sleep(max(remaining, 0) + 1.0)
        up.mode["yahoo"] = "ok"
        up.delay["yahoo"] = 1.5
        before = up.snapshot()
        with cf.ThreadPoolExecutor(10) as ex:
            outs = list(ex.map(lambda _: observe(a3.history("AAPL", "always"), "AAPL"), range(10)))
        up.delay["yahoo"] = 0
        d = hits_delta(up, before).get("yahoo", 0)
        say(f"    10 路并发的结果: {sorted({(o['form'], o.get('count'), o.get('tail_refresh_error')) for o in outs})}  假上游收到的请求数={d}")
        check("10 路并发 always 的上游请求数", d, "1", d == 1)
        check("10 路并发都拿到正常结果", sum(1 for o in outs if o["form"] == "正常结果" and (o.get("count") or 0) > 0), "10", all(o["form"] == "正常结果" for o in outs))

        phase_awaiting_close(mysql, up, token, work, instances, sql)
        phase_fallback(mysql, up, token, work, instances, sql)

        if args.real_yahoo_probes:
            say(f"\n[S10] 真实 Yahoo 探测 {args.real_yahoo_probes} 次（很小量；走与服务相同的 GuardedFetcher + 真实 yfinance）")
            probe_real_yahoo(args.real_yahoo_probes)
    finally:
        if not RESULTS_PRINTED:
            cleanup(instances, up, mysql, work, args)
    return summarize()


def cleanup(instances: List[Instance], up: FakeUpstream, mysql: Mysql, work: Path, args: argparse.Namespace) -> None:
    for i in instances:
        i.stop()
    up.close()
    mysql.stop()
    if args.keep:
        say(f"\n临时目录保留在 {work}（含各实例日志）")
    else:
        shutil.rmtree(work, ignore_errors=True)


def finish(instances: List[Instance], up: FakeUpstream, mysql: Mysql, work: Path, args: argparse.Namespace) -> int:
    cleanup(instances, up, mysql, work, args)
    global RESULTS_PRINTED
    RESULTS_PRINTED = True
    return summarize()


def summarize() -> int:
    failed = [r for r in RESULTS if not r[3]]
    say(f"\n== 汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} 项检查通过 ==")
    for name, observed, expected, _ in failed:
        say(f"  FAIL {name}: 观察={observed} 期望={expected}")
    return 1 if failed else 0


def phase_awaiting_close(mysql: Mysql, up: FakeUpstream, token: str, work: Path, instances: List[Instance], sql: Any) -> None:
    from zoneinfo import ZoneInfo

    sh = ZoneInfo("Asia/Shanghai")
    day = dt.datetime.now(sh).date()
    while day.weekday() >= 5:  # 假时钟落在最近一个工作日
        day -= dt.timedelta(days=1)
    prev = day - dt.timedelta(days=1)
    ready_text = f"{day.isoformat()}T15:30:00+08:00"
    fake_now = dt.datetime.combine(day, dt.time(15, 29, 30), tzinfo=sh)  # 距「可定稿」30 秒
    up.today = day
    for s in up.mode:
        up.mode[s] = "ok"
        up.delay[s] = 0
    say(f"\n[S11] 收盘前不回源：服务假时钟 = {fake_now.isoformat()}（A 股 15:00 收盘 + 30 分钟缓冲 → {ready_text} 可定稿，还差 30 秒）；假上游的「今天」= {day}")
    a4 = Instance("A4", free_port(), mysql, up.url, token, work, extra_env={"MCP_TEST_NOW": fake_now.isoformat()}, base=3, cap=12, gap=0.2)
    instances.append(a4)
    a4.start()
    cached, uncached_hist, only_today = "510300.SS", "513500.SS", "515000.SS"

    def em_requests(before: int) -> List[str]:
        return [path for _, source, path in up.log[before:] if source == "eastmoney"]

    def call_hist(symbol: str, start: dt.date, end: dt.date, refresh: str = "missing", **extra: Any) -> Dict[str, Any]:
        return a4.call("get_price_history", {"symbols": [symbol], "start_date": start.isoformat(), "end_date": end.isoformat(), "refresh": refresh, **extra})

    before = len(up.log)
    resp = call_hist(cached, day - dt.timedelta(days=30), prev)
    sc = resp["sc"]
    say(f"    a) 先备历史（范围到前一天 {prev}，不含当日）: count={sc['count']} 假上游请求数={len(em_requests(before))}")
    check("备历史时回源 1 次", len(em_requests(before)), "1", len(em_requests(before)) == 1)

    before = len(up.log)
    resp = call_hist(cached, day - dt.timedelta(days=30), day)
    entry = resp["sc"]["cache"][cached]
    aw = entry["awaiting_close"]
    last_row = resp["sc"]["data"][-1]["date"]
    say(f"    b) 收盘前请求含当日（refresh=missing）: 假上游请求数={len(em_requests(before))}  count={resp['sc']['count']} 最后一行={last_row}")
    say(f"       awaiting_close={aw}  tail_refresh_error={entry['tail_refresh_error']}  retry_after={entry['retry_after']}")
    check("收盘前：假上游收到的请求数", len(em_requests(before)), "0", len(em_requests(before)) == 0)
    check("awaiting_close.date / final_ready_at 正确", (aw["date"], aw["final_ready_at"]), f"({day}, {ready_text})", aw["date"] == day.isoformat() and aw["final_ready_at"] == ready_text)
    check("awaiting_close.wait_seconds 在 1~30 之间（假时钟在走）", aw["wait_seconds"], "1..30", 1 <= aw["wait_seconds"] <= 30)
    check("tail_refresh_error 保持 null（这不是错误）", (entry["tail_refresh_error"], entry["retry_after"]), "(None, None)", entry["tail_refresh_error"] is None and entry["retry_after"] is None)
    check("返回的最后一行是前一个交易日的定稿行", last_row < day.isoformat() and resp["sc"]["data"][-1]["is_final"], f"< {day} 且 is_final", last_row < day.isoformat() and resp["sc"]["data"][-1]["is_final"])

    before = len(up.log)
    latest = a4.call("get_latest_price", {"symbol": cached})["sc"]
    say(f"    c) get_latest_price: 假上游请求数={len(em_requests(before))}  data.date={latest['data']['date']}  awaiting_close={latest['awaiting_close']}")
    say(f"       说明: {latest['awaiting_close_message']}")
    check("get_latest 收盘前：不回源且带说明", (len(em_requests(before)), "15:30" in latest["awaiting_close_message"]), "(0, True)",
          len(em_requests(before)) == 0 and "15:30" in latest["awaiting_close_message"] and latest["awaiting_close"]["date"] == day.isoformat())

    before = len(up.log)
    resp = call_hist(uncached_hist, day - dt.timedelta(days=30), day)
    reqs = em_requests(before)
    end_param = reqs[0].split("end=")[1].split("&")[0] if reqs else None
    say(f"    d) 没缓存 + 范围含当日: 假上游请求数={len(reqs)}  请求的 end={end_param}（前一天={prev:%Y%m%d}）  count={resp['sc']['count']}  awaiting_close.date={resp['sc']['cache'][uncached_hist]['awaiting_close']['date']}")
    check("没缓存时只回源到前一天，不碰当日", (len(reqs), end_param), f"(1, {prev:%Y%m%d})", len(reqs) == 1 and end_param == f"{prev:%Y%m%d}")

    before = len(up.log)
    resp = call_hist(only_today, day, day)
    err = (resp["sc"].get("error") or {})
    say(f"    e) 只请求当日且没缓存: isError={resp['isError']} error={err.get('code')} wait_seconds={err.get('wait_seconds')}  假上游请求数={len(em_requests(before))}")
    check("只请求当日且没缓存：结构化 awaiting_close 错误，不回源", (resp["isError"], err.get("code"), len(em_requests(before))), "(True, awaiting_close, 0)",
          resp["isError"] and err.get("code") == "awaiting_close" and len(em_requests(before)) == 0)

    before = len(up.log)
    resp = call_hist(cached, day - dt.timedelta(days=30), day, refresh="always", intraday=True)
    row = resp["sc"]["data"][-1]
    say(f"    f) intraday=true + refresh=always（显式要盘中值；always 绕过 TTL，便于观察）: 假上游请求数={len(em_requests(before))}  最后一行={row['date']} is_final={row['is_final']}  awaiting_close={resp['sc']['cache'][cached]['awaiting_close']}")
    check("intraday=true：回源并带 is_final=false 的当日行", (len(em_requests(before)), row["date"], row["is_final"]), f"(1, {day}, False)",
          len(em_requests(before)) == 1 and row["date"] == day.isoformat() and row["is_final"] is False)

    left = call_hist(cached, day - dt.timedelta(days=30), day, refresh="never")["sc"]["cache"][cached]["awaiting_close"]["wait_seconds"]
    wait = left + 2  # 多等 2 秒越过可定稿时刻
    say(f"    …等待假时钟越过 {ready_text}（还差 {left}s，等 {wait}s）")
    time.sleep(wait)
    before = len(up.log)
    resp = call_hist(cached, day - dt.timedelta(days=30), day)
    row = resp["sc"]["data"][-1]
    say(f"    g) 过了可定稿时刻: 假上游请求数={len(em_requests(before))}  最后一行={row['date']} is_final={row['is_final']} as_of={row['as_of']}  awaiting_close={resp['sc']['cache'][cached]['awaiting_close']}")
    check("过了 final_ready_at 之后第一次请求就回源（不等 TTL），拿到定稿的当日行", (len(em_requests(before)), row["is_final"], resp["sc"]["cache"][cached]["awaiting_close"]), "(1, True, None)",
          len(em_requests(before)) == 1 and row["is_final"] is True and resp["sc"]["cache"][cached]["awaiting_close"] is None)

    before = len(up.log)
    for refresh in ("missing", "missing", "never"):
        call_hist(cached, day - dt.timedelta(days=30), day, refresh=refresh)
        a4.call("get_latest_price", {"symbol": cached, "refresh": refresh})
    say(f"    h) 当日行已定稿后再请求 6 次（missing / missing / never，含 get_latest）: 假上游请求数={len(em_requests(before))}")
    check("已定稿的当日行永远不回源", len(em_requests(before)), "0", len(em_requests(before)) == 0)


def phase_fallback(mysql: Mysql, up: FakeUpstream, token: str, work: Path, instances: List[Instance], sql: Any) -> None:
    """S12：Yahoo 限流时港股 / 美股改用备用上游（东财不复权 / 新浪），走真实 HTTP + 真实 requests + 真实冷却保护."""
    from fake_upstream import _price, _weekdays

    for src in up.mode:
        up.mode[src] = "ok"
        up.delay[src] = 0
    up.today = TODAY
    end = TODAY - dt.timedelta(days=3)
    while end.weekday() >= 5:
        end -= dt.timedelta(days=1)
    start = end - dt.timedelta(days=25)
    window = _weekdays(start, end)
    say(f"\n[S12] Yahoo 不可达时的备用上游：窗口 {start} ~ {end}（{len(window)} 个工作日，都早已收盘）；假上游的 Yahoo 对所有请求返回 429")
    a5 = Instance("A5", free_port(), mysql, up.url, token, work, base=3, cap=12, gap=0.2)
    instances.append(a5)
    a5.start()
    up.mode["yahoo"] = "limit"

    def hist(symbol: str, refresh: str = "missing") -> Dict[str, Any]:
        return a5.call("get_price_history", {"symbols": [symbol], "start_date": start.isoformat(), "end_date": end.isoformat(), "refresh": refresh})

    def seed_legacy(symbol: str, code: str, factor: float) -> int:
        rows = [d for d in window if d < end]  # 最后一个交易日留给备用源去补
        for d in rows:
            price = round(_price(code, d) * factor, 3)
            sql("INSERT IGNORE INTO trading_data (symbol,date,open_price,close_price,high,low,volume,create_time,update_time) "
                "VALUES (:s,:d,:p,:p,:p,:p,1,'2020-01-01 00:00:00',:u)", s=symbol, d=d, p=price, u=f"{(d + dt.timedelta(days=1)).isoformat()} 03:00:00")
        return len(rows)

    new_hk, new_us, legacy_ok, legacy_bad = "9988.HK", "NVDA", "0700.HK", "9626.HK"

    before = up.snapshot()
    resp = hist(new_hk)
    info, sc = resp["sc"]["cache"][new_hk], resp["sc"]
    d = hits_delta(up, before)
    say(f"    a) 新港股 {new_hk}：fallback={info['fallback']} source={info['upstream_source']} price_basis={info['price_basis']} count={sc['count']} 假上游请求={d}")
    check("a) Yahoo 429 → 东财不复权补齐（fallback=true、真实 source、unadjusted）",
          (info["fallback"], info["upstream_source"], info["price_basis"], sc["count"] == len(window), d.get("yahoo"), d.get("eastmoney")),
          "(True, eastmoney_fqt0, unadjusted, True, 1, 1)",
          info["fallback"] is True and info["upstream_source"] == "eastmoney_fqt0" and info["price_basis"] == "unadjusted" and sc["count"] == len(window)
          and d.get("yahoo") == 1 and d.get("eastmoney") == 1 and not d.get("sina") and not d.get("tencent"))
    check("a) 行上标真实来源与口径", (sc["data"][-1]["source"], sc["data"][-1]["price_basis"], sc["data"][-1]["is_final"]), "(eastmoney_fqt0, unadjusted, True)",
          sc["data"][-1]["source"] == "eastmoney_fqt0" and sc["data"][-1]["price_basis"] == "unadjusted" and sc["data"][-1]["is_final"] is True)

    before = up.snapshot()
    for _ in range(3):
        hist(new_hk)
    d = hits_delta(up, before)
    say(f"    b) 已写入的定稿行再读 3 次：假上游请求={d}")
    check("b) 定稿行不回源（备用源也一样）", sum(d.values()), "0", not d)

    before = up.snapshot()
    resp = hist(new_us)
    info, sc = resp["sc"]["cache"][new_us], resp["sc"]
    d = hits_delta(up, before)
    say(f"    c) 新美股 {new_us}：source={info['upstream_source']} price_basis={info['price_basis']} count={sc['count']} 假上游请求={d}")
    check("c) 美股首选新浪（不复权，只截窗口），不碰东财 / 腾讯", (info["upstream_source"], info["price_basis"], sc["count"] == len(window), d.get("sina"), d.get("eastmoney"), d.get("tencent")),
          "(sina_us, unadjusted, True, 1, None, None)",
          info["upstream_source"] == "sina_us" and info["price_basis"] == "unadjusted" and sc["count"] == len(window) and d.get("sina") == 1 and not d.get("eastmoney") and not d.get("tencent"))

    n = seed_legacy(legacy_ok, "00700", 1.0)
    before = up.snapshot()
    resp = hist(legacy_ok)
    info, sc = resp["sc"]["cache"][legacy_ok], resp["sc"]
    d = hits_delta(up, before)
    seeded_close = sql("SELECT close_price FROM trading_data WHERE symbol=:s AND date=:d", s=legacy_ok, d=window[n - 1])[0][0]
    say(f"    d) 遗留序列 {legacy_ok}（{n} 行）重叠日一致：fallback={info['fallback']} price_bases={info['price_bases']} count={sc['count']} overlap={(info['fallback_detail'] or {}).get('overlap')}")
    check("d) 重叠日校验通过：只追加锚点之后的行，口径标 mixed", (info["fallback"], info["tail_refresh_error"], info["price_basis"], sc["count"] == n + 1, sorted(info["price_bases"])),
          "(True, None, mixed, True, [legacy_unknown, unadjusted])",
          info["fallback"] is True and info["tail_refresh_error"] is None and info["price_basis"] == "mixed" and sc["count"] == n + 1
          and sorted(info["price_bases"]) == ["legacy_unknown", "unadjusted"] and info["fallback_detail"]["overlap"]["ok"] is True)
    check("d) 锚点行及更早的历史原样不动", float(seeded_close), f"{_price('00700', window[n - 1])}", abs(float(seeded_close) - _price("00700", window[n - 1])) < 1e-6)

    m = seed_legacy(legacy_bad, "09626", 1.02)  # 库里的价格比不复权价高 2%：模拟已被 Yahoo 按分红调整
    before = up.snapshot()
    resp = hist(legacy_bad)
    info, sc = resp["sc"]["cache"][legacy_bad], resp["sc"]
    rows_after = sql("SELECT COUNT(*) FROM trading_data WHERE symbol=:s", s=legacy_bad)[0][0]
    ov = (info["basis_mismatch"] or {}).get("overlap") or {}
    say(f"    e) 遗留序列 {legacy_bad}（{m} 行）重叠日不一致：tail_refresh_error={info['tail_refresh_error']} fallback={info['fallback']} overlap={ov} 库内行数={rows_after}")
    check("e) 重叠日校验失败：不追加，返回 basis_mismatch", (info["tail_refresh_error"], info["fallback"], ov.get("reason"), rows_after, d := hits_delta(up, before).get("tencent")),
          f"(basis_mismatch, False, close_differs, {m}, None)",
          info["tail_refresh_error"] == "basis_mismatch" and info["fallback"] is False and ov.get("reason") == "close_differs" and rows_after == m and not d)

    for src in ("eastmoney", "sina", "tencent"):
        up.mode[src] = "limit"
    before = up.snapshot()
    resp = hist("TSLA")
    err = resp["sc"].get("error") or {}
    attempts = (err.get("fallback_attempts") or {}).get("TSLA") or []
    d = hits_delta(up, before)
    say(f"    f) Yahoo 与全部备用源都限流（无缓存）：isError={resp['isError']} code={err.get('code')} retry_after={err.get('retry_after')} attempts={[(a['source'], a['result']) for a in attempts]} 假上游请求={d}")
    check("f) 结构化 rate_limited，列出 3 个备用源各自的结果", (resp["isError"], err.get("code"), [a["source"] for a in attempts], {a["result"] for a in attempts}),
          "(True, rate_limited, [sina_us, eastmoney_fqt0, tencent_qfq], {rate_limited})",
          resp["isError"] and err.get("code") == "rate_limited" and [a["source"] for a in attempts] == ["sina_us", "eastmoney_fqt0", "tencent_qfq"]
          and {a["result"] for a in attempts} == {"rate_limited"} and (err.get("retry_after") or 0) >= 1)
    before = up.snapshot()
    hist("TSLA")
    hist("TSLA")
    d = hits_delta(up, before)
    check("f) 冷却 / 静默期内再请求：不再打任何上游", sum(d.values()), "0", not d)

    for src in up.mode:
        up.mode[src] = "ok"  # Yahoo 与备用源都恢复了
    wait = (err.get("retry_after") or 3) + 3  # 等备用源自己的冷却结束（冷却状态在服务里，不会因为假上游恢复就消失）
    say(f"    …等待备用源冷却结束（{wait}s）")
    time.sleep(wait)
    before = up.snapshot()
    resp = hist(new_hk, "always")
    info = resp["sc"]["cache"][new_hk]
    d = hits_delta(up, before)
    say(f"    g) Yahoo 恢复后，已锁在不复权口径的 {new_hk} 刷新：fallback_detail.reason={(info['fallback_detail'] or {}).get('reason')} 假上游请求={d}")
    check("g) 序列已是不复权口径：沿用备用源、不去碰 Yahoo（Yahoo 的调整价接不上去）", (d.get("yahoo"), d.get("eastmoney"), (info["fallback_detail"] or {}).get("reason")),
          "(None, 1, series_basis_locked)", not d.get("yahoo") and d.get("eastmoney") == 1 and (info["fallback_detail"] or {}).get("reason") == "series_basis_locked")


def probe_real_yahoo(n: int) -> None:
    os.environ.pop("MCP_TEST_UPSTREAM_URL", None)
    sys.path.insert(0, str(REPO))
    from trading.market.errors import FetchError, RateLimited
    from trading.market.fetchers import RangePriceFetcher
    from trading.market.upstream import GuardedFetcher, UpstreamGuard

    fetcher = GuardedFetcher(RangePriceFetcher(), UpstreamGuard())
    end = TODAY - dt.timedelta(days=1)
    for i in range(n):
        t = time.time()
        try:
            r = fetcher.fetch("0700.HK", end - dt.timedelta(days=20), end, mode="raw")
            say(f"    第 {i + 1} 次: 成功 rows={len(r.rows)} 口径={r.price_basis} 事件={len(r.actions)} 耗时={time.time() - t:.1f}s")
        except RateLimited as exc:
            say(f"    第 {i + 1} 次: 被限流 retry_after={int(exc.retry_after)}s（熔断已生效，停止探测）")
            break
        except FetchError as exc:
            say(f"    第 {i + 1} 次: 抓取失败 {exc}")


CLIENT_SCRIPT = textwrap.dedent('''
    import json, sys, time, datetime as dt, tempfile
    from pathlib import Path
    from quant_engine.market.remote import RemoteMarketGateway, RemoteSettings, RateLimitedError
    url, token, start, end = sys.argv[1:5]
    scenario = sys.argv[5]
    waits = []
    settings = RemoteSettings(url=url, token=token, refresh_token=token, timeout=60, cache_path=Path(tempfile.mkdtemp()) / "c.sqlite3",
                              rate_limit_retries=2, rate_limit_max_wait=40, rate_limit_total_wait=60, internal_error_retries=2)
    gw = RemoteMarketGateway(settings, on_wait=waits.append)
    sym, refresh = {"degraded": ("AAPL", "always"), "nocache": ("MSFT", "missing"), "recover": ("AAPL", "always")}[scenario]
    t = time.time()
    out = {"scenario": scenario}
    try:
        p = gw.query_range(sym, dt.date.fromisoformat(start), dt.date.fromisoformat(end), refresh)
        out.update(outcome="returned", rows=p["count"], rate_limit=p.get("rate_limit"), tail_refresh_error=p.get("tail_refresh_error"))
    except RateLimitedError as exc:
        out.update(outcome="raised RateLimitedError", retry_after=exc.retry_after, waited=round(exc.waited, 1))
    except Exception as exc:
        out.update(outcome=f"raised {type(exc).__name__}", message=str(exc)[:120])
    out.update(elapsed=round(time.time() - t, 1), waits=[w for w in waits if "waiting" in w], notices=[w for w in waits if "waiting" not in w])
    print("RESULT " + json.dumps(out, ensure_ascii=False))
''')


def run_client_steps(src: str, inst: Instance, up: FakeUpstream, token: str, work: Path) -> None:
    say("\n[C] AI 侧客户端（只读运行，不写字节码）连本地服务：是否按 retry_after 等待、次数和总时长有上限、不无限重试")
    say("    客户端上限: 限流重试 2 次 / 单请求最多等 40s / 总共最多等 60s")
    script = work / "client_probe.py"
    script.write_text(CLIENT_SCRIPT)

    def run(scenario: str) -> Dict[str, Any]:
        env = {**os.environ, "PYTHONPATH": src, "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run(["uv", "run", "--with", "lark>=1.1.9,<2", "python", "-B", str(script), inst.url + "/mcp", token, START, END, scenario],
                              cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
        line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
        if not line:
            raise SystemExit(f"客户端步骤失败:\n{proc.stdout[-800:]}\n{proc.stderr[-800:]}")
        return json.loads(line[7:])

    def reset_and_trip() -> None:
        """恢复上游→冷却结束后成功一次（失败计数清零）→再限流并触发一次：此时服务端处于首次冷却（≈3s）。"""
        up.mode["yahoo"] = "ok"
        time.sleep(16)
        inst.history("AAPL", "always")
        up.mode["yahoo"] = "limit"
        inst.history("AAPL", "always")

    reset_and_trip()

    for scenario, label in (("degraded", "有缓存 + 上游持续限流"), ("nocache", "没缓存 + 上游持续限流")):
        before = up.snapshot()
        r = run(scenario)
        d = hits_delta(up, before).get("yahoo", 0)
        say(f"    [{label}] {r}  期间假上游收到的请求数={d}")
        check(f"客户端 {label}: 等待次数 ≤ 2、总等待 ≤ 60s、不无限重试", (len(r["waits"]), r["elapsed"]), "(≤2, ≤60)", len(r["waits"]) <= 2 and r["elapsed"] <= 60)
        if scenario == "degraded":
            check("客户端 有缓存: 上限用尽后降级使用缓存行（不抛错）", (r["outcome"], (r.get("rate_limit") or {}).get("outcome")), "(returned, degraded)",
                  r["outcome"] == "returned" and (r.get("rate_limit") or {}).get("outcome") == "degraded")
        else:
            check("客户端 没缓存: 上限用尽后抛 RateLimitedError", r["outcome"], "raised RateLimitedError", r["outcome"] == "raised RateLimitedError")
        check(f"客户端 {label}: 服务端只在冷却结束后才被触达（上游请求数 ≤ 重试次数+0）", d, "≤ 2", d <= 2)
        time.sleep(0.5)

    # 恢复场景：触发冷却后立刻让上游恢复；客户端等 retry_after 后重试成功
    reset_and_trip()
    up.mode["yahoo"] = "ok"
    before = up.snapshot()
    r = run("recover")
    d = hits_delta(up, before).get("yahoo", 0)
    say(f"    [上游已恢复] {r}  期间假上游收到的请求数={d}")
    check("客户端 上游恢复: 等够 retry_after 后重试成功", (r["outcome"], (r.get("rate_limit") or {}).get("outcome"), len(r["waits"])), "(returned, recovered, 1)",
          r["outcome"] == "returned" and (r.get("rate_limit") or {}).get("outcome") == "recovered" and len(r["waits"]) == 1)
    up.mode["yahoo"] = "limit"


if __name__ == "__main__":
    sys.exit(main())
