"""完整版日报（Markdown）：给 iGotify 与邮件用，PushGo 的精简正文保持不变。

与 `build_digest` 的区别：精简正文只挑「信号动作/持仓/浮盈」几行；这里把 JSON 里和操作相关的
内容全部写出来——止损位、触发条件、最近交易与换仓（含原因）、轮动目标与各标的动量排名、绩效指标、
年度收益、策略参数、数据日期、边界声明——不做任何长度截断。

语法只用 iGotify/Gotify 客户端确定支持的子集：标题、粗体、无序/有序列表、分隔线；
不用表格（宽表在手机上会溢出，且 iOS 端渲染范围未公开）、不用图片/HTML；
所有来自数据的文本先经 `md_escape`，防止被解析成链接/图片/强调。
"""

from __future__ import annotations

from datetime import date
from typing import Iterable

from .channels.markup import md_escape
from .digest import (
    ACTION_DISPLAY_LABEL,
    ACTION_EMOJI,
    ACTION_PRIORITY,
    DigestItem,
    ReportRecord,
    build_rotation_item,
    build_symbol_item,
    format_price,
    format_trade_date,
    resolve_symbol_name,
)

RECENT_TRADES_SHOWN = 5
RECENT_ROTATIONS_SHOWN = 3

# 与前端 QuantTradeReport.tsx 的单位口径一致
PERCENT_METRICS = {
    "年化收益率",
    "复合年化收益",
    "Alpha",
    "最大回撤",
    "当前回撤",
    "波动率",
    "胜率",
    "持仓比例",
    "最大亏损比例",
}
LOSS_MAGNITUDE_METRICS = {"最大回撤", "当前回撤", "最大亏损金额", "最大亏损比例", "平均亏损"}
MONEY_METRICS = {"总盈亏", "平均盈利", "平均亏损", "最大亏损金额"}
METRIC_GROUPS = (
    ("returnMetrics", "收益"),
    ("riskMetrics", "风险"),
    ("riskAdjustedMetrics", "风险调整"),
    ("tradingMetrics", "交易"),
    ("positionMetrics", "持仓"),
    ("benchmarkMetrics", "基准"),
)

BOUNDARY_LINES = (
    "以上是策略对历史行情的机械计算结果（信号监测），仅供参考，不构成投资建议；系统只提示、不自动下单。",
    "止损位、动量排名等是策略的参考位与排序，不是委托价；回测与历史表现不代表未来收益。",
    "各标的数据截止日以「数据日期」为准；数据缺失或滞后时，该标的的结论可能已过期。",
)


def _e(value: object) -> str:
    return md_escape(value)


def _num(value: object, digits: int = 2, signed: bool = False) -> str:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "暂无"
    return f"{number:+.{digits}f}" if signed else f"{number:.{digits}f}"


def _metric_text(metric: dict) -> str:
    name = str(metric.get("name") or "")
    value = metric.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        shown = _e(value if value not in (None, "") else "暂无")
    else:
        signed = -value if name in LOSS_MAGNITUDE_METRICS and value > 0 else value
        if name in PERCENT_METRICS:
            shown = f"{signed:.2f}%"
        elif name in MONEY_METRICS:
            shown = f"¥{signed:.2f}"
        elif isinstance(value, int):
            shown = f"{value:,}"
        else:
            shown = f"{signed:.2f}"
    return f"{_e(name)} {shown}"


def _metric_lines(raw: dict) -> list[str]:
    lines = []
    for key, label in METRIC_GROUPS:
        metrics = [m for m in raw.get(key) or [] if isinstance(m, dict) and m.get("name")]
        if metrics:
            lines.append(f"- **{label}**：" + " ｜ ".join(_metric_text(m) for m in metrics))
    return lines


def _annual_line(raw: dict) -> list[str]:
    years = [y for y in raw.get("annualReturns") or [] if isinstance(y, dict) and "year" in y]
    if not years:
        return []
    parts = [f"{_e(y['year'])} {_num(y.get('value'), 2, True)}%" for y in years]
    return ["- **年度收益**：" + " ｜ ".join(parts)]


def _symbol_extra_lines(raw: dict) -> list[str]:
    lines: list[str] = []
    latest = raw.get("latestSignal") or {}
    date_range = raw.get("dateRange") or {}
    lines.append(
        f"数据日期：{_e(format_trade_date(date_range.get('end') or latest.get('timestamp')))}"
        f"，报告日期：{_e(raw.get('reportDate') or '暂无')}"
    )
    prices = latest.get("prices") or {}
    if prices:
        lines.append(
            "最新价："
            + " ｜ ".join(
                f"{label} {format_price(prices.get(key))}"
                for key, label in (("open", "开"), ("high", "高"), ("low", "低"), ("close", "收"))
                if prices.get(key) is not None
            )
        )
    stop = latest.get("stopLoss")
    if isinstance(stop, dict) and stop.get("price") is not None:
        lines.append(
            f"止损位：{_e(stop.get('type') or '止损')} {format_price(stop.get('price'))}"
            f"（距现价 {_num(stop.get('distancePct'))}%）"
        )
    for condition in latest.get("conditions") or []:
        lines.append(f"触发条件：{_e(condition)}")
    position = raw.get("positionInfo") or {}
    if position:
        lines.append(
            f"持仓：{_e(position.get('position_size', '暂无'))} 股，"
            f"成本 {_num(position.get('cost'))}，市值 {_num(position.get('current_value'))}，"
            f"浮盈亏 {_num(position.get('unrealized_pnl'), 2, True)}"
        )
    trades = [t for t in raw.get("recentTrades") or [] if isinstance(t, dict)]
    for trade in reversed(trades[-RECENT_TRADES_SHOWN:]):
        pnl = trade.get("profitLossPercentage")
        pnl_text = f"，收益 {_num(pnl, 2, True)}%" if pnl is not None else ""
        reason = f"，原因：{_e(trade['reason'])}" if trade.get("reason") else ""
        lines.append(
            f"交易记录：{_e(format_trade_date(trade.get('date')))} {_e(trade.get('action'))} "
            f"价 {format_price(trade.get('price'))} 量 {_e(trade.get('quantity', '暂无'))}{pnl_text}{reason}"
        )
    return lines


def _rotation_extra_lines(raw: dict) -> list[str]:
    lines: list[str] = []
    next_signal = raw.get("nextSignal") or {}
    date_range = raw.get("dateRange") or {}
    evaluated = next_signal.get("evaluatedAt")
    lines.append(
        f"数据日期：{_e(format_trade_date(evaluated))}，报告日期：{_e(raw.get('reportDate') or '暂无')}"
        f"，回测区间：{_e(format_trade_date(date_range.get('start')))} ~ "
        f"{_e(format_trade_date(date_range.get('end')))}"
    )
    rotate_from, rotate_to = next_signal.get("rotateFrom"), next_signal.get("rotateTo")
    if rotate_from or rotate_to:
        lines.append(
            f"轮动目标：{_e(_label(rotate_from) if rotate_from else '空仓')} → "
            f"{_e(_label(rotate_to) if rotate_to else '空仓')}"
        )
    rotations = [r for r in raw.get("rotations") or [] if isinstance(r, dict)]
    for rotation in reversed(rotations[-RECENT_ROTATIONS_SHOWN:]):
        lines.append(
            f"最近换仓：{_e(_label(rotation.get('symbol')))}，"
            f"{_e(format_trade_date(rotation.get('entryDate')))} → "
            f"{_e(format_trade_date(rotation.get('exitDate')))}，持有 {_e(rotation.get('holdingDays', '暂无'))} 天，"
            f"入场 {format_price(rotation.get('entryPrice'))} 出场 {format_price(rotation.get('exitPrice'))}，"
            f"收益 {_num(rotation.get('periodReturnPct'), 2, True)}%"
        )
    return lines


def _label(symbol: object) -> str:
    text = str(symbol or "暂无")
    name = resolve_symbol_name(text)
    return text if name == text else f"{text}（{name}）"


def _ranking_lines(raw: dict) -> list[str]:
    ranking = [r for r in raw.get("universeRanking") or [] if isinstance(r, dict)]
    if not ranking:
        return []
    lines = ["**动量排名**（候选池按动量降序，✅ 为目标持仓）："]
    for entry in ranking:
        mark = "✅ " if entry.get("selected") else ""
        weight = _num(entry.get("targetWeight"), 2)
        momentum = "暂无" if entry.get("momentum") is None else _num(entry.get("momentum"), 4, True)
        name = resolve_symbol_name(str(entry.get("symbol") or ""), entry.get("name"))
        lines.append(
            f"{entry.get('rank', '-')}. {mark}{_e(name)}（{_e(entry.get('symbol'))}）"
            f" 动量 {momentum} ｜ 现价 {format_price(entry.get('currentPrice'))} ｜ 目标权重 {weight}"
        )
    return lines


def _summary_line(raw: dict) -> list[str]:
    summary = raw.get("summary")
    if not isinstance(summary, dict):
        return []
    parts = [
        ("总收益", summary.get("totalReturnPct"), "%", True),
        ("年化", summary.get("annualReturn"), "%", True),
        (
            "最大回撤",
            None if summary.get("maxDrawdown") is None else -abs(float(summary["maxDrawdown"])),
            "%",
            True,
        ),
        ("夏普", summary.get("sharpe"), "", False),
        ("波动率", summary.get("volatility"), "%", False),
    ]
    text = " ｜ ".join(
        f"{name} {_num(v, 2, signed)}{unit}" for name, v, unit, signed in parts if v is not None
    )
    return [f"- **回测概览**：{text}"] if text else []


def _config_line(raw: dict) -> list[str]:
    config = raw.get("config")
    if not isinstance(config, dict) or not config:
        return []
    return ["- **策略参数**：" + " ｜ ".join(f"{_e(k)}={_e(v)}" for k, v in config.items())]


def _item_section(
    item: DigestItem, extra: Iterable[str], tail: Iterable[str], ranking: Iterable[str] = ()
) -> list[str]:
    lines = [
        f"### {ACTION_EMOJI[item.action]} {_e(item.name)}（{_e(item.key)}）· {ACTION_DISPLAY_LABEL[item.action]}"
    ]
    lines.extend(f"- {_e(line)}" for line in item.detail_lines)
    lines.extend(f"- {line}" for line in extra)
    ranking_lines = list(ranking)
    if ranking_lines:
        lines.append("")
        lines.extend(ranking_lines)
        lines.append("")
    lines.extend(tail)
    return lines


def build_full_markdown(
    reports: list[ReportRecord],
    target_date: date,
    rotation_reports: list[dict] | None = None,
    warnings: Iterable[str] = (),
) -> str:
    """完整日报 Markdown；没有任何数据时给出「无数据」说明与边界声明。"""
    rotation_reports = rotation_reports or []
    date_str = target_date.isoformat()
    entries: list[tuple[DigestItem, list[str]]] = []
    for record in reports:
        raw = record.raw
        item = build_symbol_item(record)
        tail = _annual_line(raw) + _metric_lines(raw)
        entries.append((item, _item_section(item, _symbol_extra_lines(raw), tail)))
    for raw in rotation_reports:
        item = build_rotation_item(raw, date_str)
        tail = _summary_line(raw) + _annual_line(raw) + _metric_lines(raw) + _config_line(raw)
        entries.append(
            (item, _item_section(item, _rotation_extra_lines(raw), tail, _ranking_lines(raw)))
        )
    entries.sort(key=lambda pair: (ACTION_PRIORITY.get(pair[0].action, 99), pair[0].key))

    lines = [f"# 📊 交易信号日报 · {date_str}"]
    if entries:
        counts = {
            label: sum(1 for item, _ in entries if item.action == label)
            for label in ACTION_PRIORITY
        }
        lines.append(
            " ｜ ".join(
                f"{ACTION_EMOJI[label]} {ACTION_DISPLAY_LABEL[label]} {counts[label]}"
                for label in ACTION_PRIORITY
            )
        )
        lines += ["", "## 🎯 信号总览"]
        for item, _ in entries:
            suffix = f" {_e(item.overview_suffix)}" if item.overview_suffix else ""
            lines.append(
                f"- {ACTION_EMOJI[item.action]} **{_e(item.name)}**（{_e(item.key)}）"
                f"{ACTION_DISPLAY_LABEL[item.action]}{suffix}"
            )
        lines += ["", "---", "", "## 📌 详情"]
        for index, (_, section) in enumerate(entries):
            if index:
                lines += ["", "---"]
            lines.append("")
            lines.extend(section)
    else:
        lines += ["", "- 状态：无任何数据可用", "- 说明：未找到可用于汇总的当日 JSON 数据。"]

    warning_list = [w for w in warnings if w]
    if warning_list:
        lines += ["", "---", "", "## 🔎 数据提示"]
        lines.extend(f"- {_e(w)}" for w in warning_list)

    lines += ["", "---", "", "## ⚠️ 边界声明"]
    lines.extend(f"- {line}" for line in BOUNDARY_LINES)
    return "\n".join(lines) + "\n"
