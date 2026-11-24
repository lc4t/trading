"""上游日线抓取：按标的类型路由，统一返回 ``FetchResult``.

两种抓取口径（``mode``）：

- ``raw``：不复权/仅拆分调整的原始价，历史行不会因分红而改变。
    A 股场内 → 东财 K 线 fqt=0；港股/美股/指数 → Yahoo ``auto_adjust=False``；场外基金 → 东财官方单位净值。
- ``legacy``：与 2.0 之前 trading-fetcher 完全一致的抓取（A 股 ETF 走同花顺前复权全量，其余走 Yahoo
    ``auto_adjust=True``），用于已有遗留行的标的，避免同一序列混入两种价格口径。

港股 / 美股另有**备用源**（``fetch_backup``）：Yahoo 限流时由缓存层按 ``backup_sources`` 的顺序调用，
口径锁与重叠日校验在 ``basis.py`` / ``cache.py``，这里只负责取数和解析。
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

from . import testing_upstream
from .basis import QFQ_TENCENT, UNADJUSTED
from .errors import FetchError, RateLimited

EASTMONEY_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_FUND_NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"
EASTMONEY_FUND_TREND_URL = "https://fund.eastmoney.com/pingzhongdata/{symbol}.js"
_SHANGHAI = ZoneInfo("Asia/Shanghai")

SINA_US_URL = "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20_x=/US_MinKService.getDailyK"
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/{kind}/get"
# 备用源的请求头：探测脚本里验证过的浏览器 UA（东财/腾讯对非浏览器 UA 不一定放行）
BACKUP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36",
    "Accept-Encoding": "identity",
}
EASTMONEY_BACKUP_UT = "fa5fd1943c7b386f172d6893dbfba10b"
EASTMONEY_KLINE_UT = "7eea3edcaed734bea9cbfc24409ed989"  # 东财公开 K 线接口的通用 ut（非账号凭据）
TENCENT_INDEX_MAX_COUNT = 2000  # 腾讯 fqkline 单次最多 2000 根（3000 会 param error），长区间按窗口分段
TENCENT_INDEX_WINDOW_DAYS = 2000  # 每段覆盖的自然日数（约 1400 根），保证落在上限内

# 指数源标识（FetchResult.source，也写入 market_data_meta.source）
INDEX_TENCENT = "tencent_index"
INDEX_EASTMONEY = "eastmoney_index"

# 备用源标识 → 它写入的价格口径
BACKUP_EASTMONEY = "eastmoney_fqt0"
BACKUP_SINA = "sina_us"
BACKUP_TENCENT = "tencent_qfq"
BACKUP_BASIS = {BACKUP_EASTMONEY: UNADJUSTED, BACKUP_SINA: UNADJUSTED, BACKUP_TENCENT: QFQ_TENCENT}

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
# trading-fetcher 批量同步沿用历史行为：Yahoo 限流最多 10 次、30+60*n 秒
SYNC_RETRY = RetryPolicy(attempts=10, delay=lambda n: 30 + 60 * n)
# 备用源：每个 HTTP 请求最多 2 次（含第一次），限流立刻放弃；上限是硬性的，调用方传更大的策略也会被截到 2
BACKUP_MAX_ATTEMPTS = 2
BACKUP_RETRY = RetryPolicy(
    attempts=BACKUP_MAX_ATTEMPTS, delay=lambda n: 0.4 + 0.4 * random.random(), stop_on_rate_limit=True
)
# 指数源：腾讯 3 次（间隔约 1s）；东财 push2his 对指数日线会间歇 RemoteDisconnected（runner 实测单次成功率
# 约 17%~60%），所以单独给 6 次、每次间隔 2~5 秒。**不要**用 SYNC_RETRY（30+60n 秒的长睡眠会拖垮夜间任务）。
TENCENT_INDEX_RETRY = RetryPolicy(
    attempts=3, delay=lambda n: 0.8 + 0.4 * random.random(), stop_on_rate_limit=True
)
EASTMONEY_INDEX_RETRY = RetryPolicy(
    attempts=6, delay=lambda n: 2.0 + 3.0 * random.random(), stop_on_rate_limit=True
)


@dataclass(frozen=True)
class IndexRoute:
    tencent: str  # 腾讯 fqkline 代码
    eastmoney: Optional[str]  # 东财 secid；None = 不依赖东财（^HSI 在 runner 上只成功约 2/12）
    yahoo_primary: bool  # True：Yahoo 仍是主源，腾讯作备源；False：腾讯主源，东财备源，不用 Yahoo


# Yahoo 对 000300.SS 只给最近一根日线，不能做历史源；^HSI 的 Yahoo 历史完整，保持主源
INDEX_ROUTES: Dict[str, IndexRoute] = {
    "000300.SS": IndexRoute("sh000300", "1.000300", False),
    "000300.SH": IndexRoute("sh000300", "1.000300", False),
    "^HSI": IndexRoute("hkHSI", None, True),
}


def is_tencent_primary_index(symbol: str) -> bool:
    route = INDEX_ROUTES.get(symbol)
    return route is not None and not route.yahoo_primary


@dataclass
class FetchResult:
    rows: List[Dict[str, Any]]
    source: str
    price_basis: str
    actions: List[Dict[str, Any]] = field(default_factory=list)
    # True 表示上游总是返回全量历史（同花顺），调用方按需裁剪窗口
    full_history: bool = False
    # 备用源写入时由缓存层填上：{used, reason, source, price_basis, overlap, attempts}；主上游为 None
    fallback: Optional[Dict[str, Any]] = None


def _number(value: Any) -> Optional[float]:
    if value in {None, "", "-", "--"}:
        return None
    number = float(value)
    return number if isfinite(number) else None


def is_china_exchange_symbol(symbol: str) -> bool:
    return symbol.endswith((".SZ", ".SS", ".SH")) and symbol != "000300.SS"


def series_type_for_symbol(symbol: str) -> str:
    return FUND_NAV_SERIES if is_off_market_fund(symbol) else MARKET_PRICE_SERIES


def overseas_kind(symbol: str) -> Optional[str]:
    """港股 / 美股（有备用上游的两类）；指数、期货、加密货币等没有备用源."""
    if re.fullmatch(r"\d{1,5}\.HK", symbol):
        return "hk"
    if re.fullmatch(r"[A-Z]{1,5}", symbol):
        return "us"
    return None


def backup_sources(symbol: str) -> List[str]:
    """Yahoo 不可达时按顺序尝试的备用源：不复权源在前；腾讯前复权只是不复权源都不可用时的最后备选."""
    kind = overseas_kind(symbol)
    if kind == "hk":
        return [BACKUP_EASTMONEY, BACKUP_TENCENT]
    if kind == "us":
        return [BACKUP_SINA, BACKUP_EASTMONEY, BACKUP_TENCENT]
    return []


def upstream_route_for_symbol(symbol: str, mode: str = "raw") -> str:
    if mode == UNADJUSTED:
        return "backup_unadjusted_market_price"
    if mode == QFQ_TENCENT:
        return "backup_tencent_qfq_market_price"
    if is_tencent_primary_index(symbol):
        return "tencent_index_with_eastmoney_fallback"
    if symbol in INDEX_ROUTES:
        return "yahoo_index_with_tencent_fallback"
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
                raise RateLimited("upstream", after or 60.0, "upstream HTTP 429", hinted=after is not None) from exc
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
        if symbol in INDEX_ROUTES:
            return self.fetch_index(symbol, start, end, mode=mode, retry=retry)
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
            "ut": EASTMONEY_KLINE_UT,
            "klt": "101",
            "fqt": str(fqt),
            "secid": f"{market}.{code}",
            "beg": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
        }
        try:
            response = _request_with_retry(
                requests.get,
                testing_upstream.endpoint(EASTMONEY_URL, "/eastmoney/kline"),
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
                    testing_upstream.endpoint(EASTMONEY_FUND_NAV_URL, "/eastmoney/lsjz"),
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
                testing_upstream.endpoint(
                    EASTMONEY_FUND_TREND_URL.format(symbol=symbol), f"/eastmoney/trend/{symbol}.js"
                ),
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

    # ------------------------------------------------------------------ 指数（沪深300 / 恒指）
    def fetch_index(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        mode: str = "raw",
        retry: Optional[RetryPolicy] = None,
        sources: Optional[List[str]] = None,
    ) -> FetchResult:
        """指数日线的取数顺序（任一源失败 / 返回空就换下一个）：

        - 000300.SS / 000300.SH：腾讯 → 东财（Yahoo 只给最近一根，不用）；
        - ^HSI：Yahoo（主源，沿用调用方的重试策略）→ 腾讯 hkHSI；东财 ^HSI 不依赖。

        ``sources`` 可显式指定（回补脚本只用腾讯 / 东财取交易日列表）。全部失败时抛 FetchError
        （全是限流时抛 RateLimited）；全部成功但都为空则返回空结果，由调用方按缓存降级。
        返回的行只含 ``start..end``（调用方保证 ``end`` 不晚于最近一个已收盘的交易日，腾讯带出的当天盘中行会被裁掉）。
        """
        route = INDEX_ROUTES[symbol]
        chain = sources or (["yahoo", INDEX_TENCENT] if route.yahoo_primary else [INDEX_TENCENT, INDEX_EASTMONEY])
        errors: List[str] = []
        limited: Optional[RateLimited] = None
        last_empty: Optional[FetchResult] = None
        for source in chain:
            try:
                if source == "yahoo":
                    result = self.fetch_yahoo(
                        symbol, start, end, retry=retry or self.retry, legacy=(mode == "legacy")
                    )
                elif source == INDEX_TENCENT:
                    result = self.fetch_index_tencent(symbol, start, end, TENCENT_INDEX_RETRY)
                elif source == INDEX_EASTMONEY:
                    result = self.fetch_index_eastmoney(symbol, start, end, EASTMONEY_INDEX_RETRY)
                else:  # pragma: no cover
                    raise FetchError(f"unknown index source {source}")
            except RateLimited as exc:
                limited = exc
                errors.append(f"{source}=rate_limited")
                logger.warning(f"{symbol} 指数源 {source} 限流，换下一个源")
                continue
            except FetchError as exc:
                errors.append(f"{source}={exc}")
                logger.warning(f"{symbol} 指数源 {source} 失败（{exc}），换下一个源")
                continue
            if result.rows:
                if errors:
                    logger.info(f"{symbol} 使用备用指数源 {result.source}（前序失败：{'; '.join(errors)}）")
                return result
            errors.append(f"{source}=empty")
            last_empty = result
            logger.warning(f"{symbol} 指数源 {source} 返回空，换下一个源")
        if last_empty is not None:
            # 至少一个源正常应答但没有数据（例如请求窗口内没有交易日）：交给调用方按缓存降级，而不是当成取数失败
            return last_empty
        if limited is not None and all(e.endswith("=rate_limited") for e in errors):
            raise limited
        raise FetchError("all index sources failed: " + "; ".join(errors))

    def fetch_index_tencent(
        self, symbol: str, start: date, end: date, retry: RetryPolicy = TENCENT_INDEX_RETRY
    ) -> FetchResult:
        """腾讯指数日线（不复权）。每次请求最多 2000 根，长区间按自然日窗口分段取."""
        import requests

        code = INDEX_ROUTES[symbol].tencent
        rows: Dict[date, Dict[str, Any]] = {}
        window_end = end
        while window_end >= start:
            window_start = max(start, window_end - timedelta(days=TENCENT_INDEX_WINDOW_DAYS - 1))
            params = {"param": f"{code},day,{window_start:%Y-%m-%d},{window_end:%Y-%m-%d},{TENCENT_INDEX_MAX_COUNT},"}
            try:
                response = _request_with_retry(
                    requests.get,
                    testing_upstream.endpoint(TENCENT_KLINE_URL.format(kind="fqkline"), "/tencent/index"),
                    retry,
                    params=params,
                    headers=BACKUP_HEADERS,
                    timeout=15,
                )
                payload = response.json()
            except RateLimited:
                raise
            except Exception as exc:  # noqa: BLE001
                raise _backup_error(INDEX_TENCENT, retry, exc) from exc
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):  # 参数非法时 data 是 []
                raise FetchError(f"{INDEX_TENCENT} rejected the request ({str(payload)[:80]})")
            items = (data.get(code) or {}).get("day") or []
            for item in items:  # [日期, 开, 收, 高, 低, 量]
                try:
                    day = date.fromisoformat(item[0])
                    if start <= day <= end:
                        rows[day] = _backup_row(symbol, day, item[1], item[3], item[4], item[2], item[5], None)
                except (ValueError, TypeError, IndexError) as exc:
                    raise FetchError(f"{INDEX_TENCENT} returned an unparsable row") from exc
            window_end = window_start - timedelta(days=1)
        return FetchResult([rows[d] for d in sorted(rows)], INDEX_TENCENT, "raw")

    def fetch_index_eastmoney(
        self, symbol: str, start: date, end: date, retry: RetryPolicy = EASTMONEY_INDEX_RETRY
    ) -> FetchResult:
        """东财指数日线（fqt=0）。断连时按 ``retry`` 短间隔重试；没有东财 secid 的指数直接报错."""
        import requests

        secid = INDEX_ROUTES[symbol].eastmoney
        if secid is None:
            raise FetchError(f"{INDEX_EASTMONEY} is not configured for {symbol}")
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57",
            "ut": EASTMONEY_KLINE_UT,
            "klt": "101",
            "fqt": "0",
            "secid": secid,
            "beg": start.strftime("%Y%m%d"),
            "end": end.strftime("%Y%m%d"),
        }
        try:
            response = _request_with_retry(
                requests.get,
                testing_upstream.endpoint(EASTMONEY_URL, "/eastmoney/kline"),
                retry,
                params=params,
                headers=BACKUP_HEADERS,
                timeout=15,
            )
            lines = (response.json().get("data") or {}).get("klines") or []
        except RateLimited:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _backup_error(INDEX_EASTMONEY, retry, exc) from exc
        rows: List[Dict[str, Any]] = []
        for line in lines:  # f51..f57：日期,开,收,高,低,量,额
            fields = line.split(",")
            if len(fields) < 6:
                continue
            try:
                day = date.fromisoformat(fields[0])
                if start <= day <= end:
                    rows.append(
                        _backup_row(symbol, day, fields[1], fields[3], fields[4], fields[2], fields[5],
                                    _number(fields[6]) if len(fields) > 6 else None)
                    )
            except (ValueError, TypeError) as exc:
                raise FetchError(f"{INDEX_EASTMONEY} returned an unparsable row") from exc
        return FetchResult(rows, INDEX_EASTMONEY, "raw")

    # -------------------------------------------------------------------- ths
    def fetch_ths_etf(self, symbol: str) -> FetchResult:
        """同花顺 ETF 日线（前复权，全量历史，忽略区间）——历史 trading-fetcher 的抓取方式."""
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
            # 价格口径与历史完全一致（auto_adjust 默认 True）；多要分红/拆股事件，用来判断何时必须整段重写
            # Yahoo 的 end 不含当日，所以要 +1 天才能拿到 ``end`` 这一天已完成的日线（调用方保证 ``end``
            # 不晚于最近一个已收盘的交易日；下面的循环仍会丢掉晚于 ``end`` 的行）
            kwargs: Dict[str, Any] = {"start": start, "end": end + timedelta(days=1), "actions": True}
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
                frame = (testing_upstream.ticker_class() or yf.Ticker)(symbol).history(**kwargs)
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
            dividend = _number(record.get("Dividends", 0))
            split = _number(record.get("Stock Splits", 0))
            if dividend:
                actions.append({"ex_date": trade_date, "kind": "dividend", "value": dividend})
            if split:
                actions.append({"ex_date": trade_date, "kind": "split", "value": split})
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
        if legacy:
            return FetchResult(rows, "yahoo_auto_adjust", "yahoo_adjusted", actions=actions)
        return FetchResult(rows, "yahoo_unadjusted", "split_adjusted", actions=actions)

    # ------------------------------------------------------------ 备用上游（Yahoo 不可达时）
    def fetch_backup(
        self, symbol: str, start: date, end: date, source: str, retry: Optional[RetryPolicy] = None
    ) -> FetchResult:
        """港股 / 美股的备用源；每个 HTTP 请求最多 2 次，失败给出带来源和次数的明确错误."""
        retry = _clamp_backup_retry(retry or self.retry)
        kind = overseas_kind(symbol)
        if kind is None or source not in BACKUP_BASIS:
            raise FetchError(f"{source} does not support {symbol}")
        if source == BACKUP_EASTMONEY:
            return self._backup_eastmoney(symbol, kind, start, end, retry)
        if source == BACKUP_SINA:
            if kind != "us":
                raise FetchError("sina_us only supports US symbols")
            return self._backup_sina_us(symbol, start, end, retry)
        return self._backup_tencent(symbol, kind, start, end, retry)

    def _backup_eastmoney(self, symbol: str, kind: str, start: date, end: date, retry: RetryPolicy) -> FetchResult:
        """东财 K 线 fqt=0（真·不复权）。港股 ``116.<5 位代码>``；美股交易所前缀不固定，先 105（纳斯达克）再 106（纽交所）."""
        import requests

        code = symbol.split(".")[0]
        secids = [f"116.{code.zfill(5)}"] if kind == "hk" else [f"105.{code}", f"106.{code}"]
        lines: List[str] = []
        for secid in secids:
            params = {
                "secid": secid,
                "ut": EASTMONEY_BACKUP_UT,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
                "klt": "101",
                "fqt": "0",
                "beg": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
            }
            try:
                response = _request_with_retry(
                    requests.get,
                    testing_upstream.endpoint(EASTMONEY_URL, "/eastmoney/kline"),
                    retry,
                    params=params,
                    headers=BACKUP_HEADERS,
                    timeout=15,
                )
            except RateLimited:
                raise
            except Exception as exc:  # noqa: BLE001
                raise _backup_error(BACKUP_EASTMONEY, retry, exc) from exc
            try:
                lines = (response.json().get("data") or {}).get("klines") or []
            except (ValueError, AttributeError) as exc:
                raise FetchError(f"{BACKUP_EASTMONEY} returned an unparsable response") from exc
            if lines:
                break
        rows: List[Dict[str, Any]] = []
        for line in lines:
            fields = line.split(",")  # f51..f57：日期,开,收,高,低,量,额（港美股的量是股数，不是手）
            if len(fields) < 6:
                continue
            try:
                day = date.fromisoformat(fields[0])
                if not start <= day <= end:
                    continue
                rows.append(
                    _backup_row(symbol, day, fields[1], fields[3], fields[4], fields[2], fields[5], _number(fields[6]) if len(fields) > 6 else None)
                )
            except (ValueError, TypeError) as exc:
                raise FetchError(f"{BACKUP_EASTMONEY} returned an unparsable row") from exc
        return FetchResult(rows, BACKUP_EASTMONEY, UNADJUSTED)

    def _backup_sina_us(self, symbol: str, start: date, end: date, retry: RetryPolicy) -> FetchResult:
        """新浪美股日线：JSONP，一次返回全部历史（不复权），这里只截请求窗口."""
        import requests

        try:
            response = _request_with_retry(
                requests.get,
                testing_upstream.endpoint(SINA_US_URL, "/sina/us"),
                retry,
                params={"symbol": symbol.lower(), "type": "daily"},
                headers={**BACKUP_HEADERS, "Referer": "https://finance.sina.com.cn/"},
                timeout=20,
            )
            body = response.text
        except RateLimited:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _backup_error(BACKUP_SINA, retry, exc) from exc
        try:
            payload = json.loads(body[body.index("([") + 1 : body.rindex("])") + 1])
            rows = []
            for item in payload:
                day = date.fromisoformat(item["d"])
                if start <= day <= end:
                    rows.append(
                        _backup_row(symbol, day, item["o"], item["h"], item["l"], item["c"], item["v"], None)
                    )
        except (ValueError, KeyError, TypeError) as exc:
            raise FetchError(f"{BACKUP_SINA} returned an unparsable response") from exc
        return FetchResult(rows, BACKUP_SINA, UNADJUSTED)

    def _backup_tencent(self, symbol: str, kind: str, start: date, end: date, retry: RetryPolicy) -> FetchResult:
        """腾讯前复权日线（只作为不复权源都失败时的备选，口径 ``qfq_tencent``，不与不复权行混写）."""
        import requests

        code = symbol.split(".")[0]
        keys = [f"hk{code.zfill(5)}"] if kind == "hk" else [f"us{code}.OQ", f"us{code}.N"]
        path = "hkfqkline" if kind == "hk" else "usfqkline"
        count = min(max((date.today() - start).days, (end - start).days) + 5, 1500)
        items: List[Any] = []
        for key in keys:
            try:
                response = _request_with_retry(
                    requests.get,
                    testing_upstream.endpoint(TENCENT_KLINE_URL.format(kind=path), "/tencent/kline"),
                    retry,
                    params={"param": f"{key},day,,,{count},qfq"},
                    headers=BACKUP_HEADERS,
                    timeout=15,
                )
            except RateLimited:
                raise
            except Exception as exc:  # noqa: BLE001
                raise _backup_error(BACKUP_TENCENT, retry, exc) from exc
            try:
                data = ((response.json().get("data") or {}).get(key)) or {}
            except (ValueError, AttributeError) as exc:
                raise FetchError(f"{BACKUP_TENCENT} returned an unparsable response") from exc
            items = data.get("qfqday") or data.get("day") or []
            if items:
                break
        rows: List[Dict[str, Any]] = []
        for item in items:  # [日期, 开, 收, 高, 低, 量, (分红说明)]
            try:
                day = date.fromisoformat(item[0])
                if start <= day <= end:
                    rows.append(_backup_row(symbol, day, item[1], item[3], item[4], item[2], item[5], None))
            except (ValueError, TypeError, IndexError) as exc:
                raise FetchError(f"{BACKUP_TENCENT} returned an unparsable row") from exc
        return FetchResult(rows, BACKUP_TENCENT, QFQ_TENCENT)


def _clamp_backup_retry(retry: RetryPolicy) -> RetryPolicy:
    """备用源的重试上限是硬性的：最多 2 次，且限流时绝不重试."""
    return RetryPolicy(
        attempts=max(1, min(retry.attempts, BACKUP_MAX_ATTEMPTS)),
        delay=retry.delay if retry.attempts <= BACKUP_MAX_ATTEMPTS else BACKUP_RETRY.delay,
        stop_on_rate_limit=True,
    )


def root_cause_name(exc: BaseException) -> str:
    """网络异常常被层层包装（requests.ConnectionError → ProtocolError → RemoteDisconnected）；取最里面一层的类名."""
    current: BaseException = exc
    for _ in range(6):
        inner = next((a for a in current.args if isinstance(a, BaseException)), None) or current.__cause__
        if inner is None or inner is current:
            break
        current = inner
    return type(current).__name__


def _backup_error(source: str, retry: RetryPolicy, exc: BaseException) -> FetchError:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    detail = f"HTTP {status}" if status else root_cause_name(exc)
    return FetchError(f"{source} request failed after {retry.attempts} attempt(s) ({detail})")


def _backup_row(symbol: str, day: date, o: Any, h: Any, lo: Any, c: Any, volume: Any, amount: Optional[float]) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "date": day,
        "open_price": float(o),
        "close_price": float(c),
        "high": float(h),
        "low": float(lo),
        "volume": int(float(volume)) if volume not in {None, ""} else 0,
        "amount": amount,
        "change": None,
        "change_pct": None,
    }


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
    if testing_upstream.base_url():
        data = testing_upstream.ths_frame(symbol.split(".")[0])
    else:
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
