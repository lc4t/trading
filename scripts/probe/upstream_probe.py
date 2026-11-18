#!/usr/bin/env python3
"""港股 / 美股日线上游探测：对每个候选源取同一只港股和同一只美股近 30 天日线，看能不能用、口径是什么.

    python scripts/probe/upstream_probe.py                  # 默认 0700.HK 与 AAPL，全部候选源
    python scripts/probe/upstream_probe.py --only eastmoney_fqt0,tencent_qfq --json out.json

只用标准库（yfinance 候选在容器里有 yfinance 时才会跑）。**每个源对每只标的最多请求 2 次**（第 2 次只在网络异常 / 5xx 时重试，
遇到 429 绝不重试），请求之间间隔 ≥1 秒，不压测、不并发。不需要数据库、不需要任何密钥。

输出每个源一行：HTTP 状态、行数、最近一个交易日、复权口径、耗时；末尾给一张「同一日期收盘价」对照表，
用来判断各源的价格口径是否一致（不复权 / 前复权 / 拆股调整的收盘价在发生过分红的标的上会不同）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
TODAY = dt.date.today()
START = TODAY - dt.timedelta(days=30)
Row = Tuple[str, float, float, float, float, float]  # date, open, high, low, close, volume

# 东财 / 腾讯需要知道美股所在交易所；探测里写死 AAPL（纳斯达克），生产实现可以用东财的 suggest 接口按代码查
SYMBOLS = {
    "0700.HK": {"em": "116.00700", "tx": "hk00700", "yahoo": "0700.HK", "sina": None, "stooq": "0700.hk"},
    "AAPL": {"em": "105.AAPL", "tx": "usAAPL.OQ", "yahoo": "AAPL", "sina": "aapl", "stooq": "aapl.us"},
}


class Unsupported(Exception):
    """这个源没有该市场的端点."""


class Blocked(Exception):
    """被限流 / 反爬挑战拦住（不重试）."""


def http_get(url: str, *, headers: Optional[Dict[str, str]] = None, timeout: float = 15.0, impersonate: bool = False) -> Tuple[int, str]:
    if impersonate:  # curl_cffi（yfinance 的依赖）：带真实浏览器的 TLS 指纹，Yahoo 会拦标准库的 TLS 指纹
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            raise Unsupported("没有 curl_cffi；在镜像里运行才会测这一项") from None
        response = curl_requests.get(url, headers=headers, timeout=timeout, impersonate="chrome")
        return response.status_code, response.text
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _f(value: Any) -> float:
    return float(value)


# ------------------------------------------------------------------------------------ 各候选源
def eastmoney(fqt: int) -> Callable[[str, Dict[str, Any]], Tuple[int, List[Row]]]:
    def fetch(_: str, ids: Dict[str, Any]) -> Tuple[int, List[Row]]:
        url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s&ut=fa5fd1943c7b386f172d6893dbfba10b"
            "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=%d&beg=%s&end=%s"
            % (ids["em"], fqt, START.strftime("%Y%m%d"), TODAY.strftime("%Y%m%d"))
        )
        status, body = http_get(url)
        if status == 429:
            raise Blocked("HTTP 429")
        lines = ((json.loads(body).get("data") or {}).get("klines") or []) if status == 200 else []
        rows = []
        for line in lines:  # f51..f57: 日期,开,收,高,低,量,额
            d, o, c, h, lo, v = line.split(",")[:6]
            rows.append((d, _f(o), _f(h), _f(lo), _f(c), _f(v)))
        return status, rows

    return fetch


def tencent_qfq(_: str, ids: Dict[str, Any]) -> Tuple[int, List[Row]]:
    key = ids["tx"]
    kind = "hkfqkline" if key.startswith("hk") else "usfqkline"
    url = f"https://web.ifzq.gtimg.cn/appstock/app/{kind}/get?param={key},day,,,30,qfq"
    status, body = http_get(url)
    if status == 429:
        raise Blocked("HTTP 429")
    data = (json.loads(body).get("data") or {}).get(key) or {} if status == 200 else {}
    rows = []
    for item in data.get("qfqday") or data.get("day") or []:  # [日期, 开, 收, 高, 低, 量, (分红说明)]
        rows.append((item[0], _f(item[1]), _f(item[3]), _f(item[4]), _f(item[2]), _f(item[5])))
    return status, [r for r in rows if r[0] >= START.isoformat()]


def sina_us(_: str, ids: Dict[str, Any]) -> Tuple[int, List[Row]]:
    if not ids["sina"]:
        raise Unsupported("新浪没有可用的港股日线端点（探测到 Service not valid）")
    url = f"https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20_x=/US_MinKService.getDailyK?symbol={ids['sina']}&type=daily"
    status, body = http_get(url, headers={"Referer": "https://finance.sina.com.cn/"})
    if status != 200:
        return status, []
    payload = body[body.index("([") + 1 : body.rindex("])") + 1]  # 一次返回全部历史（体积大），这里只截近 30 天
    rows = [(i["d"], _f(i["o"]), _f(i["h"]), _f(i["l"]), _f(i["c"]), _f(i["v"])) for i in json.loads(payload)]
    return status, [r for r in rows if r[0] >= START.isoformat()]


def stooq(_: str, ids: Dict[str, Any]) -> Tuple[int, List[Row]]:
    url = f"https://stooq.com/q/d/l/?s={ids['stooq']}&i=d&d1={START:%Y%m%d}&d2={TODAY:%Y%m%d}"
    status, body = http_get(url)
    if "__verify" in body or "<script" in body[:400]:
        raise Blocked("stooq 返回了 JS 工作量证明挑战页（脚本无法通过）")
    rows = []
    for line in body.strip().splitlines()[1:]:
        d, o, h, lo, c, v = (line.split(",") + ["0"])[:6]
        rows.append((d, _f(o), _f(h), _f(lo), _f(c), _f(v or 0)))
    return status, rows


def yahoo_chart(host: str, impersonate: bool = False) -> Callable[[str, Dict[str, Any]], Tuple[int, List[Row]]]:
    def fetch(_: str, ids: Dict[str, Any]) -> Tuple[int, List[Row]]:
        url = f"https://{host}/v8/finance/chart/{ids['yahoo']}?range=1mo&interval=1d&events=div%2Csplits"
        status, body = http_get(url, impersonate=impersonate)
        if status == 429 or body.strip().startswith("Too Many Requests"):
            raise Blocked("HTTP 429（直连 chart 接口，不带 cookie/crumb）")
        if status != 200:
            return status, []
        result = json.loads(body)["chart"]["result"][0]
        offset = (result.get("meta") or {}).get("gmtoffset", 0)
        quote = result["indicators"]["quote"][0]
        adj = ((result["indicators"].get("adjclose") or [{}])[0]).get("adjclose")
        rows = []
        for i, ts in enumerate(result.get("timestamp") or []):
            if quote["close"][i] is None:
                continue
            day = dt.datetime.fromtimestamp(ts + offset, dt.timezone.utc).date().isoformat()
            rows.append((day, quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i], quote["volume"][i] or 0))
        info = ""
        if adj and rows:  # adjclose / close：窗口内没有分红时恒为 1；< 1 说明窗口内有除息，auto_adjust 会把早期价格下调
            first_idx = next(i for i, c in enumerate(quote["close"]) if c is not None)
            info = f"adjclose/close(窗口最早一天)={adj[first_idx] / quote['close'][first_idx]:.4f}"
        return status, rows, info

    return fetch


def yahoo_yfinance(_: str, ids: Dict[str, Any]) -> Tuple[int, List[Row]]:
    try:
        import yfinance as yf
    except ImportError:
        raise Unsupported("镜像外没有 yfinance；在容器里运行才会测这一项") from None
    try:
        frame = yf.Ticker(ids["yahoo"]).history(period="1mo", auto_adjust=True, timeout=15)
    except Exception as exc:  # noqa: BLE001
        if "RateLimit" in type(exc).__name__ or "Too Many" in str(exc):
            raise Blocked(f"{type(exc).__name__}（cookie/crumb 阶段）") from None
        raise
    rows = [(i.date().isoformat(), float(r.Open), float(r.High), float(r.Low), float(r.Close), float(r.Volume)) for i, r in frame.iterrows()]
    return 200, rows


# 名字 → (函数, 复权口径说明, 是否官方/稳定性备注)
CANDIDATES: Dict[str, Tuple[Callable, str, str]] = {
    "yahoo_yfinance": (yahoo_yfinance, "auto_adjust（= 库里的 yahoo_adjusted 口径）", "当前实现；cookie/crumb 阶段会被出口 IP 限流"),
    "yahoo_chart_q1": (yahoo_chart("query1.finance.yahoo.com"), "不复权收盘（响应里另带 adjclose）", "非官方；绕开 cookie/crumb 但仍可能被 IP 限流"),
    "yahoo_chart_q2": (yahoo_chart("query2.finance.yahoo.com"), "不复权收盘（响应里另带 adjclose）", "同上，备用主机名"),
    "yahoo_chart_curl": (yahoo_chart("query1.finance.yahoo.com", impersonate=True), "不复权收盘（响应里另带 adjclose）",
                         "非官方；curl_cffi 带浏览器 TLS 指纹直连 chart，跳过 cookie/crumb 阶段（yfinance 失败的那一步）"),
    "eastmoney_fqt0": (eastmoney(0), "不复权（真·未复权，拆股前价格也是当时的价格）", "非官方；A 股场内已在用同一类接口"),
    "eastmoney_fqt1": (eastmoney(1), "前复权（东财自己的分红/拆股因子）", "同上"),
    "tencent_qfq": (tencent_qfq, "前复权（腾讯自己的因子；行里带分红说明）", "非官方；无鉴权"),
    "sina_us": (sina_us, "疑似不复权（AAPL 1984 年收盘 26.5）；仅美股；一次返回全部历史", "非官方；JSONP，响应体大"),
    "stooq": (stooq, "拆股调整", "近期需要 JS 工作量证明，脚本基本不可用"),
}


# ------------------------------------------------------------------------------------ 运行与输出
def run_one(name: str, symbol: str, sleep: Callable[[float], None] = time.sleep) -> Dict[str, Any]:
    fn, adjust, note = CANDIDATES[name]
    out: Dict[str, Any] = {"source": name, "symbol": symbol, "http": None, "rows": 0, "last_date": None, "adjust": adjust,
                           "ms": None, "requests": 0, "note": note, "series": []}
    for attempt in (1, 2):
        out["requests"] = attempt
        started = time.time()
        try:
            result = fn(symbol, SYMBOLS[symbol])
            status, rows = result[0], result[1]
            if len(result) > 2 and result[2]:
                out["adjust"] += f"；{result[2]}"
            out.update(http=status, rows=len(rows), ms=int((time.time() - started) * 1000), series=rows)
            if rows:
                out["last_date"] = rows[-1][0]
            if status < 500 and status != 429:
                break
        except Unsupported as exc:
            out.update(http="-", ms=0, requests=0, error=f"不支持：{exc}")
            break
        except Blocked as exc:
            out.update(http=429, ms=int((time.time() - started) * 1000), error=f"被拦：{exc}")
            break  # 限流绝不重试
        except Exception as exc:  # noqa: BLE001 - 网络异常 / 解析失败：只重试一次
            out.update(http="ERR", ms=int((time.time() - started) * 1000), error=f"{type(exc).__name__}: {str(exc)[:100]}")
        if attempt == 1:
            sleep(2.0)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", help="只测这些源，逗号分隔；可选：" + ",".join(CANDIDATES))
    ap.add_argument("--symbols", default=",".join(SYMBOLS), help="默认 0700.HK,AAPL")
    ap.add_argument("--json", metavar="PATH", help="把完整结果（含日线）写成 JSON")
    ap.add_argument("--gap", type=float, default=1.0, help="请求之间的最小间隔秒数（默认 1）")
    args = ap.parse_args(argv)
    names = [n for n in (args.only.split(",") if args.only else CANDIDATES) if n in CANDIDATES]
    symbols = [s for s in args.symbols.split(",") if s in SYMBOLS]

    print(f"探测窗口 {START} ~ {TODAY}；每个源对每只标的最多 2 次请求；请求间隔 ≥{args.gap}s")
    results: List[Dict[str, Any]] = []
    total = 0
    for symbol in symbols:
        print(f"\n== {symbol} ==")
        print(f"{'source':16s} {'http':>5s} {'rows':>4s} {'last_date':10s} {'ms':>6s} {'req':>3s}  复权口径 / 备注")
        for name in names:
            r = run_one(name, symbol)
            results.append(r)
            total += r["requests"]
            extra = r.get("error") or r["adjust"]
            print(f"{name:16s} {r['http']!s:>5s} {r['rows']:>4d} {r['last_date'] or '-':10s} {r['ms'] if r['ms'] is not None else '-':>6} {r['requests']:>3d}  {extra}")
            if r["requests"]:
                time.sleep(args.gap)

    print("\n== 同一日期的收盘价对照（判断口径是否一致）==")
    for symbol in symbols:
        series = {r["source"]: {row[0]: row[4] for row in r["series"]} for r in results if r["symbol"] == symbol and r["rows"]}
        if len(series) < 2:
            print(f"{symbol}: 可用的源不足 2 个，无法对照")
            continue
        common = sorted(set.intersection(*(set(s) for s in series.values())))
        if not common:
            print(f"{symbol}: 没有共同日期")
            continue
        first, last = common[0], common[-1]
        base = "eastmoney_fqt0" if "eastmoney_fqt0" in series else next(iter(series))
        print(f"{symbol}: 共同日期 {len(common)} 个；以 {base} 为基准，列出 {first}（窗口最早）与 {last}（最近）的收盘价及相对差异")
        for name, s in series.items():
            d_first = s[first] / series[base][first] - 1
            d_last = s[last] / series[base][last] - 1
            print(f"  {name:16s} {first}: {s[first]:>10.3f} ({d_first:+.3%})   {last}: {s[last]:>10.3f} ({d_last:+.3%})")
    print(f"\n共发出请求 {total} 次。判读：最近日期各源应当几乎相等；窗口最早日期上出现固定比例差异 = 该源做了复权（窗口内发生过分红）。")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1, default=str)
        print(f"完整结果已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
