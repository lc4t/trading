"""业务时钟与「当日数据是否已定稿」的判断.

约定：
- 业务日 = Asia/Shanghai 的自然日（请求的 end 不能晚于它）。
- 库里所有元数据时间戳（fetched_at / as_of）存 naive UTC，对外序列化成 Asia/Shanghai 带偏移的 ISO8601。
- 一行日线只有在「抓取时刻 ≥ 该交易日收盘 + 缓冲」时才算 ``is_final``。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("Asia/Shanghai")
UTC = dt.timezone.utc

# 收盘后留一段缓冲，覆盖尾盘集合竞价和上游数据延迟
SETTLE_BUFFER = dt.timedelta(minutes=30)


@dataclass(frozen=True)
class MarketSession:
    name: str
    tz: ZoneInfo
    close: dt.time


_CN = MarketSession("cn", ZoneInfo("Asia/Shanghai"), dt.time(15, 0))
_HK = MarketSession("hk", ZoneInfo("Asia/Hong_Kong"), dt.time(16, 10))
_US = MarketSession("us", ZoneInfo("America/New_York"), dt.time(16, 0))
_JP = MarketSession("jp", ZoneInfo("Asia/Tokyo"), dt.time(15, 30))

_SUFFIX_SESSIONS = {"SS": _CN, "SZ": _CN, "SH": _CN, "HK": _HK, "T": _JP}
_INDEX_SESSIONS = {"^HSI": _HK, "^HSCE": _HK, "^N225": _JP}


def is_off_market_fund(symbol: str) -> bool:
    """六位纯数字 = 场外基金净值序列."""
    return len(symbol) == 6 and symbol.isdigit()


def session_for_symbol(symbol: str) -> MarketSession:
    """按代码推断交易所时区与收盘时间；无法识别的按美股处理."""
    if is_off_market_fund(symbol):
        return _CN
    if symbol in _INDEX_SESSIONS:
        return _INDEX_SESSIONS[symbol]
    if "." in symbol:
        return _SUFFIX_SESSIONS.get(symbol.rsplit(".", 1)[1], _US)
    return _US


def utcnow() -> dt.datetime:
    """当前时刻，naive UTC（与库内元数据时间戳同一约定）."""
    return dt.datetime.now(UTC).replace(tzinfo=None)


def business_today(now: dt.datetime | None = None) -> dt.date:
    """业务日（Asia/Shanghai）。``now`` 为 naive UTC，缺省取当前时刻."""
    current = now if now is not None else utcnow()
    return current.replace(tzinfo=UTC).astimezone(BUSINESS_TZ).date()


def local_naive_to_utc(value: dt.datetime) -> dt.datetime:
    """把 trading_data.update_time 这类「业务时区 naive」时间转成 naive UTC."""
    return value.replace(tzinfo=BUSINESS_TZ).astimezone(UTC).replace(tzinfo=None)


def business_naive_now() -> dt.datetime:
    """业务时区的 naive 当前时间（写 trading_data.create/update_time 用，与历史 datetime.now() 同值域）."""
    return dt.datetime.now(BUSINESS_TZ).replace(tzinfo=None)


def session_close_utc(symbol: str, day: dt.date) -> dt.datetime:
    """``day`` 这个交易日的收盘时刻（naive UTC）。"""
    session = session_for_symbol(symbol)
    close_local = dt.datetime.combine(day, session.close, tzinfo=session.tz)
    return close_local.astimezone(UTC).replace(tzinfo=None)


def is_final_snapshot(symbol: str, day: dt.date, fetched_at: dt.datetime) -> bool:
    """在 ``fetched_at`` 抓到的 ``day`` 这一行是否已是收盘后的最终值."""
    if is_off_market_fund(symbol):
        # 官方单位净值公布即定稿
        return True
    return fetched_at >= session_close_utc(symbol, day) + SETTLE_BUFFER


def final_ready_at(symbol: str, day: dt.date) -> dt.datetime:
    """该交易日的行最早可以被认定为定稿的时刻（naive UTC）."""
    return session_close_utc(symbol, day) + SETTLE_BUFFER


def as_of_for(symbol: str, day: dt.date, fetched_at: dt.datetime, is_final: bool) -> dt.datetime:
    """数据实际所属时点：定稿行 = 当日收盘时刻；非终值行 = 抓取时刻."""
    return session_close_utc(symbol, day) if is_final else fetched_at


def to_iso(value: dt.datetime | None) -> str | None:
    """naive UTC → Asia/Shanghai 带偏移 ISO8601."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC).astimezone(BUSINESS_TZ).isoformat(timespec="seconds")
