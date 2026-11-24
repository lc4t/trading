"""日报的统一视图模型：每个标的/策略算成一张「卡片」，按信号类型分区。

iGotify（Markdown，手机精简）与邮件（HTML，可更全）都从同一份视图渲染，内容同源：
    分区固定顺序：买入 → 卖出 → 换仓 → 持有 → 空仓；图标语义固定：🟢买入 🔴卖出 🔄换仓 🔵持有 ⚪空仓 ⚠️警示。
    每张卡片：名称（代码）、状态/信号日期、收益、价格、止损、「为什么」整句理由；字段缺失就整行省略，不写占位。
术语（避免歧义）：
    - 「状态：空仓 → 信号：买入」= 现在空仓、今天出现买入信号（该买入，不是已买入）
    - 「收益」一律指入场以来收益；「离场收益」指上一段持仓的收益
    - 「信号日期」= 信号对应的行情日（买入/卖出）或入场日（持有）
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Iterable

from .digest import (
    ReportRecord,
    base_strategy_name,
    format_price,
    format_trade_date,
)
from .digest_freshness import card_note, classify, freshness_warnings, reference_label
from .digest_markdown import DASH, Collected, Entry, NameBook, _metric_value, _pct, collect_entries

ZONES = ("买入", "卖出", "换仓", "持有", "空仓")
ZONE_EMOJI = {"买入": "🟢", "卖出": "🔴", "换仓": "🔄", "持有": "🔵", "空仓": "⚪"}
ZONE_TITLE = {
    "买入": "买入信号",
    "卖出": "卖出信号",
    "换仓": "换仓信号",
    "持有": "持有状态",
    "空仓": "空仓状态",
}
EMPTY_TEXT = {
    "买入": "当前无新增买入信号",
    "卖出": "当前无新增卖出信号",
    "换仓": "当前无换仓信号",
    "持有": "当前没有持有的标的",
    "空仓": "当前没有空仓的标的",
}
ACTION_ZONE = {"买入": "买入", "卖出": "卖出", "换仓": "换仓", "持有": "持有", "观望": "空仓"}
MAX_FLAG_LINES = 3
RECORD_EPS = 0.005  # 当前回撤与历史最深回撤相差不到这么多，就算「已达历史最深」


@dataclass(frozen=True)
class Card:
    zone: str
    kind: str  # "symbol" | "rotation"
    title: str  # 名称（代码）；轮动策略为策略名
    rows: tuple[tuple[str, ...], ...]  # 每行一组短字段
    why: str | None = None
    ranking: tuple[tuple[int, str, str, bool], ...] = ()  # (名次, 名称, 动量, 是否目标)
    flags: tuple[str, ...] = ()
    extra_rows: tuple[tuple[str, ...], ...] = ()  # 邮件额外细节：交易记录、绩效、参数
    action_word: str = ""  # 结论里的动词：买入 / 卖出 / 换入 X / 需清仓


@dataclass(frozen=True)
class View:
    label: str  # "trading 日报 2026-11-22"
    headline: str  # "买入 1"、"无信号变化"
    report_date: str
    latest: str | None
    generated_at: str | None
    actions: tuple[tuple[str, str], ...]  # (emoji, 一行文字)，结论里列出
    warnings: tuple[str, ...]  # ⚠️ 行（不含图标）
    metrics_lines: tuple[tuple[str, ...], ...]  # 策略级关键指标，每行一组字段
    zones: dict[str, list[Card]]
    email_hint: bool
    warnings_full: tuple[str, ...] = field(default=())  # 邮件版警示：名字列全
    stale_labels: tuple[str, ...] = field(default=())


# ---------- 小工具 ----------


def _split_exit_reason(reason: str, book: NameBook) -> str:
    """「吊灯止损 [ATR=…, 止损价=432.222]」→「吊灯止损（止损价 432.222）」。"""
    text = reason.strip()
    head = text.split(" [")[0].strip()
    stop = re.search(r"止损价=([\d.]+)", text)
    if stop:
        return f"{head}（止损价 {stop.group(1)}）"
    return book.annotate(head or text)


def _last_trade(raw: dict, action: str) -> dict | None:
    trades = [
        t
        for t in raw.get("recentTrades") or []
        if isinstance(t, dict) and str(t.get("action")).upper() == action
    ]
    return trades[-1] if trades else None


def _drawdown(entry: Entry) -> tuple[float, float] | None:
    raw = entry.raw
    if entry.kind == "rotation":
        summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
        maximum = (
            summary.get("maxDrawdown")
            if summary.get("maxDrawdown") is not None
            else _metric_value(raw, "最大回撤")
        )
    else:
        maximum = _metric_value(raw, "最大回撤")
    current = _metric_value(raw, "当前回撤")
    if isinstance(current, (int, float)) and isinstance(maximum, (int, float)):
        return float(current), float(maximum)
    return None


def _at_record(entry: Entry) -> bool:
    dd = _drawdown(entry)
    return bool(dd and dd[0] > 0 and dd[0] >= dd[1] - RECORD_EPS)


def _net_value(entry: Entry) -> float | None:
    raw = entry.raw
    if entry.kind == "rotation":
        summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
        if summary.get("finalValue") is not None and summary.get("initialCapital"):
            return float(summary["finalValue"]) / float(summary["initialCapital"])
        return None
    value = _metric_value(raw, "最新净值")
    return float(value) if isinstance(value, (int, float)) else None


def _entry_title(entry: Entry, book: NameBook) -> str:
    if entry.kind == "symbol":
        return book.label(entry.item.key)
    return base_strategy_name(str(entry.raw.get("strategyName") or entry.item.key))


def _num(value: object, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DASH


def _clean_rows(rows: Iterable[tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    """字段缺失就整个字段省略（不写占位）；整行都没内容则整行省略。"""
    cleaned = []
    for row in rows:
        fields = tuple(
            f
            for f in row
            if f and not f.rstrip().endswith(DASH) and not re.search(r"\bNone\b|\bnan\b", f, re.I)
        )
        if fields:
            cleaned.append(fields)
    return tuple(cleaned)


# ---------- 单标的卡片 ----------


def _symbol_card(entry: Entry, book: NameBook) -> Card:
    raw, action = entry.raw, entry.item.action
    zone = ACTION_ZONE[action]
    latest = raw.get("latestSignal") or {}
    prices = latest.get("prices") or {}
    position = raw.get("positionInfo") or {}
    stop = latest.get("stopLoss") if isinstance(latest.get("stopLoss"), dict) else None
    conditions = [str(c) for c in latest.get("conditions") or [] if c]
    close = prices.get("close")
    sell, buy = _last_trade(raw, "SELL"), _last_trade(raw, "BUY")
    rows: list[tuple[str, ...]] = []
    why: str | None = None
    stop_row = (
        (
            f"止损 {format_price(stop.get('price'))}（距现价 {_pct(stop.get('distancePct'), False)}）",
        )
        if stop and stop.get("price") is not None
        else None
    )
    exit_row = (
        (
            f"最近离场：{format_trade_date(sell.get('date'))}",
            *(
                [f"离场收益：{_pct(sell['profitLossPercentage'])}"]
                if sell.get("profitLossPercentage") is not None
                else []
            ),
        )
        if sell
        else None
    )
    if zone == "买入":
        rows.append(("状态：空仓 → 信号：买入",))
        rows.append(
            (
                f"信号日期：{entry.data_date or DASH}",
                *([f"现价 {format_price(close)}"] if close is not None else []),
            )
        )
        if exit_row:
            rows.append(exit_row)
        why = (
            f"满足买入条件 {book.annotate('；'.join(conditions))}" if conditions else "出现买入信号"
        )
    elif zone == "卖出":
        rows.append(("状态：持有 → 信号：卖出",))
        rows.append(
            (
                f"信号日期：{entry.data_date or DASH}",
                *([f"收益：{_pct(position.get('unrealized_pnl_pct'))}"] if position else []),
            )
        )
        prices_row = [
            *(
                [f"现价 {format_price(position.get('current_price') or close)}"]
                if (position.get("current_price") or close) is not None
                else []
            ),
            *(
                [f"入场价 {format_price(position.get('entry_price'))}"]
                if position.get("entry_price") is not None
                else []
            ),
        ]
        if prices_row:
            rows.append(tuple(prices_row))
        if stop_row:
            rows.append(stop_row)
        sell_reason = (sell or {}).get("reason")
        why = (
            f"触发卖出条件：{_split_exit_reason(str(sell_reason), book)}"
            if sell_reason and sell and format_trade_date(sell.get("date")) == entry.data_date
            else (
                f"触发卖出条件：{book.annotate('；'.join(conditions))}"
                if conditions
                else "触发卖出条件"
            )
        )
    elif zone == "持有":
        entry_date = position.get("entry_date") or (buy or {}).get("date")
        rows.append(
            (
                *([f"信号日期：{format_trade_date(entry_date)}（入场）"] if entry_date else []),
                *(
                    [f"收益：{_pct(position.get('unrealized_pnl_pct'))}"]
                    if position.get("unrealized_pnl_pct") is not None
                    else []
                ),
            )
        )
        prices_row = [
            *(
                [f"现价 {format_price(position.get('current_price') or close)}"]
                if (position.get("current_price") or close) is not None
                else []
            ),
            *(
                [f"入场价 {format_price(position.get('entry_price'))}"]
                if position.get("entry_price") is not None
                else []
            ),
        ]
        if prices_row:
            rows.append(tuple(prices_row))
        if stop_row:
            rows.append(stop_row)
        why = "未触发卖出条件，继续持有"
    else:  # 空仓
        if exit_row:
            rows.append(exit_row)
        reason = (sell or {}).get("reason")
        why = (
            f"上次因{_split_exit_reason(str(reason), book)}离场，当前未出现买入信号"
            if reason
            else "当前未出现买入信号"
        )
    flags: list[str] = []
    dd = _drawdown(entry)
    if _at_record(entry) and dd:
        flags.append(f"回撤已达历史最深｜当前 -{dd[0]:.2f}%｜最深 -{dd[1]:.2f}%")
    extra = _email_extra_rows(entry, book)
    action_word = {"买入": "买入", "卖出": "卖出"}.get(zone, "")
    return Card(
        zone,
        "symbol",
        _entry_title(entry, book),
        tuple(r for r in rows if r),
        why,
        (),
        tuple(flags),
        extra,
        action_word,
    )


# ---------- 轮动卡片 ----------


def _momentum_text(value: object, fn: str) -> str:
    if value is None:
        return "无数据"
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "无数据"
    return f"{number * 100:+.2f}%" if fn == "simple_return" else f"{number:+.4f}"


def _rotation_card(entry: Entry, book: NameBook) -> Card:
    raw, action = entry.raw, entry.item.action
    zone = ACTION_ZONE[action]
    holding = raw.get("currentHolding") or {}
    signal = raw.get("nextSignal") or {}
    ranking = [r for r in raw.get("universeRanking") or [] if isinstance(r, dict)]
    fn = str((raw.get("config") or {}).get("momentumFn") or "")
    target, source = signal.get("rotateTo"), signal.get("rotateFrom") or holding.get("symbol")
    rows: list[tuple[str, ...]] = [
        (
            (
                f"当前持有：{book.label(holding.get('symbol'))}"
                if holding.get("symbol")
                else "当前持有：空仓"
            ),
        )
    ]
    if zone == "换仓":
        word = f"需换仓 → 换入 {book.label(target)}" if target else "需换仓"
        action_word = f"换入 {book.short(str(target))}" if target else "换仓"
    elif zone == "买入":
        word = f"需建仓 → 买入 {book.label(target)}" if target else "需建仓"
        action_word = f"买入 {book.short(str(target))}" if target else "买入"
    elif zone == "卖出":
        word, action_word = "需清仓（全部转现金）", "清仓"
    elif zone == "持有":
        word, action_word = "继续持有", ""
    else:
        word, action_word = "继续空仓", ""
    rows.append((f"本期结论：{word}",))
    if holding:
        rows.append(
            (
                f"入场：{format_trade_date(holding.get('since'))}",
                (
                    f"收益：{_pct(holding.get('unrealizedPnlPct'))}（入场以来）"
                    if holding.get("unrealizedPnlPct") is not None
                    else DASH
                ),
            )
        )
        rows.append(
            tuple(
                [
                    *(
                        [f"现价 {format_price(holding.get('currentPrice'))}"]
                        if holding.get("currentPrice") is not None
                        else []
                    ),
                    *(
                        [f"入场价 {format_price(holding.get('entryPrice'))}"]
                        if holding.get("entryPrice") is not None
                        else []
                    ),
                ]
            )
        )
    ranks = tuple(
        (
            int(r.get("rank") or i + 1),
            book.short(str(r.get("symbol"))),
            _momentum_text(r.get("momentum"), fn),
            bool(r.get("selected")),
        )
        for i, r in enumerate(ranking)
    )
    why = _rotation_why(zone, ranking, target, source, book, fn)
    flags: list[str] = []
    dd = _drawdown(entry)
    if _at_record(entry) and dd:
        flags.append(f"回撤已达历史最深｜当前 -{dd[0]:.2f}%｜最深 -{dd[1]:.2f}%")
    return Card(
        zone,
        "rotation",
        _entry_title(entry, book),
        tuple(r for r in rows if r),
        why,
        ranks,
        tuple(flags),
        _email_extra_rows(entry, book),
        action_word,
    )


def _rotation_why(
    zone: str, ranking: list[dict], target: object, source: object, book: NameBook, fn: str
) -> str:
    by_symbol = {str(r.get("symbol")): r for r in ranking}
    total = len(ranking)

    def rank_of(code: object) -> tuple[int | None, str]:
        r = by_symbol.get(str(code))
        return (
            int(r["rank"]) if r and r.get("rank") is not None else None,
            _momentum_text(r.get("momentum"), fn) if r else DASH,
        )

    if zone in ("换仓", "买入") and target:
        t_rank, t_mom = rank_of(target)
        text = (
            f"本期再平衡：{book.label(target)} 排名第 {t_rank or 1}（{total} 个候选中动量 {t_mom}）"
        )
        if source and str(source) != str(target):
            s_rank, _ = rank_of(source)
            text += f"，{book.label(source)} 排名第 {s_rank}" if s_rank else ""
        return text + "，因此换入"
    if zone == "卖出":
        return "本期再平衡：所有候选动量都低于阈值，因此转为现金"
    if zone == "持有" and target:
        t_rank, t_mom = rank_of(target)
        return f"本期再平衡：{book.label(target)} 仍排名第 {t_rank or 1}（{total} 个候选中动量 {t_mom}），因此继续持有"
    return "本期再平衡：没有候选资产满足持仓条件，因此继续空仓"


def _email_extra_rows(entry: Entry, book: NameBook) -> tuple[tuple[str, ...], ...]:
    """邮件才放的细节：最近交易/换仓、绩效、年度收益、参数。"""
    raw = entry.raw
    rows: list[tuple[str, ...]] = []
    if entry.kind == "symbol":
        for label, trade in zip(
            ("最近交易", "上一笔"),
            reversed([t for t in raw.get("recentTrades") or [] if isinstance(t, dict)][-2:]),
        ):
            action = {"BUY": "买入", "SELL": "卖出"}.get(
                str(trade.get("action")).upper(), str(trade.get("action"))
            )
            parts = [
                f"{label}：{format_trade_date(trade.get('date'))} {action} @{format_price(trade.get('price'))}"
            ]
            if trade.get("profitLossPercentage") is not None:
                parts.append(_pct(trade["profitLossPercentage"]))
            if trade.get("reason"):
                parts.append(book.annotate(str(trade["reason"])))
            rows.append(tuple(parts))
    else:
        for label, rot in zip(
            ("最近换仓", "上一次"),
            reversed([r for r in raw.get("rotations") or [] if isinstance(r, dict)][-2:]),
        ):
            rows.append(
                (
                    f"{label}：{book.label(rot.get('symbol'))}",
                    f"{format_trade_date(rot.get('entryDate'))} → {format_trade_date(rot.get('exitDate'))}",
                    f"{rot.get('holdingDays', DASH)} 天",
                    _pct(rot.get("periodReturnPct")),
                )
            )
    summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}

    def pick(summary_key: str, metric: str) -> object:
        value = summary.get(summary_key)
        return value if value is not None else _metric_value(raw, metric)

    perf = [
        f"年化 {_pct(pick('annualReturn', '年化收益率'))}",
        (
            f"最大回撤 -{_num(pick('maxDrawdown', '最大回撤'))}%"
            if pick("maxDrawdown", "最大回撤") is not None
            else None
        ),
        (
            f"当前回撤 -{_num(_metric_value(raw, '当前回撤'))}%"
            if _metric_value(raw, "当前回撤") is not None
            else None
        ),
        (
            f"夏普 {_num(pick('sharpe', '夏普比率'))}"
            if pick("sharpe", "夏普比率") is not None
            else None
        ),
        (
            f"胜率 {_pct(_metric_value(raw, '胜率'), False)}"
            if _metric_value(raw, "胜率") is not None
            else None
        ),
    ]
    rows.append(("回测：" + "｜".join(p for p in perf if p),) if any(perf) else ())
    years = [y for y in raw.get("annualReturns") or [] if isinstance(y, dict) and "year" in y]
    if years:
        rows.append(
            ("年度收益：" + "｜".join(f"{y['year']} {_pct(y.get('value'))}" for y in years),)
        )
    config = raw.get("config")
    if isinstance(config, dict) and config:
        rows.append(
            (
                "参数："
                + "｜".join(f"{k}={book.annotate(v)}" for k, v in config.items() if v is not None),
            )
        )
    return tuple(r for r in rows if r)


# ---------- 视图 ----------


def _action_line(card: Card) -> tuple[str, str]:
    emoji = ZONE_EMOJI[card.zone]
    if card.kind == "symbol":
        return emoji, f"{card.action_word} {card.title}"
    return emoji, f"{card.action_word}（{card.title}）" if card.action_word else card.title


def build_view(
    reports: list[ReportRecord],
    target_date: date,
    rotation_reports: list[dict] | None = None,
    warnings: Iterable[str] = (),
    generated_at: str | None = None,
    email_hint: bool = False,
    now: dt.datetime | None = None,
) -> View:
    """now 为 naive UTC（缺省取当前时刻），用来推算各市场「应有的最后完成交易日」。"""
    collected: Collected = collect_entries(reports, target_date, rotation_reports)
    book = collected.book
    fresh = classify(collected.entries, now)
    zones: dict[str, list[Card]] = {zone: [] for zone in ZONES}
    cards: list[tuple[Entry, Card]] = []
    for entry in collected.entries:
        card = (
            _rotation_card(entry, book) if entry.kind == "rotation" else _symbol_card(entry, book)
        )
        note = card_note(fresh[id(entry)])
        if note:  # 数据截止日与参照日不符：在卡片里注明实际截止日
            card = replace(card, rows=(*card.rows, (note,)))
        zones[card.zone].append(card)
        cards.append((entry, card))
    actions = tuple(_action_line(card) for zone in ("买入", "卖出", "换仓") for card in zones[zone])
    latest = reference_label(fresh)
    flags: list[str] = []
    label_of = lambda e: _entry_title(e, book)  # noqa: E731
    stale_lines = freshness_warnings(collected.entries, fresh, label_of)
    flags += stale_lines
    flagged = [e for e in collected.entries if fresh[id(e)].status != "ok"]
    records = [c for _, c in cards if c.flags]
    if records:
        shown = [f"{c.title}" for c in records[: MAX_FLAG_LINES - 1]]
        more = f" 等{len(records)}项" if len(records) > len(shown) else ""
        flags.append("回撤已达历史最深：" + "、".join(shown) + more)
    warning_list = [book.annotate(w) for w in warnings if w]
    if warning_list:
        flags.append(f"有 {len(warning_list)} 条数据提示")
    metrics_lines: list[tuple[str, ...]] = []
    for entry in collected.entries:
        if entry.kind != "rotation":
            continue
        dd, nav = _drawdown(entry), _net_value(entry)
        fields = [_entry_title(entry, book)]
        if dd:
            fields += [f"当前回撤 -{dd[0]:.2f}%", f"历史最深 -{dd[1]:.2f}%"]
        if nav is not None:
            fields.append(f"净值 {nav:.3f}")
        if len(fields) > 1:
            metrics_lines.append(tuple(fields))
    headline_parts = [
        f"{zone} {len(zones[zone])}" for zone in ("买入", "换仓", "卖出") if zones[zone]
    ]
    headline = (
        "｜".join(headline_parts)
        if headline_parts
        else ("无数据" if not collected.entries else "无信号变化")
    )
    return View(
        label=f"trading 日报 {target_date.isoformat()}",
        headline=headline,
        report_date=target_date.isoformat(),
        latest=latest,
        generated_at=generated_at,
        actions=actions,
        warnings=tuple(flags),
        warnings_full=tuple(
            [
                *freshness_warnings(collected.entries, fresh, label_of, cap=None),
                *flags[len(stale_lines) :],
            ]
        ),
        metrics_lines=tuple(metrics_lines),
        zones=zones,
        email_hint=email_hint,
        stale_labels=tuple(_entry_title(e, book) for e in flagged),
    )
