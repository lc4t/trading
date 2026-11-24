"""Runner 探针：从当前机器测试各家指数日线接口是否可达、稳定、数据是否完整.

只做 GET 公开行情，不带任何令牌 / Cookie，输出只有状态码、耗时、条数、最后一个日期。
用法：python scripts/probe/runner_index_probe.py [--rounds 12] [--gap 2]
"""
import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request

EM = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
TX = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
UT = "7eea3edcaed734bea9cbfc24409ed989"  # 东财公开 K 线接口的通用 ut，仓库里已有同样用法


def em_url(secid: str) -> str:
    q = {"fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57", "ut": UT,
         "klt": "101", "fqt": "0", "secid": secid, "beg": "20241201", "end": "20500101"}
    return EM + "?" + urllib.parse.urlencode(q)


def tx_url(code: str) -> str:
    return TX + "?" + urllib.parse.urlencode({"param": f"{code},day,2024-12-01,2050-01-01,800,"})


def em_parse(body: bytes):
    lines = (json.loads(body).get("data") or {}).get("klines") or []
    return len(lines), (lines[-1].split(",")[0] if lines else None)


def tx_parse(code: str):
    def parse(body: bytes):
        data = json.loads(body)["data"][code]
        days = data.get("day") or data.get("qfqday") or []
        return len(days), (days[-1][0] if days else None)
    return parse


TARGETS = [
    ("eastmoney 000300", em_url("1.000300"), em_parse),
    ("eastmoney ^HSI", em_url("100.HSI"), em_parse),
    ("tencent sh000300", tx_url("sh000300"), tx_parse("sh000300")),
    ("tencent hkHSI", tx_url("hkHSI"), tx_parse("hkHSI")),
]


def once(url, parse):
    req = urllib.request.Request(url, headers={"User-Agent": "trading-market/1.0"})
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read()
            rows, last = parse(body)
            return True, resp.status, time.time() - start, rows, last
    except urllib.error.HTTPError as exc:
        return False, exc.code, time.time() - start, 0, None
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__, time.time() - start, 0, None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=12)
    ap.add_argument("--gap", type=float, default=2.0)
    args = ap.parse_args()
    for name, url, parse in TARGETS:
        ok, lat, errors, shape = 0, [], {}, set()
        for _ in range(args.rounds):
            good, status, secs, rows, last = once(url, parse)
            if good and rows:
                ok += 1
                lat.append(secs)
                shape.add((rows, last))
            else:
                errors[str(status)] = errors.get(str(status), 0) + 1
            time.sleep(args.gap)
        avg = sum(lat) / len(lat) if lat else 0.0
        print(f"{name:20s} ok {ok}/{args.rounds} avg={avg:.2f}s max={max(lat) if lat else 0:.2f}s "
              f"errors={errors or '-'} rows/last={sorted(shape)[-1] if shape else '-'}")


if __name__ == "__main__":
    main()
