"""完整版日报（Markdown）：给 iGotify 与邮件用，PushGo 的精简正文保持不变。

版式（先结论、再总览、再细节、最后绩效），全部是短行列表，**不用表格**（手机太窄，表格列会被挤得换行）：
    # 标题 + 数据截止行
    ## 今日结论      —— 今天要不要动作，逐个点名；数据缺失/落后也写在这里
    ## 状态总览      —— 每个标的/策略一组：第一行「emoji **名称（代码）** 动作」，其后 1~2 行短字段（｜分隔）
    ## 标的详情      —— 每个标的同样的字段顺序；轮动策略附动量排名
    ## 回测与绩效    —— 每个标的 2~3 行短字段；再列年度收益与策略参数
    ## 数据提示 / 边界声明

规则：
- 凡出现标的/基金代码，一律写成「名称（代码）」（名称取数据里的 name，缺失用已知映射，再缺失写「未知名称」并记 warning）；
  同一条消息里同一标的叫法一致。
- 每行尽量不超过手机一行（约 22 个全角字符），超出的字段拆到下一行，不靠自然折行。
- 只用 iGotify 实测能渲染的语法：二级标题、粗体、列表、分隔线（前后各留空行，否则 `---` 紧跟文字会被当成标题）。
- 来自数据的文本一律转义，防止注入。
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .channels.chunking import Block, DigestParts, Unit, render_full
from .channels.common import log
from .channels.markup import md_escape
from .digest import (
    ACTION_DISPLAY_LABEL,
    ACTION_EMOJI,
    ACTION_PRIORITY,
    KNOWN_SYMBOL_NAMES,
    TRADE_ACTIONS,
    DigestItem,
    ReportRecord,
    base_strategy_name,
    build_rotation_item,
    build_symbol_item,
    format_price,
    format_trade_date,
)
from .digest_freshness import classify, freshness_warnings, reference_label

RECENT_ROTATIONS_SHOWN = 2
DASH = "—"
LINE_WIDTH = 44  # 一行的显示宽度上限：全角字符算 2，约 22 个全角字符
FIELD_SEP = "｜"
UNKNOWN_NAME = "未知名称"

BOUNDARY_LINES = (
    "以上是策略对历史行情的机械计算结果（信号监测），仅供参考，不构成投资建议；系统只提示、不自动下单。",
    "止损位、动量排名等是策略的参考位与排序，不是委托价；回测与历史表现不代表未来收益。",
    "各标的数据截止日以「数据日期」为准；数据缺失或滞后时，该标的的结论可能已过期。",
)
TRADE_LABELS = {"BUY": "买入", "SELL": "卖出"}
CODE_RE = re.compile(r"(?<![\w.])(\d{6}\.(?:SZ|SS|SH)|\d{4,5}\.HK)(?!\w)")


@dataclass(frozen=True)
class Entry:
    item: DigestItem
    kind: str  # "symbol" | "rotation"
    raw: dict
    data_date: str | None


# ---------- 名称簿：保证「名称（代码）」且全文叫法一致 ----------


class NameBook:
    def __init__(self, names: dict[str, str]):
        self._names = names
        self._warned: set[str] = set()

    @classmethod
    def from_reports(cls, reports: list[ReportRecord], rotations: list[dict]) -> NameBook:
        names: dict[str, str] = {}

        def add(code: object, name: object) -> None:
            code, name = str(code or "").strip(), str(name or "").strip()
            if code and name and name != code:
                names.setdefault(code, name)

        for record in reports:
            add(record.symbol, record.name)
        for raw in rotations:
            holding = raw.get("currentHolding") or {}
            add(holding.get("symbol"), holding.get("name"))
            for entry in raw.get("universeRanking") or []:
                if isinstance(entry, dict):
                    add(entry.get("symbol"), entry.get("name"))
        return cls(names)

    def name(self, code: str) -> str | None:
        return self._names.get(code) or KNOWN_SYMBOL_NAMES.get(code)

    def short(self, code: str) -> str:
        """只要名称（总览里省宽度用）；没有名称时退回「未知名称（代码）」。"""
        return self.name(code) or self.label(code)

    def label(self, code: object) -> str:
        text = str(code or "").strip()
        if not text:
            return DASH
        name = self.name(text)
        if name:
            return f"{name}（{text}）"
        if text not in self._warned:
            self._warned.add(text)
            log("WARN", f"日报里缺少 {text} 的名称，已写作「{UNKNOWN_NAME}（{text}）」")
        return f"{UNKNOWN_NAME}（{text}）"

    def annotate(self, text: object) -> str:
        """把自由文本里孤立的代码补成「名称（代码）」；已写成「名称（代码）」的不重复处理。"""
        source = str(text)

        def replace(match: re.Match[str]) -> str:
            if match.start() > 0 and source[match.start() - 1] == "（":
                return match.group(0)
            return self.label(match.group(1))

        return CODE_RE.sub(replace, source)


# ---------- 小工具 ----------


def _e(value: object) -> str:
    return md_escape(value)


def _num(value: object, digits: int = 2, signed: bool = False) -> str:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DASH
    return f"{number:+.{digits}f}" if signed else f"{number:.{digits}f}"


def _pct(value: object, signed: bool = True) -> str:
    text = _num(value, 2, signed)
    return text if text == DASH else f"{text}%"


def _is_wide(ch: str) -> bool:
    return unicodedata.east_asian_width(ch) in ("W", "F") or ord(ch) >= 0x1F000


def display_width(text: str) -> int:
    """显示宽度：全角/emoji 算 2，其余算 1；转义用的反斜杠与粗体星号不占宽。"""
    return sum(0 if ch in "\\*" else (2 if _is_wide(ch) else 1) for ch in text)


def wrap_fields(fields: list[str], limit: int = LINE_WIDTH) -> list[str]:
    """把短字段用「｜」拼成行，超出一行宽度就换到下一行（不靠自然折行）。"""
    lines: list[str] = []
    current = ""
    for field in fields:
        candidate = f"{current}{FIELD_SEP}{field}" if current else field
        if current and display_width(candidate) > limit:
            lines.append(current)
            current = field
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def split_reason(text: str, limit: int = LINE_WIDTH) -> list[str]:
    """长依据在「；」与「 → 」处拆成多行（不靠自然折行）；拆不动的保持原样，绝不丢字。"""
    if display_width(text) <= limit:
        return [text]
    pieces: list[str] = []
    for part in (p.strip() for p in text.split("；") if p.strip()):
        if display_width(part) > limit and " → " in part:
            first, *rest = part.split(" → ")
            pieces.append(first.strip())
            pieces.extend(f"→ {r.strip()}" for r in rest)
        else:
            pieces.append(part)
    return pieces or [text]


def _reason_lines(label: str, text: str) -> list[str]:
    """「依据 xxx」：第一行带标签，其余续行不带，已转义，可直接作为列表项。"""
    pieces = split_reason(text)
    return [_e(f"{label} {pieces[0]}"), *(_e(piece) for piece in pieces[1:])]


def _bullets(lines: Iterable[str]) -> list[str]:
    return [f"- {line}" for line in lines]


def _trade_lines(label: str, trade: dict | None, book: NameBook) -> list[str]:
    """交易/换仓记录：一行放不下时把原因另起一行。"""
    if not trade:
        return [_e(f"{label} {DASH}")]
    action = TRADE_LABELS.get(str(trade.get("action")).upper(), str(trade.get("action") or "交易"))
    text = f"{format_trade_date(trade.get('date'))} {action} @{format_price(trade.get('price'))}"
    if trade.get("quantity") not in (None, ""):
        text += f" ×{trade['quantity']}"
    if trade.get("profitLossPercentage") is not None:
        text += f"，{_pct(trade['profitLossPercentage'])}"
    reason = book.annotate(str(trade.get("reason") or "").strip())
    if not reason:
        return [_e(f"{label} {text}")]
    inline = f"{label} {text}（{reason}）"
    if display_width(inline) <= LINE_WIDTH:
        return [_e(inline)]
    return [_e(f"{label} {text}"), *_reason_lines("原因", reason)]


def _metric_value(raw: dict, name: str) -> object:
    for key in ("returnMetrics", "riskMetrics", "riskAdjustedMetrics", "tradingMetrics"):
        for metric in raw.get(key) or []:
            if isinstance(metric, dict) and metric.get("name") == name:
                return metric.get("value")
    groups = raw.get("metrics")
    if isinstance(groups, dict):
        for group in groups.values():
            for metric in group or []:
                if isinstance(metric, dict) and metric.get("name") == name:
                    return metric.get("value")
    return None


def _entry_label(entry: Entry, book: NameBook) -> str:
    """「名称（代码）」：单标的用名称簿；轮动策略是「策略名（策略 id）」。"""
    if entry.kind == "symbol":
        return book.label(entry.item.key)
    base = base_strategy_name(str(entry.raw.get("strategyName") or entry.item.key))
    return f"{base}（{entry.item.key}）"


def _head_line(entry: Entry, book: NameBook) -> str:
    item = entry.item
    return f"{ACTION_EMOJI[item.action]} **{_e(_entry_label(entry, book))}**"


# ---------- 每个标的的快照与细节 ----------


def _symbol_snapshot(raw: dict) -> dict[str, str]:
    latest = raw.get("latestSignal") or {}
    position = raw.get("positionInfo") or {}
    stop = latest.get("stopLoss") if isinstance(latest.get("stopLoss"), dict) else None
    trades = [t for t in raw.get("recentTrades") or [] if isinstance(t, dict)]
    last = trades[-1] if trades else None
    return {
        "position": f"{position['position_size']}股" if position.get("position_size") else "空仓",
        "pnl": _pct(position.get("unrealized_pnl_pct")) if position else DASH,
        "stop": format_price(stop.get("price")) if stop and stop.get("price") is not None else DASH,
        "switch": (
            f"{format_trade_date(last.get('date'))} {TRADE_LABELS.get(str(last.get('action')).upper(), '')}".strip()
            if last
            else DASH
        ),
    }


def _rotation_snapshot(raw: dict, book: NameBook) -> dict[str, str]:
    holding = raw.get("currentHolding") or {}
    rotations = [r for r in raw.get("rotations") or [] if isinstance(r, dict)]
    if holding:
        switch = f"{format_trade_date(holding.get('since'))} 入场"
    elif rotations:
        switch = f"{format_trade_date(rotations[-1].get('exitDate'))} 清仓"
    else:
        switch = DASH
    return {
        "position": book.short(str(holding.get("symbol"))) if holding.get("symbol") else "空仓",
        "pnl": _pct(holding.get("unrealizedPnlPct")) if holding else DASH,
        "stop": "",
        "switch": switch,
    }


def _status_lines(entry: Entry, book: NameBook, level: int) -> list[str]:
    """一个标的在总览里的行：第 1 行名称+动作，其后是短字段。level 越大越精简（仅第 1 条放不下时用）。"""
    snap = (
        _rotation_snapshot(entry.raw, book)
        if entry.kind == "rotation"
        else _symbol_snapshot(entry.raw)
    )
    head = f"{_head_line(entry, book)} {ACTION_DISPLAY_LABEL[entry.item.action]}"
    if level >= 3:
        return [head]
    fields = [f"持仓 {snap['position']}", f"浮盈 {snap['pnl']}"]
    if level <= 1 and snap["stop"]:
        fields.append(f"止损 {snap['stop']}")
    if level == 0:
        with_switch = wrap_fields([*fields, f"最近切换 {snap['switch']}"])
        fields_lines = (
            with_switch if len(with_switch) <= 2 else wrap_fields(fields)
        )  # 超过两行就舍去最近切换
    else:
        fields_lines = wrap_fields(fields)
    return [head, *_bullets(_e(line) for line in fields_lines)]


def _basis(entry: Entry, book: NameBook) -> str:
    """一句话依据：买入/卖出条件，或轮动的下次动作原因（里面的代码补成名称（代码））。"""
    raw = entry.raw
    if entry.kind == "rotation":
        return book.annotate(str((raw.get("nextSignal") or {}).get("reason") or "").strip()) or DASH
    latest = raw.get("latestSignal") or {}
    conditions = [str(c) for c in latest.get("conditions") or [] if c]
    sells = [
        t
        for t in raw.get("recentTrades") or []
        if isinstance(t, dict) and str(t.get("action")).upper() == "SELL"
    ]
    if entry.item.action == "卖出" and sells and sells[-1].get("reason"):
        return book.annotate(str(sells[-1]["reason"]).strip())
    if conditions:
        return book.annotate("；".join(conditions))
    return {"买入": "出现买入信号", "卖出": "触发卖出条件"}.get(entry.item.action, DASH)


def _symbol_detail(entry: Entry, book: NameBook) -> list[str]:
    raw = entry.raw
    latest = raw.get("latestSignal") or {}
    prices = latest.get("prices") or {}
    position = raw.get("positionInfo") or {}
    stop = latest.get("stopLoss") if isinstance(latest.get("stopLoss"), dict) else None
    trades = [t for t in raw.get("recentTrades") or [] if isinstance(t, dict)]
    lines: list[str] = []
    price = format_price(prices.get("close")) if prices.get("close") is not None else DASH
    lines += wrap_fields(
        [_e(f"现价 {price}")]
        + (
            [
                _e(f"成本 {_num(position.get('cost'))}"),
                _e(f"市值 {_num(position.get('current_value'))}"),
            ]
            if position
            else []
        )
    )
    if stop and stop.get("price") is not None:
        lines.append(
            _e(
                f"止损 {stop.get('type') or '止损'} {format_price(stop.get('price'))}（距现价 {_pct(stop.get('distancePct'), False)}）"
            )
        )
    lines += _reason_lines("依据", _basis(entry, book))
    lines += _trade_lines("最近", trades[-1] if trades else None, book)
    lines += _trade_lines("上笔", trades[-2] if len(trades) > 1 else None, book)
    return _bullets(lines)


def _rotation_detail(entry: Entry, book: NameBook) -> list[str]:
    raw = entry.raw
    holding = raw.get("currentHolding") or {}
    next_signal = raw.get("nextSignal") or {}
    rotations = [r for r in raw.get("rotations") or [] if isinstance(r, dict)]
    lines: list[str] = []
    if holding:
        lines.append(_e(f"持仓 {book.label(holding.get('symbol'))}"))
        lines += wrap_fields(
            [
                _e(
                    f"入场 {format_trade_date(holding.get('since'))} @{format_price(holding.get('entryPrice'))}"
                ),
                _e(f"现价 {format_price(holding.get('currentPrice'))}"),
                _e(f"浮盈 {_pct(holding.get('unrealizedPnlPct'))}"),
            ]
        )
    else:
        lines.append("持仓 空仓")
    source = book.label(next_signal.get("rotateFrom")) if next_signal.get("rotateFrom") else "空仓"
    target = book.label(next_signal.get("rotateTo")) if next_signal.get("rotateTo") else "空仓"
    lines += [_e(f"目标 {source}"), _e(f"→ {target}")]
    lines += _reason_lines("依据", _basis(entry, book))
    for label, rotation in zip(
        ("最近换仓", "上一次"), reversed(rotations[-RECENT_ROTATIONS_SHOWN:])
    ):
        lines.append(_e(f"{label} {book.label(rotation.get('symbol'))}"))
        lines += wrap_fields(
            [
                _e(
                    f"{format_trade_date(rotation.get('entryDate'))} → {format_trade_date(rotation.get('exitDate'))}"
                ),
                _e(f"{rotation.get('holdingDays', DASH)} 天"),
                _e(_pct(rotation.get("periodReturnPct"))),
            ]
        )
    if not rotations:
        lines.append("最近换仓 —")
    return _bullets(lines)


def _ranking_block(raw: dict, book: NameBook) -> Block | None:
    ranking = [r for r in raw.get("universeRanking") or [] if isinstance(r, dict)]
    if not ranking:
        return None
    lines = ["**动量排名**（✅ 为目标持仓）", ""]
    for index, r in enumerate(ranking):
        if index:
            lines.append("")
        mark = "✅ " if r.get("selected") else ""
        momentum = DASH if r.get("momentum") is None else _num(r.get("momentum"), 4, True)
        fields = [
            f"动量 {momentum}",
            f"现价 {format_price(r.get('currentPrice'))}",
            f"权重 {_num(r.get('targetWeight'))}",
        ]
        lines.append(f"第{_e(r.get('rank', DASH))}名 {mark}{_e(book.label(r.get('symbol')))}")
        lines += _bullets(_e(line) for line in wrap_fields(fields))
    return Block(tuple(lines), body_start=2, grouped=True)


# ---------- 各小节 ----------


def _data_date(kind: str, raw: dict) -> str | None:
    if kind == "rotation":
        value = (raw.get("nextSignal") or {}).get("evaluatedAt")
    else:
        value = (raw.get("dateRange") or {}).get("end") or (raw.get("latestSignal") or {}).get(
            "timestamp"
        )
    text = format_trade_date(value) if value else ""
    return text if text and text != "暂无" else None


def _conclusion(
    entries: list[Entry], book: NameBook, warnings: list[str], freshness_lines: list[str]
) -> list[str]:
    """今日结论：每条要点一组（第一行名称+动作，其后是依据），组之间空行分隔。"""
    if not entries:
        return ["⚠️ 未取得任何当日数据，今日无法判断"]
    groups: list[list[str]] = []
    trades = [e for e in entries if e.item.action in TRADE_ACTIONS]
    for entry in trades:
        groups.append(
            [
                f"{_head_line(entry, book)} {ACTION_DISPLAY_LABEL[entry.item.action]}",
                *_bullets(_reason_lines("依据", _basis(entry, book))),
            ]
        )
    if not trades:
        hold = sum(1 for e in entries if e.item.action == "持有")
        groups.append(["今日无信号变化", *_bullets([f"持有 {hold}｜空仓 {len(entries) - hold}"])])
    groups += [[f"⚠️ {_e(line)}"] for line in freshness_lines]
    if warnings:
        groups.append([f"⚠️ 有 {len(warnings)} 条数据提示，见「数据提示」"])
    lines: list[str] = []
    for index, group in enumerate(groups):
        if index:
            lines.append("")
        lines += group
    return lines


def _status_block(entries: list[Entry], book: NameBook) -> Block:
    def variant(level: int) -> tuple[str, ...]:
        lines: list[str] = ["## 状态总览", ""]
        for index, entry in enumerate(entries):
            if index:
                lines.append("")
            lines += _status_lines(entry, book, level)
        return tuple(lines)

    # 第 1 条放不下时依次去掉最次要的字段：最近切换 → 止损 → 只留名称与动作
    return Block(
        variant(0), body_start=2, grouped=True, alternatives=tuple(variant(n) for n in (1, 2, 3))
    )


def _perf_blocks(entries: list[Entry], book: NameBook) -> list[Block]:
    def grouped(title: str, groups: list[list[str]]) -> Block | None:
        if not groups:
            return None
        lines: list[str] = [title, ""]
        for index, group in enumerate(groups):
            if index:
                lines.append("")
            lines += group
        return Block(tuple(lines), body_start=2, grouped=True)

    perf, annual, params = [], [], []
    for entry in entries:
        raw = entry.raw
        summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
        label = f"**{_e(_entry_label(entry, book))}**"

        def pick(
            summary_key: str, metric_name: str, raw: dict = raw, summary: dict = summary
        ) -> object:
            value = summary.get(summary_key)
            return value if value is not None else _metric_value(raw, metric_name)

        drawdown, current_dd = pick("maxDrawdown", "最大回撤"), _metric_value(raw, "当前回撤")
        fields = [
            f"年化 {_pct(pick('annualReturn', '年化收益率'))}",
            f"最大回撤 {_pct(-abs(float(drawdown))) if isinstance(drawdown, (int, float)) else DASH}",
            f"当前回撤 {_pct(-abs(float(current_dd))) if isinstance(current_dd, (int, float)) else DASH}",
            f"夏普 {_num(pick('sharpe', '夏普比率'))}",
            f"胜率 {_pct(_metric_value(raw, '胜率'), False)}",
        ]
        perf.append([label, *_bullets(_e(line) for line in wrap_fields(fields))])
        years = [y for y in raw.get("annualReturns") or [] if isinstance(y, dict) and "year" in y]
        if years:
            annual.append(
                [
                    label,
                    *_bullets(
                        _e(line)
                        for line in wrap_fields(
                            [f"{y['year']} {_pct(y.get('value'))}" for y in years]
                        )
                    ),
                ]
            )
        config = raw.get("config")
        if isinstance(config, dict) and config:
            params.append(
                [label, *_bullets(_e(f"{k} {book.annotate(v)}") for k, v in config.items())]
            )
    blocks = [
        grouped("## 回测与绩效", perf),
        grouped("**年度收益**", annual),
        grouped("**策略参数**", params),
    ]
    return [b for b in blocks if b]


def _headline(entries: list[Entry]) -> str:
    if not entries:
        return "无数据"
    parts = [
        f"{label} {count}"
        for label in ("买入", "换仓", "卖出")
        if (count := sum(1 for e in entries if e.item.action == label))
    ]
    return "｜".join(parts) if parts else "无信号变化"


@dataclass(frozen=True)
class Collected:
    entries: list[Entry]
    book: NameBook


def collect_entries(
    reports: list[ReportRecord], target_date: date, rotation_reports: list[dict] | None
) -> Collected:
    """把单标的与轮动报告整理成按动作优先级排序的条目，并建好名称簿。"""
    date_str = target_date.isoformat()
    rotation_reports = rotation_reports or []
    book = NameBook.from_reports(reports, rotation_reports)
    entries: list[Entry] = []
    for record in reports:
        entries.append(
            Entry(build_symbol_item(record), "symbol", record.raw, _data_date("symbol", record.raw))
        )
    for raw in rotation_reports:
        entries.append(
            Entry(build_rotation_item(raw, date_str), "rotation", raw, _data_date("rotation", raw))
        )
    entries.sort(key=lambda e: (ACTION_PRIORITY.get(e.item.action, 99), e.item.key))
    return Collected(entries, book)


def build_digest_parts(
    reports: list[ReportRecord],
    target_date: date,
    rotation_reports: list[dict] | None = None,
    warnings: Iterable[str] = (),
    now: dt.datetime | None = None,
) -> DigestParts:
    """完整日报的结构化版本：摘要块（第 1 条）+ 按标的/小节划分的单元，供分段与整体渲染。

    now（naive UTC，缺省取当前时刻）用来推算各市场「应有的最后完成交易日」，作为数据新鲜度的参照日。"""
    date_str = target_date.isoformat()
    collected = collect_entries(reports, target_date, rotation_reports)
    entries, book = collected.entries, collected.book
    fresh = classify(entries, now)
    latest = reference_label(fresh)
    freshness_lines = freshness_warnings(entries, fresh, lambda e: _entry_label(e, book), cap=None)
    warning_list = [book.annotate(w) for w in warnings if w]

    summary = [
        Block((f"# 📊 交易信号日报 · {date_str}",)),
        Block((f"数据截至 {latest or DASH}｜共 {len(entries)} 项",)),
        Block(
            ("## 今日结论", "", *_conclusion(entries, book, warning_list, freshness_lines)),
            body_start=2,
        ),
    ]
    units: list[Unit] = []
    if entries:
        summary.append(_status_block(entries, book))
        for index, entry in enumerate(entries):
            title = f"**{ACTION_EMOJI[entry.item.action]} {_e(_entry_label(entry, book))}**"
            bullets = (
                _rotation_detail(entry, book)
                if entry.kind == "rotation"
                else _symbol_detail(entry, book)
            )
            blocks = [Block((title, *bullets), body_start=1)]
            if entry.kind == "rotation" and (ranking := _ranking_block(entry.raw, book)):
                blocks.append(ranking)
            if index == 0:  # 小节标题跟第一个标的放在一起
                first = blocks[0]
                blocks[0] = Block(("## 标的详情", "", *first.lines), body_start=3)
            units.append(Unit(_entry_label(entry, book), tuple(blocks), rule_before=index > 0))
        units.append(Unit("回测与绩效", tuple(_perf_blocks(entries, book))))
    if warning_list:
        units.append(
            Unit(
                "数据提示",
                (Block(("## 数据提示", "", *[f"- {_e(w)}" for w in warning_list]), body_start=2),),
            )
        )
    units.append(
        Unit(
            "边界声明",
            (Block(("## 边界声明", "", *[f"- {line}" for line in BOUNDARY_LINES]), body_start=2),),
        )
    )
    return DigestParts(
        label=f"trading 日报 {date_str}",
        headline=_headline(entries),
        summary=tuple(summary),
        units=tuple(units),
    )


def build_full_markdown(
    reports: list[ReportRecord],
    target_date: date,
    rotation_reports: list[dict] | None = None,
    warnings: Iterable[str] = (),
    now: dt.datetime | None = None,
) -> str:
    """完整日报 Markdown（不分段）；没有任何数据时在「今日结论」里直说，并保留边界声明。"""
    return render_full(build_digest_parts(reports, target_date, rotation_reports, warnings, now))
