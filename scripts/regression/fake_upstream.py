"""限流演练用的本地假上游：模拟 Yahoo / 东财（K 线、基金净值）/ 同花顺，可随时切换成限流，并统计每个来源收到的请求数."""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List
from urllib.parse import parse_qs, urlparse

SOURCES = ("yahoo", "eastmoney", "eastmoney_fund", "ths")


def _weekdays(start: dt.date, end: dt.date) -> List[dt.date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def _price(symbol: str, day: dt.date) -> float:
    return round(10 + (sum(map(ord, symbol)) % 50) + (day.toordinal() % 17) * 0.1, 3)


class FakeUpstream:
    def __init__(self) -> None:
        self.mode: Dict[str, str] = {s: "ok" for s in SOURCES}  # ok | limit
        self.delay: Dict[str, float] = {s: 0.0 for s in SOURCES}
        self.retry_after_header: Dict[str, str] = {}
        self.hits: Counter = Counter()
        self.log: List[tuple] = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # 静音
                pass

            def do_GET(self):
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                source = outer.classify(url.path)
                if source is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                with outer._lock:
                    outer.hits[source] += 1
                    outer.log.append((time.time(), source, url.path))
                if outer.delay[source]:
                    time.sleep(outer.delay[source])
                if outer.mode[source] == "limit":
                    if source == "ths":
                        return self._send(200, "IP_LIMIT", "text/plain")
                    headers = {"Retry-After": outer.retry_after_header[source]} if source in outer.retry_after_header else {}
                    return self._send(429, "Too Many Requests", "text/plain", headers)
                body, ctype = outer.render(source, url.path, q)
                self._send(200, body, ctype)

            def _send(self, code, body, ctype, headers=None):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @staticmethod
    def classify(path: str):
        if path.startswith("/yahoo/"):
            return "yahoo"
        if path.startswith("/eastmoney/kline"):
            return "eastmoney"
        if path.startswith("/eastmoney/lsjz") or path.startswith("/eastmoney/trend/"):
            return "eastmoney_fund"
        if path.startswith("/ths/"):
            return "ths"
        return None

    def render(self, source: str, path: str, q: Dict[str, str]):
        today = dt.date.today()
        if source == "yahoo":
            symbol = path.rsplit("/", 1)[-1]
            start = dt.date.fromisoformat(q["start"][:10])
            end = min(dt.date.fromisoformat(q["end"][:10]), today)
            rows = [
                {"date": d.isoformat(), "Open": _price(symbol, d), "High": _price(symbol, d) + 1, "Low": _price(symbol, d) - 1,
                 "Close": _price(symbol, d), "Volume": 1000, "dividends": 0.0, "splits": 0.0}
                for d in _weekdays(start, end)
            ]
            return json.dumps({"rows": rows}), "application/json"
        if source == "eastmoney":
            code = q["secid"].split(".")[1]
            start = dt.datetime.strptime(q["beg"], "%Y%m%d").date()
            end = dt.datetime.strptime(q["end"], "%Y%m%d").date()
            lines = [
                f"{d.isoformat()},{_price(code, d)},{_price(code, d)},{_price(code, d) + 1},{_price(code, d) - 1},100,1000000,0,0.5,0.01,1.0"
                for d in _weekdays(start, min(end, today))
            ]
            return json.dumps({"data": {"klines": lines}}), "application/json"
        if source == "eastmoney_fund":
            if "trend" in path:
                code = path.rsplit("/", 1)[-1].replace(".js", "")
                pts = [{"x": int(dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp() * 1000), "y": _price(code, d), "equityReturn": 0.1}
                       for d in _weekdays(today - dt.timedelta(days=60), today)]
                return "Data_netWorthTrend = " + json.dumps(pts) + ";", "text/javascript"
            if int(q.get("pageIndex", "1")) > 1:
                return json.dumps({"ErrCode": 0, "Data": {"LSJZList": []}}), "application/json"
            code = q["fundCode"]
            start = dt.date.fromisoformat(q["startDate"])
            end = min(dt.date.fromisoformat(q["endDate"]), today)
            items = [{"FSRQ": d.isoformat(), "DWJZ": str(_price(code, d)), "JZZZL": "0.1"} for d in reversed(_weekdays(start, end))][:49]
            return json.dumps({"ErrCode": 0, "Data": {"LSJZList": items}}), "application/json"
        if source == "ths":
            code = path.rsplit("/", 1)[-1]
            rows = [
                {"trade_date": d.isoformat(), "open": str(_price(code, d)), "high": str(_price(code, d) + 1), "low": str(_price(code, d) - 1),
                 "close": str(_price(code, d)), "volume": "1000", "amount": 1000000.0, "change": 0.1, "change_pct": 0.5}
                for d in _weekdays(today - dt.timedelta(days=90), today - dt.timedelta(days=1))
            ]
            return json.dumps({"rows": rows}), "application/json"
        raise AssertionError(source)

    def snapshot(self) -> Counter:
        with self._lock:
            return Counter(self.hits)

    def close(self) -> None:
        self.server.shutdown()
