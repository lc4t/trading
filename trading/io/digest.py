"""交易信号日报：汇总当日单标的 + 动量轮动 JSON 报告，生成推送标题/正文。

数据源约定：
- 单标的报告：data_dir/*.json（backtest CLI 的 --output-json 产物）
- 轮动策略报告：data_dir/rotation/*.json（rotation_backtest CLI 的 --output-json 产物）
只汇总 reportDate 等于目标日期的 JSON；单标的和轮动策略共用同一套
买入/卖出/持有/观望 展示逻辑（见 DigestItem）。
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable
from urllib import error, request

DEFAULT_PUSHGO_URL = "https://gateway.pushgo.dev/push"
DEFAULT_TIMEZONE = "Asia/Shanghai"
TRADE_ACTIONS = {"买入", "卖出", "换仓"}
ACTION_PRIORITY = {"买入": 0, "换仓": 1, "卖出": 2, "持有": 3, "观望": 4}
ACTION_ALIASES = {
    "BUY": "买入",
    "SELL": "卖出",
    "HOLD": "持有",
    "WATCH": "观望",
    "OBSERVE": "观望",
    "观察": "观望",
}
ROTATION_ACTION_LABELS = {
    "HOLD": "持有",
    "ROTATE": "换仓",
    "EXIT": "清仓",
}
ACTION_EMOJI = {"买入": "🟢", "换仓": "🔄", "卖出": "🔴", "持有": "🟡", "观望": "⚪"}
ACTION_DISPLAY_LABEL = {"买入": "买入", "换仓": "换仓", "卖出": "卖出", "持有": "持有", "观望": "空仓"}
# rotation-v1 JSON 没配 DB 名称查询时，currentHolding.name 会退化成 symbol 本身，
# 推送里就看不出持有的到底是黄金 ETF 还是别的。候选池固定且很小，这里兜底一份中文名。
KNOWN_SYMBOL_NAMES = {
    "159915.SZ": "创业板ETF",
    "513100.SS": "国泰纳斯达克100ETF",
    "513100.SH": "国泰纳斯达克100ETF",
    "518880.SS": "华安易富黄金ETF",
    "518880.SH": "华安易富黄金ETF",
}


@dataclass(frozen=True)
class ReportRecord:
    source_path: Path
    raw: dict
    action: str
    symbol: str
    name: str


@dataclass(frozen=True)
class DigestItem:
    """标的/策略在日报里的统一展示单元，单标的和动量轮动策略共用同一套渲染逻辑。"""

    key: str
    name: str
    action: str
    overview_suffix: str
    detail_lines: tuple[str, ...]


def today_in_timezone(timezone_name: str = DEFAULT_TIMEZONE) -> date:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(timezone_name)).date()


def normalize_action(action: str | None) -> str:
    if not action:
        return "观望"
    action = str(action).strip()
    return ACTION_ALIASES.get(action.upper(), ACTION_ALIASES.get(action, action))


def load_reports(data_dir: Path, target_date: date) -> tuple[list[ReportRecord], list[str], str | None]:
    if not data_dir.exists():
        return [], [f"数据目录不存在: {data_dir}"], "missing_dir"

    json_files = sorted(data_dir.glob("*.json"))
    if not json_files:
        return [], [], "no_json"

    warnings: list[str] = []
    reports: list[ReportRecord] = []
    target_date_str = target_date.isoformat()

    for json_file in json_files:
        try:
            raw = json.loads(json_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            warnings.append(f"跳过损坏 JSON: {json_file} ({exc})")
            continue

        if not isinstance(raw, dict):
            warnings.append(f"跳过非对象 JSON: {json_file}")
            continue

        report_date = raw.get("reportDate")
        if report_date != target_date_str:
            continue

        symbol = str(raw.get("symbol") or "").strip()
        latest_signal = raw.get("latestSignal")

        if not symbol or not isinstance(latest_signal, dict):
            warnings.append(f"跳过缺少关键字段的 JSON: {json_file}")
            continue

        action = normalize_action(latest_signal.get("action"))
        name = str(raw.get("name") or symbol).strip() or symbol

        reports.append(
            ReportRecord(
                source_path=json_file,
                raw=raw,
                action=action,
                symbol=symbol,
                name=name,
            )
        )

    reason = None if reports else "no_matching_reports"
    reports.sort(key=lambda item: (ACTION_PRIORITY.get(item.action, 99), item.symbol))
    return reports, warnings, reason


def load_rotation_reports(data_dir: Path, target_date: date) -> tuple[list[dict], list[str]]:
    """加载动量轮动策略报告（rotation-v1 schema，位于 data_dir/rotation/*.json）。"""
    rotation_dir = data_dir / "rotation"
    if not rotation_dir.exists():
        return [], []

    json_files = sorted(rotation_dir.glob("*.json"))
    warnings: list[str] = []
    reports: list[dict] = []
    target_date_str = target_date.isoformat()

    for json_file in json_files:
        try:
            raw = json.loads(json_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            warnings.append(f"跳过损坏的轮动 JSON: {json_file} ({exc})")
            continue

        if not isinstance(raw, dict):
            warnings.append(f"跳过非对象轮动 JSON: {json_file}")
            continue

        if raw.get("reportDate") != target_date_str:
            continue

        if not raw.get("strategyId"):
            warnings.append(f"跳过缺少 strategyId 的轮动 JSON: {json_file}")
            continue

        reports.append(raw)

    reports.sort(key=lambda item: str(item.get("strategyId") or ""))
    return reports, warnings


def extract_latest_price(report: dict, fallback_action: str | None = None) -> tuple[str, str]:
    latest_signal = report.get("latestSignal") or {}
    timestamp = format_trade_date(latest_signal.get("timestamp"))
    prices = latest_signal.get("prices") or {}
    close_price = prices.get("close")
    if close_price is not None:
        return timestamp, format_price(close_price)

    if fallback_action:
        trade = find_latest_trade(report.get("recentTrades"), fallback_action)
        if trade:
            return format_trade_date(trade.get("date")), format_price(trade.get("price"))

    return timestamp, "暂无"


def find_latest_trade(trades: Iterable[dict] | None, action: str) -> dict | None:
    if not trades:
        return None

    normalized_action = action.upper()
    latest_trade = None
    latest_trade_dt = None

    for trade in trades:
        if not isinstance(trade, dict):
            continue
        trade_action = str(trade.get("action") or "").upper()
        if trade_action != normalized_action:
            continue
        trade_date = parse_trade_datetime(trade.get("date"))
        if latest_trade is None or trade_date >= latest_trade_dt:
            latest_trade = trade
            latest_trade_dt = trade_date

    return latest_trade


def find_matching_buy_for_latest_sell(trades: Iterable[dict] | None) -> dict | None:
    if not trades:
        return None

    latest_sell = None
    latest_sell_dt = None

    for trade in trades:
        if not isinstance(trade, dict):
            continue
        if str(trade.get("action") or "").upper() != "SELL":
            continue
        trade_dt = parse_trade_datetime(trade.get("date"))
        if latest_sell is None or trade_dt >= latest_sell_dt:
            latest_sell = trade
            latest_sell_dt = trade_dt

    if latest_sell is None:
        return None

    matching_buy = None
    matching_buy_dt = None
    for trade in trades:
        if not isinstance(trade, dict):
            continue
        if str(trade.get("action") or "").upper() != "BUY":
            continue
        trade_dt = parse_trade_datetime(trade.get("date"))
        if trade_dt <= latest_sell_dt and (matching_buy is None or trade_dt >= matching_buy_dt):
            matching_buy = trade
            matching_buy_dt = trade_dt

    return matching_buy


def resolve_symbol_name(symbol: str, raw_name: str | None = None) -> str:
    if raw_name and raw_name != symbol:
        return raw_name
    return KNOWN_SYMBOL_NAMES.get(symbol, symbol)


def base_strategy_name(strategy_name: str) -> str:
    """策略名常年带着完整候选池，比如"动量轮动 Top-1（创业板/纳指/黄金）"——只保留前缀，
    候选池是固定配置，不是"现在持有什么"的答案，放进总览一行会太长又没信息量。"""
    for sep in ("（", "("):
        if sep in strategy_name:
            return strategy_name.split(sep, 1)[0].strip() or strategy_name
    return strategy_name


def format_pnl_label(value: object) -> str:
    """返回如 '浮盈34.48%' / '浮亏3.20%' / '暂无' 的简短盈亏标签。"""
    if value is None or value == "":
        return "暂无"
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return str(value)
    label = "浮盈" if pct >= 0 else "浮亏"
    return f"{label}{abs(pct):.2f}%"


def build_symbol_item(record: ReportRecord) -> DigestItem:
    raw = record.raw
    action = record.action
    overview_suffix = ""
    detail_lines: list[str] = []

    if action == "买入":
        signal_time, signal_price = extract_latest_price(raw, "BUY")
        detail_lines.append(f"买入信号：{signal_time}，价格：{signal_price}")

    elif action == "卖出":
        trades = raw.get("recentTrades")
        matching_buy = find_matching_buy_for_latest_sell(trades)
        sell_trade = find_latest_trade(trades, "SELL")
        sell_time, sell_price = extract_latest_price(raw, "SELL")
        trigger_reason = str(sell_trade.get("reason") or "暂无") if sell_trade else "暂无"

        if matching_buy:
            detail_lines.append(
                f"买入信号：{format_trade_date(matching_buy.get('date'))}，价格：{format_price(matching_buy.get('price'))}"
            )
        else:
            detail_lines.append("买入信号：暂无，价格：暂无")

        if sell_trade and raw.get("latestSignal", {}).get("prices", {}).get("close") is None:
            sell_time = format_trade_date(sell_trade.get("date"))
            sell_price = format_price(sell_trade.get("price"))

        detail_lines.append(f"卖出信号：{sell_time}，价格：{sell_price}")
        detail_lines.append(f"触发条件：{trigger_reason}")

    elif action == "持有":
        position_info = raw.get("positionInfo") or {}
        entry_date = format_trade_date(position_info.get("entry_date"))
        entry_price = format_price(position_info.get("entry_price"))
        pnl_label = format_pnl_label(position_info.get("unrealized_pnl_pct"))
        detail_lines.append(f"买入信号：{entry_date}，价格：{entry_price}")
        detail_lines.append(f"浮动盈亏：{pnl_label}")
        overview_suffix = pnl_label

    else:
        latest_buy = find_latest_trade(raw.get("recentTrades"), "BUY")
        latest_sell = find_latest_trade(raw.get("recentTrades"), "SELL")
        if latest_buy:
            detail_lines.append(
                f"上次买入：{format_trade_date(latest_buy.get('date'))}，价格：{format_price(latest_buy.get('price'))}"
            )
        else:
            detail_lines.append("上次买入：暂无，价格：暂无")

        if latest_sell:
            detail_lines.append(
                f"上次卖出：{format_trade_date(latest_sell.get('date'))}，价格：{format_price(latest_sell.get('price'))}"
            )
        else:
            detail_lines.append("上次卖出：暂无，价格：暂无")

    return DigestItem(
        key=record.symbol,
        name=record.name,
        action=action,
        overview_suffix=overview_suffix,
        detail_lines=tuple(detail_lines),
    )


def build_rotation_item(raw: dict, target_date_str: str) -> DigestItem:
    strategy_id = str(raw.get("strategyId") or "").strip()
    strategy_name = str(raw.get("strategyName") or strategy_id).strip()
    base_name = base_strategy_name(strategy_name)
    holding = raw.get("currentHolding")
    rotations = raw.get("rotations") or []
    next_signal = raw.get("nextSignal") or {}

    exit_today = next((r for r in rotations if r.get("exitDate") == target_date_str), None)
    next_signal_action = str(next_signal.get("action") or "").upper()

    if holding and holding.get("since") == target_date_str:
        # 已经发生的买入：如果同一天还清出了旧仓位，说明是当天完成的换仓，而不是从空仓建仓
        action = "换仓" if exit_today else "买入"
    elif not holding:
        if exit_today:
            action = "卖出"
        elif next_signal_action == "ROTATE":
            # 尚未发生的建仓：nextSignal 算出该从空仓买入了，但交易还没实际执行
            action = "买入"
        else:
            action = "观望"
    elif next_signal_action == "ROTATE":
        # 换仓交易还没实际发生（这是开盘前生成的下一步计划），currentHolding 仍是旧持仓，
        # 不能因为 since != 今天就把它标成「持有」——那会让 PushGo 漏报换仓信号。
        action = "换仓"
    elif next_signal_action == "EXIT":
        action = "卖出"
    else:
        action = "持有"

    overview_suffix = ""
    detail_lines: list[str] = []
    display_name = f"{base_name} · 空仓"

    if holding:
        symbol = holding.get("symbol") or "暂无"
        held_name = resolve_symbol_name(symbol, holding.get("name"))
        label = symbol if held_name == symbol else f"{symbol}（{held_name}）"
        pnl_label = format_pnl_label(holding.get("unrealizedPnlPct"))
        detail_lines.append(
            f"当前持仓：{label}，入场：{format_trade_date(holding.get('since'))}，"
            f"入场价：{format_price(holding.get('entryPrice'))}，"
            f"当前价：{format_price(holding.get('currentPrice'))}"
        )
        detail_lines.append(f"浮动盈亏：{pnl_label}")
        overview_suffix = pnl_label
        display_name = f"{base_name} · {held_name}"
    elif action == "卖出" and exit_today:
        sold_symbol = exit_today.get("symbol") or "暂无"
        sold_name = resolve_symbol_name(sold_symbol)
        realized_label = format_pnl_label(exit_today.get("periodReturnPct"))
        detail_lines.append(
            f"卖出标的：{sold_symbol}（{sold_name}），"
            f"入场：{format_trade_date(exit_today.get('entryDate'))}，"
            f"价格：{format_price(exit_today.get('entryPrice'))}，"
            f"出场价：{format_price(exit_today.get('exitPrice'))}"
        )
        detail_lines.append(f"本段收益：{realized_label}")
        overview_suffix = realized_label
        display_name = f"{base_name} · 已清仓{sold_name}"
    else:
        detail_lines.append("当前持仓：空仓")

    next_action_label = ROTATION_ACTION_LABELS.get(str(next_signal.get("action") or "").upper(), "未知")
    reason = str(next_signal.get("reason") or "暂无")
    detail_lines.append(f"下次预期动作：{next_action_label}（{reason}）")

    return DigestItem(
        key=strategy_id,
        name=display_name,
        action=action,
        overview_suffix=overview_suffix,
        detail_lines=tuple(detail_lines),
    )


def build_digest(
    reports: list[ReportRecord],
    target_date: date,
    reason: str | None = None,
    rotation_reports: list[dict] | None = None,
) -> tuple[str, str]:
    rotation_reports = rotation_reports or []
    date_str = target_date.isoformat()
    if not reports and not rotation_reports:
        title = f"[{date_str}] 无任何数据可用"
        body = "\n".join(
            [
                "# 📊 交易信号日报",
                f"- 日期：{date_str}",
                "- 状态：无任何数据可用",
                "- 说明：未找到可用于汇总的当日 JSON 数据。",
            ]
        )
        return title, body

    items = [build_symbol_item(record) for record in reports]
    items.extend(build_rotation_item(raw, date_str) for raw in rotation_reports)
    items.sort(key=lambda item: (ACTION_PRIORITY.get(item.action, 99), item.key))

    counts = {
        label: sum(1 for item in items if item.action == label)
        for label in ("买入", "换仓", "卖出", "持有", "观望")
    }
    trade_signal_count = counts["买入"] + counts["换仓"] + counts["卖出"]
    title = (
        f"📊 [{date_str}] {trade_signal_count}个交易信号"
        if trade_signal_count
        else f"📊 [{date_str}] 无交易信号"
    )

    lines = [
        "# 📊 交易信号日报",
        f"📅 {date_str}",
        f"{ACTION_EMOJI['买入']} 买入 {counts['买入']} ｜ "
        f"{ACTION_EMOJI['换仓']} 换仓 {counts['换仓']} ｜ "
        f"{ACTION_EMOJI['卖出']} 卖出 {counts['卖出']} ｜ "
        f"{ACTION_EMOJI['持有']} 持有 {counts['持有']} ｜ "
        f"{ACTION_EMOJI['观望']} 空仓 {counts['观望']}",
        "",
        "## 🎯 信号总览",
    ]
    for item in items:
        suffix = f" {item.overview_suffix}" if item.overview_suffix else ""
        lines.append(
            f"- {ACTION_EMOJI[item.action]} {item.name}（{item.key}）"
            f"{ACTION_DISPLAY_LABEL[item.action]}{suffix}"
        )

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 📌 详情")
    for index, item in enumerate(items):
        if index > 0:
            lines.append("")
            lines.append("---")
        lines.append("")
        lines.append(f"### {ACTION_EMOJI[item.action]} {item.name}（{item.key}）· {ACTION_DISPLAY_LABEL[item.action]}")
        lines.extend(f"- {detail_line}" for detail_line in item.detail_lines)

    return title, "\n".join(lines)


def format_price(value: object) -> str:
    if value is None or value == "":
        return "暂无"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return str(value)


def format_trade_date(value: object) -> str:
    """只保留日期，不要时分秒——JSON 里的时间戳基本都是同一个开盘时刻，没有信息量，
    只会让推送里的每一行变长、对不齐。"""
    if not value:
        return "暂无"
    text = str(value).strip()
    if not text:
        return "暂无"
    return text.split(" ", 1)[0].split("T", 1)[0]


def parse_trade_datetime(value: object) -> datetime:
    if not value:
        return datetime.min
    if isinstance(value, datetime):
        return value
    value_str = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value_str, fmt)
        except ValueError:
            continue
    return datetime.min


def build_email_subject(
    title: str,
    reports: list[ReportRecord],
    target_date: date,
    rotation_reports: list[dict] | None = None,
    max_names: int = 3,
) -> str:
    """邮件主题：日报标题（含日期）+ 有交易信号的标的/策略名（最多 max_names 个）。"""
    items = [build_symbol_item(record) for record in reports]
    items.extend(build_rotation_item(raw, target_date.isoformat()) for raw in rotation_reports or [])
    items.sort(key=lambda item: (ACTION_PRIORITY.get(item.action, 99), item.key))
    names = [item.name for item in items if item.action in TRADE_ACTIONS]
    if not names:
        return title
    shown = "、".join(names[:max_names])
    more = f" 等{len(names)}个" if len(names) > max_names else ""
    return f"{title} {shown}{more}"


def send_pushgo_notification(
    title: str,
    body: str,
    channel_id: str,
    password: str,
    pushgo_url: str,
    max_retries: int,
    retry_delay: int,
    urlopen: Callable[..., object] = request.urlopen,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict:
    payload = json.dumps(
        {
            "channel_id": channel_id,
            "password": password,
            "title": title,
            "body": body,
        }
    ).encode("utf-8")

    last_error: Exception | None = None

    def redact(text: object) -> str:
        # 异常文本可能回显凭据，写日志/异常前先抹掉
        result = str(text)
        for secret in (password, channel_id):
            if secret:
                result = result.replace(secret, "***")
        return result

    for attempt in range(1, max_retries + 1):
        req = request.Request(
            pushgo_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=15) as response:
                response_body = response.read().decode("utf-8")
                status_code = getattr(response, "status", response.getcode())
                if status_code >= 400:
                    raise RuntimeError(f"PushGo 返回状态码 {status_code}: {response_body}")
                parsed_body = json.loads(response_body)
                if not parsed_body.get("success", False):
                    raise RuntimeError(f"PushGo 返回失败: {response_body}")
                return parsed_body
        except (error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            print(
                f"[WARN] PushGo 推送失败，第 {attempt}/{max_retries} 次: {redact(exc)}",
                file=sys.stderr,
            )
            if attempt < max_retries:
                sleep_fn(retry_delay)

    raise RuntimeError(f"PushGo 推送失败，已重试 {max_retries} 次: {redact(last_error)}")
