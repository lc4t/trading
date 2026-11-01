"""上游日线抓取：按标的类型路由，统一返回 ``FetchResult``.

两种抓取口径（``mode``）：

- ``raw``：不复权/仅拆分调整的原始价，历史行不会因分红而改变。
    A 股场内 → 东财 K 线 fqt=0；港股/美股/指数 → Yahoo ``auto_adjust=False``；场外基金 → 东财官方单位净值。
- ``legacy``：与 2.0 之前 tt-fetcher 完全一致的抓取（A 股 ETF 走同花顺前复权全量，其余走 Yahoo
    ``auto_adjust=True``），用于已有遗留行的标的，避免同一序列混入两种价格口径。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from math import isfinite
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from loguru import logger

from .clock import is_off_market_fund
import random

from .errors import FetchError, RateLimited

EASTMONEY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_FUND_NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"
EASTMONEY_FUND_TREND_URL = "https://fund.eastmoney.com/pingzhongdata/{symbol}.js"
_SHANGHAI = ZoneInfo("Asia/Shanghai")

LONG_NAV_SPAN_DAYS = 1100  # 超过约 3 年的净值区间直接走 trend 脚本
MAX_NAV_PAGES = 400  # lsjz 每页 49 条，约 19600 条

FUND_NAV_SERIES = "fund_nav"
MARKET_PRICE_SERIES = "market_price"


@dataclass(frozen=True)
class RetryPolicy:
    """有上限的重试策略；``delay(n)`` 给出第 n 次失败后的等待秒数."""

    attempts: int = 2
    delay: Callable[[int], float] = lambda n: 0.5
    # 请求路径：命中限流就立刻放弃（交给冷却保护），绝不在限流时重试加重封禁
    stop_on_rate_limit: bool = False


# 服务请求路径：最多 2 次，间隔 ≤ 1s，绝不长时间 sleep
REQUEST_RETRY = RetryPolicy(
    attempts=2, delay=lambda n: 0.4 + 0.4 * random.random(), stop_on_rate_limit=True
)  # 退避带抖动
# tt-fetcher 批量同步沿用历史行为：Yahoo 限流最多 10 次、30+60*n 秒
SYNC_RETRY = RetryPolicy(attempts=10, delay=lambda n: 30 + 60 * n)


@dataclass
class FetchResult:
    rows: List[Dict[str, Any]]
    source: str
    price_basis: str
    actions: List[Dict[str, Any]] = field(default_factory=list)
    # True 表示上游总是返回全量历史（同花顺），调用方按需裁剪窗口
    full_history: bool = False


def _number(value: Any) -> Optional[float]:
    if value in {None, "", "-", "--"}:
        return None
    number = float(value)
    return number if isfinite(number) else None


def is_china_exchange_symbol(symbol: str) -> bool:
    return symbol.endswith((".SZ", ".SS", ".SH")) and symbol != "000300.SS"


def series_type_for_symbol(symbol: str) -> str:
    return FUND_NAV_SERIES if is_off_market_fund(symbol) else MARKET_PRICE_SERIES


def upstream_route_for_symbol(symbol: str, mode: str = "raw") -> str:
    if is_off_market_fund(symbol):
        return "eastmoney_off_market_fund_nav_with_trend_fallback"
    if is_china_exchange_symbol(symbol):
        return "eastmoney_exchange_price" if mode == "raw" else "ths_etf_forward_adjusted"
    return "yahoo_unadjusted_market_price" if mode == "raw" else "yahoo_adjusted_market_price"


def _retry_after_seconds(headers: Any) -> Optional[float]:
    try:
        value = (headers or {}).get("Retry-After")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _request_with_retry(get: Any, url: str, retry: RetryPolicy, **kwargs: Any) -> Any:
    last: Exception | None = None
    for attempt in range(retry.attempts):
        try:
            response = get(url, **kwargs)
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001 - 统一归一成上游失败
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 429:
                after = _retry_after_seconds(getattr(exc.response, "headers", None))
                raise RateLimited("upstream", after or 60.0, "upstream HTTP 429") from exc
            last = exc
            if attempt < retry.attempts - 1:
                time.sleep(retry.delay(attempt))
    assert last is not None
    raise last


class RangePriceFetcher:
    """按请求区间抓取日线（含场外基金净值）."""

    def __init__(self, retry: RetryPolicy = REQUEST_RETRY) -> None:
        self.retry = retry

    def fetch(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        mode: str = "raw",
        retry: Optional[RetryPolicy] = None,
    ) -> FetchResult:
        retry = retry or self.retry
        if is_off_market_fund(symbol):
            return self.fetch_fund_nav(symbol, start, end, retry)
        if is_china_exchange_symbol(symbol):
            if mode == "legacy":
                return self.fetch_ths_etf(symbol)
            return self.fetch_eastmoney(symbol, start, end, retry)
        return self.fetch_yahoo(symbol, start, end, retry=retry, legacy=(mode == "legacy"))

    # ---------------------------------------------------------------- eastmoney
    def fetch_eastmoney(
        self, symbol: str, start: date, end: date, retry: RetryPolicy, *, fqt: int = 0
    ) -> FetchResult:
        import requests

        code, suffix = symbol.rsplit(".", 1)
        market = "1" if suffix in {"SS", "SH"} else "0"
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "ut": "7eea3edcaed734bea9cbfc24409ed989",
            "klt": "101",
            "fqt": str(fqt),
            "secid": f"{market}.{code}",
            "beg": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
        }
        try:
            response = _request_with_retry(
                requests.get,
                EASTMONEY_URL,
                retry,
                params=params,
                headers={"User-Agent": "trading-market/1.0"},
                timeout=20,
            )
            lines = (response.json().get("data") or {}).get("klines") or []
        except RateLimited:
            raise
        except Exception as exc:
            raise FetchError(f"Eastmoney range request failed ({type(exc).__name__})") from exc

        rows: List[Dict[str, Any]] = []
        for line in lines:
            fields = line.split(",")
            if len(fields) < 11:
                continue
            trade_date = date.fromisoformat(fields[0])
            if not start <= trade_date <= end:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "date": trade_date,
                    "open_price": float(fields[1]),
                    "close_price": float(fields[2]),
                    "high": float(fields[3]),
                    "low": float(fields[4]),
                    "volume": int(float(fields[5])) * 100,
                    "amount": _number(fields[6]),
                    "change": _number(fields[9]),
                    "change_pct": _number(fields[8]),
                }
            )
        return FetchResult(rows, "eastmoney_exchange_price", "raw" if fqt == 0 else "ths_qfq")

    def fetch_fund_nav(
        self, symbol: str, start: date, end: date, retry: RetryPolicy
    ) -> FetchResult:
        """场外基金官方单位净值：先走净值接口，失败/为空再走历史趋势脚本."""
        import requests

        # 跨度很长时 lsjz 要翻上百页（每页 49 条，串行），改成先走一次请求拿全量的 trend 脚本
        if (end - start).days > LONG_NAV_SPAN_DAYS:
            try:
                return self._fund_trend(symbol, start, end, retry)
            except RateLimited:
                raise
            except FetchError:
                pass
        headers = {
            "User-Agent": "trading-market/1.0",
            "Referer": f"https://fundf10.eastmoney.com/jjjz_{symbol}.html",
        }
        try:
            rows: List[Dict[str, Any]] = []
            complete = False
            for page in range(1, MAX_NAV_PAGES + 1):
                response = _request_with_retry(
                    requests.get,
                    EASTMONEY_FUND_NAV_URL,
                    retry,
                    params={
                        "fundCode": symbol,
                        "pageIndex": page,
                        "pageSize": 49,
                        "startDate": start.isoformat(),
                        "endDate": end.isoformat(),
                    },
                    headers=headers,
                    timeout=15,
                )
                payload = response.json()
                if payload.get("ErrCode") not in {None, 0}:
                    raise FetchError("Eastmoney fund NAV response reported an error")
                items = (payload.get("Data") or {}).get("LSJZList") or []
                if not items:
                    complete = True
                    break
                observed: list[date] = []
                for item in items:
                    raw_date, raw_nav = item.get("FSRQ"), item.get("DWJZ")
                    if not raw_date or raw_nav in {None, ""}:
                        continue
                    nav_date = date.fromisoformat(raw_date)
                    observed.append(nav_date)
                    if start <= nav_date <= end:
                        rows.append(_nav_row(symbol, nav_date, float(raw_nav), item.get("JZZZL")))
                # 接口返回的页数不可靠，按实际日期覆盖范围停止翻页
                if observed and min(observed) <= start:
                    complete = True
                    break
            # 翻到页数上限还没覆盖到 start 就是不完整，宁可回退也不返回被截断的结果
            if rows and complete:
                return FetchResult(
                    sorted(rows, key=lambda r: r["date"]), "eastmoney_fund_nav_api", "raw"
                )
        except RateLimited:
            raise
        except Exception:  # noqa: BLE001 - 回退到趋势脚本
            pass
        return self._fund_trend(symbol, start, end, retry)

    def _fund_trend(self, symbol: str, start: date, end: date, retry: RetryPolicy) -> FetchResult:
        import requests

        try:
            response = _request_with_retry(
                requests.get,
                EASTMONEY_FUND_TREND_URL.format(symbol=symbol),
                retry,
                headers={
                    "User-Agent": "trading-market/1.0",
                    "Referer": f"https://fund.eastmoney.com/{symbol}.html",
                },
                timeout=15,
            )
            match = re.search(
                r"Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", response.text, flags=re.DOTALL
            )
            if match is None:
                raise FetchError("Eastmoney fund trend payload is missing NAV data")
            rows: List[Dict[str, Any]] = []
            for item in json.loads(match.group(1)):
                ts, nav = item.get("x"), item.get("y")
                if ts in {None, ""} or nav in {None, ""}:
                    continue
                # x 是北京时间 00:00 的毫秒时间戳，必须按 Asia/Shanghai 取日期
                nav_date = dt.datetime.fromtimestamp(float(ts) / 1000, tz=_SHANGHAI).date()
                if start <= nav_date <= end:
                    rows.append(_nav_row(symbol, nav_date, float(nav), item.get("equityReturn")))
            if not rows:
                raise FetchError("Eastmoney fund trend returned no NAV rows in range")
            return FetchResult(
                sorted(rows, key=lambda r: r["date"]), "eastmoney_fund_nav_trend_fallback", "raw"
            )
        except (FetchError, RateLimited):
            raise
        except Exception as exc:
            raise FetchError(
                f"Eastmoney fund NAV primary and fallback failed ({type(exc).__name__})"
            ) from exc

    # -------------------------------------------------------------------- ths
    def fetch_ths_etf(self, symbol: str) -> FetchResult:
        """同花顺 ETF 日线（前复权，全量历史，忽略区间）——历史 tt-fetcher 的抓取方式."""
        try:
            rows = ths_etf_rows(symbol)
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError(f"THS ETF request failed ({type(exc).__name__})") from exc
        return FetchResult(rows, "ths_etf_forward_adjusted", "ths_qfq", full_history=True)

    # ------------------------------------------------------------------ yahoo
    def fetch_yahoo(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        retry: RetryPolicy,
        legacy: bool = False,
    ) -> FetchResult:
        import yfinance as yf

        try:
            from yfinance.exceptions import YFRateLimitError
        except ImportError:  # pragma: no cover
            YFRateLimitError = ()  # type: ignore[assignment]

        if legacy:
            kwargs: Dict[str, Any] = {"start": start, "end": end, "actions": False}
        else:
            kwargs = {
                "start": start,
                "end": end + timedelta(days=1),
                "actions": True,
                "auto_adjust": False,
                "timeout": 20,
            }
        frame = None
        last: Exception | None = None
        for attempt in range(retry.attempts):
            try:
                frame = yf.Ticker(symbol).history(**kwargs)
                break
            except YFRateLimitError as exc:
                last = exc
                logger.warning(f"Yahoo 限流 {symbol}（第 {attempt + 1}/{retry.attempts} 次）")
                if retry.stop_on_rate_limit:
                    raise RateLimited("yahoo", 120.0, "Yahoo Finance rate limited") from exc
                if attempt < retry.attempts - 1:
                    time.sleep(retry.delay(attempt))
            except Exception as exc:  # noqa: BLE001
                last = exc
                break
        if frame is None:
            raise FetchError(
                f"Yahoo Finance range request failed ({type(last).__name__})"
            ) from last

        rows: List[Dict[str, Any]] = []
        actions: List[Dict[str, Any]] = []
        for index, record in frame.iterrows():
            trade_date = index.date()
            if not start <= trade_date <= end:
                continue
            o, c = _number(record["Open"]), _number(record["Close"])
            h, lo = _number(record["High"]), _number(record["Low"])
            if None in {o, c, h, lo}:
                # 盘中/未完成的行 OHLC 可能是 NaN，不是可用日线
                continue
            volume = _number(record["Volume"])
            rows.append(
                {
                    "symbol": symbol,
                    "date": trade_date,
                    "open_price": o,
                    "close_price": c,
                    "high": h,
                    "low": lo,
                    "volume": int(volume) if volume is not None else 0,
                    "amount": None,
                    "change": None,
                    "change_pct": None,
                }
            )
            if not legacy:
                dividend = _number(record.get("Dividends", 0))
                split = _number(record.get("Stock Splits", 0))
                if dividend:
                    actions.append({"ex_date": trade_date, "kind": "dividend", "value": dividend})
                if split:
                    actions.append({"ex_date": trade_date, "kind": "split", "value": split})
        if legacy:
            return FetchResult(rows, "yahoo_auto_adjust", "yahoo_adjusted")
        return FetchResult(rows, "yahoo_unadjusted", "split_adjusted", actions=actions)


def _nav_row(symbol: str, day: date, nav: float, pct: Any) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "date": day,
        "open_price": nav,
        "close_price": nav,
        "high": nav,
        "low": nav,
        "volume": 0,
        "amount": None,
        "change": None,
        "change_pct": _number(pct),
    }


def ths_etf_rows(symbol: str) -> List[Dict[str, Any]]:
    """同花顺 ETF 日线（前复权）。保持与历史 ``ADataFetcher`` 完全一致的行内容."""
    import adata
    import pandas as pd

    logger.info(f"正在获取 {symbol} 的数据（同花顺 ETF 前复权全量）")
    data = adata.fund.market.get_market_etf(symbol.split(".")[0], k_type=1)
    if isinstance(data, Exception):
        # adata 在同花顺 IP 限制时返回的是 Exception 对象而不是抛出
        raise RateLimited("ths", 600.0, "THS ETF request hit the IP limit")
    if data.empty:
        logger.warning(f"No data fetched for {symbol} from AData")
        return []
    df = pd.DataFrame(data)
    logger.info(
        f"{symbol} 数据统计:\n价格范围: {df['low'].min()}-{df['high'].max()}\n"
        f"成交量范围: {df['volume'].min()}-{df['volume'].max()}"
    )
    result = []
    for _, record in df.iterrows():
        result.append(
            {
                "symbol": symbol,
                "date": dt.datetime.strptime(record["trade_date"], "%Y-%m-%d").date(),
                "open_price": float(record["open"]),
                "close_price": float(record["close"]),
                "high": float(record["high"]),
                "low": float(record["low"]),
                "volume": int(record["volume"]),
                "amount": float(record["amount"]) if pd.notna(record["amount"]) else None,
                "change": float(record["change"]) if pd.notna(record["change"]) else None,
                "change_pct": float(record["change_pct"])
                if pd.notna(record["change_pct"])
                else None,
            }
        )
    return result
