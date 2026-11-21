"""手机推送（iGotify）版日报：只回答三件事——今天要做什么、每个标的现在什么状态、哪里需要盯。

回测概览、绩效、年度收益、策略参数、交易/换仓明细、动量全排名一律不进推送（只在邮件完整版里）。

固定三条消息（常规日 2~3 条）：
    1. 今日结论   —— 第一行直接给结论；只列有动作的标的，每个一行；整条尽量 ≤600 字节，保证横幅能弹出
    2. 状态总览   —— 每个标的一组：「emoji **名称（代码）** 动作」+ 1~2 行短字段（持仓｜浮盈 / 止损｜切换）
    3. 需关注     —— 只为有动作或数据异常的标的写，每个最多 3 行（依据 / 触发 / 止损；轮动只写目标）；没有则整条省略
边界声明压成 1 行放在最后一条末尾。

版式约束：只用一级小标题（##）+ 列表，不用表格；动作词与标的名用粗体、其余不用；每行 ≤22 个全角字符；
凡出现标的/基金代码一律「名称（代码）」。
"""

from __future__ import annotations

from datetime import date
from typing import Iterable

from .channels.chunking import Block, DigestParts, Unit
from .channels.markup import md_escape
from .digest import (
    ACTION_DISPLAY_LABEL,
    ACTION_EMOJI,
    TRADE_ACTIONS,
    ReportRecord,
    base_strategy_name,
    format_price,
    format_trade_date,
)
from .digest_markdown import (
    DASH,
    LINE_WIDTH,
    Collected,
    Entry,
    NameBook,
    _basis,
    _headline,
    _pct,
    _rotation_snapshot,
    _symbol_snapshot,
    collect_entries,
    display_width,
    wrap_fields,
)

PUSH_BOUNDARY = "仅为策略信号提示，非投资建议，不自动下单。"
MAX_ACTION_LINES = 5  # 结论里最多列几个有动作的标的；超出的折叠成一行
BASIS_MAX_CHARS = 30  # 「依据」一句话的字数上限


def _e(value: object) -> str:
    return md_escape(value)


def shorten(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def shorten_width(text: str, limit: int = LINE_WIDTH) -> str:
    """按显示宽度截断并加省略号（推送里宁可删字也不让一行折成两行）。"""
    if display_width(text) <= limit:
        return text
    out = ""
    for ch in text:
        if display_width(out + ch + "…") > limit:
            break
        out += ch
    return out + "…"


def _bullets(lines: Iterable[str]) -> list[str]:
    return [f"- {line}" for line in lines]


def _label(entry: Entry, book: NameBook) -> str:
    """推送里的标的名：单标的「名称（代码）」；轮动策略只写策略名（策略 id 不是基金代码，省宽度）。"""
    if entry.kind == "symbol":
        return book.label(entry.item.key)
    return base_strategy_name(str(entry.raw.get("strategyName") or entry.item.key))


def _verb(entry: Entry, book: NameBook) -> tuple[str, str | None]:
    """动作词与（轮动的）目标名；目标只写名称，代码放在详情里。"""
    action = entry.item.action
    if entry.kind == "symbol" or action not in TRADE_ACTIONS:
        return ACTION_DISPLAY_LABEL[action], None
    signal = entry.raw.get("nextSignal") or {}
    holding = entry.raw.get("currentHolding") or {}
    target = signal.get("rotateTo")
    source = signal.get("rotateFrom") or holding.get("symbol")
    if action == "换仓":
        return ("换入", book.short(str(target))) if target else ("换仓", None)
    if action == "买入":
        return ("买入", book.short(str(target))) if target else ("买入", None)
    return ("卖出", book.short(str(source))) if source else ("清仓", None)


def _head(entry: Entry, book: NameBook, emoji: str | None = None) -> str:
    verb, target = _verb(entry, book)
    is_action = entry.item.action in TRADE_ACTIONS
    verb_md = f"**{verb}**" if is_action else verb  # 动作词只在有动作时加粗，突出重点
    tail = f" {_e(target)}" if target else ""
    return (
        f"{emoji or ACTION_EMOJI[entry.item.action]} **{_e(_label(entry, book))}** {verb_md}{tail}"
    )


# ---------- 第 1 条：今日结论 ----------


def _flagged(collected: Collected) -> tuple[list[Entry], list[Entry]]:
    latest = collected.latest
    stale = [e for e in collected.entries if latest and e.data_date and e.data_date < latest]
    missing = [e for e in collected.entries if not e.data_date]
    return stale, missing


def _conclusion_block(collected: Collected, warning_count: int) -> Block:
    entries, book = collected.entries, collected.book
    actions = [e for e in entries if e.item.action in TRADE_ACTIONS]
    stale, missing = _flagged(collected)
    if not entries:
        return Block(("⚠️ **今日未取得数据**",), body_start=1)
    bullets: list[str] = []
    if actions:
        headline = f"🔄 **今日需操作 {len(actions)} 项**"
        shown = actions if len(actions) <= MAX_ACTION_LINES else actions[: MAX_ACTION_LINES - 1]
        bullets += _bullets(_head(e, book) for e in shown)
        if len(shown) < len(actions):
            bullets.append(f"- …另有 {len(actions) - len(shown)} 项，见「状态总览」")
    else:
        headline = "✅ **今日无信号变化**"
        hold = sum(1 for e in entries if e.item.action == "持有")
        bullets.append(
            f"- {ACTION_EMOJI['持有']} 持有 {hold}｜{ACTION_EMOJI['观望']} 空仓 {len(entries) - hold}"
        )
    flagged = [*stale, *missing]
    if flagged:
        more = f" 等{len(flagged)}项" if len(flagged) > 1 else ""
        bullets.append(f"- ⚠️ 数据落后：{_e(_label(flagged[0], book))}{more}")
    if warning_count:
        bullets.append(f"- ⚠️ 有 {warning_count} 条数据提示")
    return Block((headline, *bullets), body_start=1)


# ---------- 第 2 条：状态总览 ----------


def _status_group(entry: Entry, book: NameBook) -> list[str]:
    raw = entry.raw
    head = _head(entry, book)
    if entry.kind == "rotation":
        snap = _rotation_snapshot(raw, book)
        first = wrap_fields([f"持仓 {snap['position']}", f"浮盈 {snap['pnl']}"])
        second = wrap_fields([f"切换 {snap['switch']}"]) if snap["switch"] != DASH else []
        return [head, *_bullets(_e(line) for line in [*first, *second])]
    snap = _symbol_snapshot(raw)
    stop = (raw.get("latestSignal") or {}).get("stopLoss")
    has_position = snap["position"] != "空仓"
    first = wrap_fields(
        [f"持仓 {snap['position']}", *([f"浮盈 {snap['pnl']}"] if has_position else [])]
    )
    second_fields: list[str] = []
    if isinstance(stop, dict) and stop.get("price") is not None:
        second_fields.append(
            f"止损 {format_price(stop.get('price'))}（距现价 {_pct(stop.get('distancePct'), False)}）"
        )
    if snap["switch"] != DASH:
        second_fields.append(f"切换 {snap['switch'].split(' ')[0]}")  # 只留日期，动作词已在标题行
    return [head, *_bullets(_e(line) for line in [*first, *wrap_fields(second_fields)])]


def _status_unit(collected: Collected) -> Unit:
    lines: list[str] = ["## 状态总览", ""]
    for index, entry in enumerate(collected.entries):
        if index:
            lines.append("")
        lines += _status_group(entry, collected.book)
    return Unit("状态总览", (Block(tuple(lines), body_start=2, grouped=True),), break_before=True)


# ---------- 第 3 条：需关注 ----------


def _detail_group(entry: Entry, book: NameBook, flagged: bool) -> list[str]:
    raw = entry.raw
    action = entry.item.action
    is_action = action in TRADE_ACTIONS
    head = _head(entry, book) if is_action else f"⚠️ **{_e(_label(entry, book))}**"
    lines: list[str] = []
    if entry.kind == "symbol" and is_action:
        latest = raw.get("latestSignal") or {}
        stop = latest.get("stopLoss")
        close = (latest.get("prices") or {}).get("close")
        lines.append(f"依据：{shorten(_basis(entry, book), BASIS_MAX_CHARS)}")
        trigger = (
            f"{format_trade_date(entry.data_date)} 收盘价 {format_price(close)}"
            if close is not None
            else format_trade_date(entry.data_date)
        )
        lines.append(f"触发：{trigger}")
        if isinstance(stop, dict) and stop.get("price") is not None:
            lines.append(
                f"止损：{format_price(stop.get('price'))}（距现价 {_pct(stop.get('distancePct'), False)}）"
            )
    elif entry.kind == "rotation" and is_action:
        target = (raw.get("nextSignal") or {}).get("rotateTo")
        need = {"换仓": "需换仓", "买入": "需建仓", "卖出": "需清仓"}[action]
        goal = f"目标：{book.label(target) if target else '空仓'}"
        # 一行放得下就写成「目标：名称（代码）；需换仓」，放不下拆成两行（不靠自然折行）
        lines += (
            [f"{goal}；{need}"] if display_width(f"{goal}；{need}") <= LINE_WIDTH else [goal, need]
        )
    if flagged:
        lines.append(f"数据：截止 {entry.data_date}，落后" if entry.data_date else "数据：日期缺失")
    return [head, *_bullets(_e(line) for line in lines)]


def _detail_unit(collected: Collected, warnings: list[str]) -> Unit | None:
    book = collected.book
    stale, missing = _flagged(collected)
    flagged_ids = {id(e) for e in [*stale, *missing]}
    groups = [
        _detail_group(e, book, id(e) in flagged_ids)
        for e in collected.entries
        if e.item.action in TRADE_ACTIONS or id(e) in flagged_ids
    ]
    if warnings:
        groups.append(["⚠️ **数据提示**", *_bullets(_e(shorten_width(w)) for w in warnings)])
    if not groups:
        return None
    lines: list[str] = ["## 需关注", ""]
    for index, group in enumerate(groups):
        if index:
            lines.append("")
        lines += group
    return Unit("需关注", (Block(tuple(lines), body_start=2, grouped=True),), break_before=True)


def build_push_parts(
    reports: list[ReportRecord],
    target_date: date,
    rotation_reports: list[dict] | None = None,
    warnings: Iterable[str] = (),
) -> DigestParts:
    """手机推送版的结构化日报：结论 / 状态总览 / 需关注（可省略）各占一条，边界声明 1 行在末尾。"""
    collected = collect_entries(reports, target_date, rotation_reports)
    warning_list = [collected.book.annotate(w) for w in warnings if w]
    units: list[Unit] = []
    if collected.entries:
        units.append(_status_unit(collected))
    detail = _detail_unit(collected, warning_list)
    if detail:
        units.append(detail)
    units.append(Unit("边界声明", (Block((PUSH_BOUNDARY,)),)))
    return DigestParts(
        label=f"trading 日报 {target_date.isoformat()}",
        headline=_headline(collected.entries),
        summary=(_conclusion_block(collected, len(warning_list)),),
        units=tuple(units),
    )
