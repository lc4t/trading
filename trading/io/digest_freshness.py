"""日报的数据新鲜度判断：以「应有的最后一个已完成交易日」为参照日，而不是各标的里最大的日期。

旧做法拿「所有 JSON 里最大的数据截止日」当参照：只要有一个标的多带了一根（比如盘中那一根），
其余全被判成「落后」，对读者是误导。现在：
- 参照日 = 标的所在市场的「应有的最后完成交易日」，沿用 ``trading.market.clock.completed_session_end``
  （收盘 + 30 分钟缓冲：A 股 15:30、港股 16:40；周末取周五；没有节假日日历）；
- 比参照日更早 → 「数据落后」（给出天数与标的）；比参照日更晚（含未收盘盘中数据）→ 单独的「含未收盘数据」，
  不会把别的标的拖成落后；
- 轮动策略按候选池里最保守（最早）的参照日。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from ..market.clock import completed_session_end, session_for_symbol, utcnow

MARKET_NAMES = {"cn": "A股", "hk": "港股", "us": "美股", "jp": "日股"}
MARKET_ORDER = ("cn", "hk", "us", "jp")
DEFAULT_SYMBOL = "000000.SS"  # 轮动策略取不到候选池时按 A 股处理
LIST_CAP = 3  # 手机版最多列几个名字，其余写「等」


@dataclass(frozen=True)
class Freshness:
    status: str  # "ok" | "stale" | "ahead" | "missing"
    market: str
    ref: dt.date | None  # 应有的最后完成交易日
    data: dt.date | None  # 实际数据截止日
    lag_days: int = 0  # stale：早几天；ahead：晚几天


def md(day: dt.date) -> str:
    return day.strftime("%m-%d")


def _parse(value: object) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def entry_symbols(entry) -> list[str]:  # noqa: ANN001 - Entry 定义在 digest_markdown，避免循环导入
    if entry.kind == "symbol":
        return [str(entry.item.key)]
    raw = entry.raw
    universe = (raw.get("config") or {}).get("universe") or []
    symbols = [str(s) for s in universe if s]
    holding = (raw.get("currentHolding") or {}).get("symbol")
    if not symbols and holding:
        symbols = [str(holding)]
    return symbols or [DEFAULT_SYMBOL]


def reference_for(symbols: Sequence[str], now: dt.datetime) -> tuple[dt.date, str]:
    """(应有的最后完成交易日, 市场)；多个标的取最早的参照日（最保守）。"""
    pairs = [
        (completed_session_end(s, dt.date.max, now), session_for_symbol(s).name) for s in symbols
    ]
    ref, market = min(pairs, key=lambda p: p[0])
    return ref, market


def classify(entries: Iterable, now: dt.datetime | None = None) -> dict[int, Freshness]:
    """按条目（id）给出新鲜度；now 为 naive UTC，缺省取当前时刻。"""
    now = now if now is not None else utcnow()
    result: dict[int, Freshness] = {}
    for entry in entries:
        ref, market = reference_for(entry_symbols(entry), now)
        data = _parse(entry.data_date)
        if data is None:
            result[id(entry)] = Freshness("missing", market, ref, None)
        elif data < ref:
            result[id(entry)] = Freshness("stale", market, ref, data, (ref - data).days)
        elif data > ref:
            result[id(entry)] = Freshness("ahead", market, ref, data, (data - ref).days)
        else:
            result[id(entry)] = Freshness("ok", market, ref, data)
    return result


def reference_label(fresh: dict[int, Freshness]) -> str | None:
    """顶部「数据截至」写的内容：参照日；各市场参照日不同就分开写。"""
    refs = {(f.market, f.ref) for f in fresh.values() if f.ref}
    if not refs:
        return None
    if len({ref for _, ref in refs}) == 1:
        return next(iter(refs))[1].isoformat()
    ordered = sorted(
        refs, key=lambda p: (MARKET_ORDER.index(p[0]) if p[0] in MARKET_ORDER else 9, p[1])
    )
    return "｜".join(
        f"{ref.isoformat()}（{MARKET_NAMES.get(market, market)}）" for market, ref in ordered
    )


def card_note(f: Freshness) -> str | None:
    """卡片里的「数据截止」行：与参照日不符时才写。"""
    if f.status == "stale" and f.data and f.ref:
        return f"数据截止：{f.data.isoformat()}（比应有日期 {md(f.ref)} 早 {f.lag_days} 天）"
    if f.status == "ahead" and f.data and f.ref:
        return f"数据截止：{f.data.isoformat()}（含未收盘数据，应为 {md(f.ref)}）"
    if f.status == "missing":
        return "数据截止：日期缺失"
    return None


def freshness_warnings(
    entries: Sequence,
    fresh: dict[int, Freshness],
    label_of: Callable[[object], str],
    cap: int | None = LIST_CAP,
) -> list[str]:
    """⚠️ 文案（不含图标）。cap 为最多列几个名字（手机版 3，邮件版 None=列全）。"""

    def names(items: Sequence) -> str:
        shown = items if cap is None else items[:cap]
        return "、".join(label_of(e) for e in shown) + (
            "等" if cap is not None and len(items) > cap else ""
        )

    stale: dict[tuple[dt.date, int], list] = {}
    ahead: dict[tuple[dt.date, dt.date], list] = {}
    missing: list = []
    for entry in entries:
        f = fresh[id(entry)]
        if f.status == "stale" and f.ref:
            stale.setdefault((f.ref, f.lag_days), []).append(entry)
        elif f.status == "ahead" and f.ref and f.data:
            ahead.setdefault((f.data, f.ref), []).append(entry)
        elif f.status == "missing":
            missing.append(entry)
    lines = [
        f"数据落后：{len(group)} 项比应有日期（{md(ref)}）早 {lag} 天：{names(group)}"
        for (ref, lag), group in sorted(stale.items(), key=lambda kv: (-kv[0][1], kv[0][0]))
    ]
    lines += [
        f"含未收盘数据：{names(group)}（数据 {md(data)}，应为 {md(ref)}）"
        for (data, ref), group in sorted(ahead.items())
    ]
    if missing:
        lines.append(f"数据日期缺失：{names(missing)}")
    return lines
