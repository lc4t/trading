"""完整版日报（Markdown）：给 iGotify 与邮件用，PushGo 的精简正文保持不变。

版式（先结论、再总表、再细节、最后绩效）：
    # 标题 + 数据截止行
    ## 今日结论      —— 今天要不要动作，逐个点名；数据缺失/落后也写在这里
    ## 状态总表      —— 每个标的/策略一行：标的｜动作｜持仓｜浮盈｜止损位｜最近切换
    ## 标的详情      —— 每个标的同样的字段顺序；轮动策略多一张动量排名表
    ## 回测与绩效    —— 一张关键指标表 + 年度收益 + 策略参数
    ## 数据提示 / 边界声明

语法只用 iGotify 实测能渲染的子集：二级标题、粗体、列表、GFM 表格、分隔线（前后各留空行，
否则 `---` 紧跟文字会被当成 Setext 标题）。标题只用两级；来自数据的文本一律经转义，防止注入。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

from .channels.markup import md_escape
from .digest import (
    ACTION_DISPLAY_LABEL,
    ACTION_EMOJI,
    ACTION_PRIORITY,
    TRADE_ACTIONS,
    DigestItem,
    ReportRecord,
    build_rotation_item,
    build_symbol_item,
    format_price,
    format_trade_date,
    resolve_symbol_name,
)

RECENT_ROTATIONS_SHOWN = 2
MAX_LAG_DAYS = 5  # 数据截止日比报告日早超过这个天数（长假之外）就提示
DASH = "—"

# 与前端 QuantTradeReport.tsx 的单位口径一致
LOSS_MAGNITUDE_METRICS = {"最大回撤", "当前回撤"}

BOUNDARY_LINES = (
    "以上是策略对历史行情的机械计算结果（信号监测），仅供参考，不构成投资建议；系统只提示、不自动下单。",
    "止损位、动量排名等是策略的参考位与排序，不是委托价；回测与历史表现不代表未来收益。",
    "各标的数据截止日以「数据日期」为准；数据缺失或滞后时，该标的的结论可能已过期。",
)
TRADE_LABELS = {"BUY": "买入", "SELL": "卖出"}


@dataclass(frozen=True)
class Entry:
    item: DigestItem
    kind: str  # "symbol" | "rotation"
    raw: dict
    data_date: str | None


# ---------- 小工具 ----------


def _e(value: object) -> str:
    return md_escape(value)


def _cell(value: object) -> str:
    return md_escape(value).replace("|", "\\|")


def _table(headers: list[str], rows: list[list[object]]) -> list[str]:
    lines = [
        "| " + " | ".join(_cell(h) for h in headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines.extend("| " + " | ".join(_cell(c) for c in row) + " |" for row in rows)
    return lines


def _num(value: object, digits: int = 2, signed: bool = False) -> str:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DASH
    return f"{number:+.{digits}f}" if signed else f"{number:.{digits}f}"


def _pct(value: object, signed: bool = True) -> str:
    text = _num(value, 2, signed)
    return text if text == DASH else f"{text}%"


def _label(symbol: object, name: object = None) -> str:
    text = str(symbol or "").strip()
    if not text:
        return DASH
    resolved = resolve_symbol_name(text, None if name in (None, "") else str(name))
    return text if resolved == text else f"{resolved}（{text}）"


def _short_name(symbol: object, name: object = None) -> str:
    text = str(symbol or "").strip()
    return resolve_symbol_name(text, None if name in (None, "") else str(name)) if text else DASH


def _trade_text(trade: dict | None) -> str:
    if not trade:
        return DASH
    action = TRADE_LABELS.get(str(trade.get("action")).upper(), str(trade.get("action") or "交易"))
    parts = [f"{format_trade_date(trade.get('date'))} {action} @{format_price(trade.get('price'))}"]
    if trade.get("quantity") not in (None, ""):
        parts[0] += f" ×{trade['quantity']}"
    if trade.get("profitLossPercentage") is not None:
        parts.append(_pct(trade["profitLossPercentage"]))
    text = "，".join(parts)
    reason = str(trade.get("reason") or "").strip()
    return f"{text}（{reason}）" if reason else text


def _metric_value(raw: dict, name: str) -> object:
    for key in ("returnMetrics", "riskMetrics", "riskAdjustedMetrics", "tradingMetrics"):
        for metric in raw.get(key) or []:
            if isinstance(metric, dict) and metric.get("name") == name:
                return metric.get("value")
    for key in ("metrics",):
        groups = raw.get(key)
        if isinstance(groups, dict):
            for group in groups.values():
                for metric in group or []:
                    if isinstance(metric, dict) and metric.get("name") == name:
                        return metric.get("value")
    return None


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
            f"{format_trade_date(last.get('date'))} "
            f"{TRADE_LABELS.get(str(last.get('action')).upper(), '')}".strip()
            if last
            else DASH
        ),
    }


def _rotation_snapshot(raw: dict) -> dict[str, str]:
    holding = raw.get("currentHolding") or {}
    rotations = [r for r in raw.get("rotations") or [] if isinstance(r, dict)]
    if holding:
        switch = f"{format_trade_date(holding.get('since'))} 入场"
    elif rotations:
        switch = f"{format_trade_date(rotations[-1].get('exitDate'))} 清仓"
    else:
        switch = DASH
    return {
        "position": _short_name(holding.get("symbol"), holding.get("name")) if holding else "空仓",
        "pnl": _pct(holding.get("unrealizedPnlPct")) if holding else DASH,
        "stop": DASH,
        "switch": switch,
    }


def _basis(entry: Entry) -> str:
    """一句话依据：买入/卖出条件，或轮动的下次动作原因。"""
    raw = entry.raw
    if entry.kind == "rotation":
        return str((raw.get("nextSignal") or {}).get("reason") or "").strip() or DASH
    latest = raw.get("latestSignal") or {}
    conditions = [str(c) for c in latest.get("conditions") or [] if c]
    sells = [
        t
        for t in raw.get("recentTrades") or []
        if isinstance(t, dict) and str(t.get("action")).upper() == "SELL"
    ]
    if entry.item.action == "卖出" and sells and sells[-1].get("reason"):
        return str(sells[-1]["reason"]).strip()
    if conditions:
        return "；".join(conditions)
    return {"买入": "出现买入信号", "卖出": "触发卖出条件"}.get(entry.item.action, DASH)


def _symbol_detail(entry: Entry) -> list[str]:
    raw, item = entry.raw, entry.item
    latest = raw.get("latestSignal") or {}
    prices = latest.get("prices") or {}
    position = raw.get("positionInfo") or {}
    stop = latest.get("stopLoss") if isinstance(latest.get("stopLoss"), dict) else None
    trades = [t for t in raw.get("recentTrades") or [] if isinstance(t, dict)]
    price_text = format_price(prices.get("close")) if prices.get("close") is not None else DASH
    holding_text = (
        f"{position.get('position_size', DASH)} 股 ｜ 成本 {_num(position.get('cost'))} ｜ "
        f"市值 {_num(position.get('current_value'))} ｜ 浮盈 {_pct(position.get('unrealized_pnl_pct'))}"
        if position
        else "空仓"
    )
    stop_text = (
        f"{stop.get('type') or '止损'} {format_price(stop.get('price'))}（距现价 {_pct(stop.get('distancePct'), False)}）"
        if stop and stop.get("price") is not None
        else DASH
    )
    fields = [
        ("动作", ACTION_DISPLAY_LABEL[item.action]),
        ("现价", price_text),
        ("持仓", holding_text),
        ("止损", stop_text),
        ("依据", _basis(entry)),
        ("最近交易", _trade_text(trades[-1] if trades else None)),
        ("上一笔", _trade_text(trades[-2] if len(trades) > 1 else None)),
    ]
    return [f"- **{k}**：{_e(v)}" for k, v in fields]


def _rotation_detail(entry: Entry) -> list[str]:
    raw, item = entry.raw, entry.item
    holding = raw.get("currentHolding") or {}
    next_signal = raw.get("nextSignal") or {}
    rotations = [r for r in raw.get("rotations") or [] if isinstance(r, dict)]
    holding_text = (
        f"{_label(holding.get('symbol'), holding.get('name'))} ｜ 入场 {format_trade_date(holding.get('since'))}"
        f" @{format_price(holding.get('entryPrice'))} ｜ 现价 {format_price(holding.get('currentPrice'))}"
        f" ｜ 浮盈 {_pct(holding.get('unrealizedPnlPct'))}"
        if holding
        else "空仓"
    )
    target_text = (
        f"{_short_name(next_signal.get('rotateFrom')) if next_signal.get('rotateFrom') else '空仓'}"
        f" → {_short_name(next_signal.get('rotateTo')) if next_signal.get('rotateTo') else '空仓'}"
    )
    switches = [
        f"{format_trade_date(r.get('entryDate'))} → {format_trade_date(r.get('exitDate'))} "
        f"{_short_name(r.get('symbol'))}，{r.get('holdingDays', DASH)} 天，{_pct(r.get('periodReturnPct'))}"
        for r in reversed(rotations[-RECENT_ROTATIONS_SHOWN:])
    ]
    fields = [
        ("动作", ACTION_DISPLAY_LABEL[item.action]),
        ("持仓", holding_text),
        ("目标", target_text),
        ("止损", "无（动量轮动不设止损位）"),
        ("依据", _basis(entry)),
        ("最近换仓", switches[0] if switches else DASH),
        ("上一次", switches[1] if len(switches) > 1 else DASH),
    ]
    lines = [f"- **{k}**：{_e(v)}" for k, v in fields]
    ranking = [r for r in raw.get("universeRanking") or [] if isinstance(r, dict)]
    if ranking:
        rows = [
            [
                r.get("rank", DASH),
                ("✅ " if r.get("selected") else "") + _short_name(r.get("symbol"), r.get("name")),
                DASH if r.get("momentum") is None else _num(r.get("momentum"), 4, True),
                format_price(r.get("currentPrice")),
                _num(r.get("targetWeight")),
            ]
            for r in ranking
        ]
        lines += ["", "**动量排名**（✅ 为目标持仓）", ""]
        lines += _table(["#", "标的", "动量", "现价", "权重"], rows)
    return lines


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
    entries: list[Entry], target_date: date, warnings: list[str], latest: str | None
) -> list[str]:
    if not entries:
        return ["- ⚠️ 未取得任何当日数据，今日无法判断"]
    lines: list[str] = []
    trades = [e for e in entries if e.item.action in TRADE_ACTIONS]
    for entry in trades:
        item = entry.item
        lines.append(
            f"- {ACTION_EMOJI[item.action]} **{_e(item.name)}**（{_e(item.key)}）{ACTION_DISPLAY_LABEL[item.action]}："
            f"{_e(_basis(entry))}"
        )
    if not trades:
        hold = sum(1 for e in entries if e.item.action == "持有")
        flat = len(entries) - hold
        lines.append(f"- 今日无信号变化：持有 {hold} ｜ 空仓 {flat}")
    stale = [e for e in entries if latest and e.data_date and e.data_date < latest]
    missing = [e for e in entries if not e.data_date]
    if stale or missing:
        names = [f"{e.item.name}（{e.data_date}）" for e in stale] + [
            f"{e.item.name}（日期缺失）" for e in missing
        ]
        lines.append(
            f"- ⚠️ 数据截止日不一致，以 {latest or DASH} 为准；落后：{_e('、'.join(names))}"
        )
    elif latest:
        try:
            lag = (target_date - date.fromisoformat(latest)).days
        except ValueError:
            lag = 0
        if lag > MAX_LAG_DAYS:
            lines.append(f"- ⚠️ 数据整体落后：截止 {latest}，比报告日早 {lag} 天")
    if warnings:
        lines.append(f"- ⚠️ 有 {len(warnings)} 条数据提示，见「数据提示」")
    return lines


def _status_table(entries: list[Entry]) -> list[str]:
    rows = []
    for entry in entries:
        snap = (
            _rotation_snapshot(entry.raw)
            if entry.kind == "rotation"
            else _symbol_snapshot(entry.raw)
        )
        rows.append(
            [
                f"{entry.item.name}",
                f"{ACTION_EMOJI[entry.item.action]} {ACTION_DISPLAY_LABEL[entry.item.action]}",
                snap["position"],
                snap["pnl"],
                snap["stop"],
                snap["switch"],
            ]
        )
    return _table(["标的", "动作", "持仓", "浮盈", "止损位", "最近切换"], rows)


def _perf_section(entries: list[Entry]) -> list[str]:
    rows, annual, params = [], [], []
    for entry in entries:
        raw = entry.raw
        summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}

        def pick(summary_key: str, metric_name: str) -> object:
            value = summary.get(summary_key)
            return value if value is not None else _metric_value(raw, metric_name)

        drawdown = pick("maxDrawdown", "最大回撤")
        current_dd = _metric_value(raw, "当前回撤")
        rows.append(
            [
                entry.item.name,
                _pct(pick("annualReturn", "年化收益率")),
                _pct(-abs(float(drawdown))) if isinstance(drawdown, (int, float)) else DASH,
                _pct(-abs(float(current_dd))) if isinstance(current_dd, (int, float)) else DASH,
                _num(pick("sharpe", "夏普比率")),
                _pct(_metric_value(raw, "胜率"), False),
            ]
        )
        years = [y for y in raw.get("annualReturns") or [] if isinstance(y, dict) and "year" in y]
        if years:
            annual.append(
                f"- {_e(entry.item.name)}："
                + " ｜ ".join(f"{_e(y['year'])} {_pct(y.get('value'))}" for y in years)
            )
        config = raw.get("config")
        if isinstance(config, dict) and config:
            params.append(
                f"- {_e(entry.item.name)}："
                + " ｜ ".join(f"{_e(k)}={_e(v)}" for k, v in config.items())
            )
    lines = _table(["标的", "年化", "最大回撤", "当前回撤", "夏普", "胜率"], rows)
    if annual:
        lines += ["", "**年度收益**", ""] + annual
    if params:
        lines += ["", "**策略参数**", ""] + params
    return lines


def build_full_markdown(
    reports: list[ReportRecord],
    target_date: date,
    rotation_reports: list[dict] | None = None,
    warnings: Iterable[str] = (),
) -> str:
    """完整日报 Markdown；没有任何数据时在「今日结论」里直说，并保留边界声明。"""
    date_str = target_date.isoformat()
    entries: list[Entry] = []
    for record in reports:
        entries.append(
            Entry(build_symbol_item(record), "symbol", record.raw, _data_date("symbol", record.raw))
        )
    for raw in rotation_reports or []:
        entries.append(
            Entry(build_rotation_item(raw, date_str), "rotation", raw, _data_date("rotation", raw))
        )
    entries.sort(key=lambda e: (ACTION_PRIORITY.get(e.item.action, 99), e.item.key))
    warning_list = [w for w in warnings if w]
    dates = [e.data_date for e in entries if e.data_date]
    latest = max(dates) if dates else None

    lines = [f"# 📊 交易信号日报 · {date_str}", ""]
    lines.append(f"数据截止 {latest or DASH} ｜ 报告日 {date_str} ｜ 共 {len(entries)} 项")
    lines += ["", "## 今日结论", ""] + _conclusion(entries, target_date, warning_list, latest)

    if entries:
        lines += ["", "## 状态总表", ""] + _status_table(entries)
        lines += ["", "## 标的详情"]
        for index, entry in enumerate(entries):
            if index:
                lines += ["", "---"]
            item = entry.item
            lines += ["", f"**{ACTION_EMOJI[item.action]} {_e(item.name)}（{_e(item.key)}）**", ""]
            lines += _rotation_detail(entry) if entry.kind == "rotation" else _symbol_detail(entry)
        lines += ["", "## 回测与绩效", ""] + _perf_section(entries)

    if warning_list:
        lines += ["", "## 数据提示", ""] + [f"- {_e(w)}" for w in warning_list]

    lines += ["", "## 边界声明", ""] + [f"- {line}" for line in BOUNDARY_LINES]
    return "\n".join(lines) + "\n"
