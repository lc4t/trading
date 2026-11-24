"""邮件版日报：同一份视图（digest_view）渲染成 HTML 与 Markdown 文本，细节比 iGotify 更全。

HTML 学策引邮件的视觉结构：顶部日期条 → 黄色警示框（结论 / 数据截至 / 警示 / 关键指标）→ 买入绿、卖出红、换仓橙、
持有蓝、空仓灰的分区条 → 标的小卡片 → 完整细节（交易记录、回测、年度收益、参数）。
全部内联样式、div 布局（不用表格，窄屏不会被挤），深色模式靠 prefers-color-scheme 覆盖；所有数据先 html.escape。
"""

from __future__ import annotations

import datetime as dt
import html
from dataclasses import replace
from datetime import date
from typing import Iterable

from .channels.chunking import render_full
from .channels.markup import md_escape
from .digest import ReportRecord
from .digest_push import PUSH_BOUNDARY, build_push_parts_from_view
from .digest_view import EMPTY_TEXT, ZONE_EMOJI, ZONE_TITLE, ZONES, Card, View, build_view

ZONE_COLORS = {  # 分区条渐变色
    "买入": ("#6bbf7a", "#4f9d63"),
    "卖出": ("#e57373", "#d0453f"),
    "换仓": ("#f0a64a", "#e08a1e"),
    "持有": ("#5b9bdb", "#4a82c4"),
    "空仓": ("#9aa6b8", "#727f96"),
}
ROW_ICONS = (
    ("信号日期", "📅"),
    ("最近离场", "📅"),
    ("入场", "📅"),
    ("收益", "💰"),
    ("离场收益", "💰"),
    ("现价", "💹"),
    ("止损", "🛑"),
    ("状态", "🧭"),
    ("当前持有", "📌"),
    ("本期结论", "📌"),
)


def _x(value: object) -> str:
    return html.escape(str(value), quote=True)


def _icon(text: str) -> str:
    for key, icon in ROW_ICONS:
        if text.startswith(key):
            return icon
    return "▫️"


def _row_html(row: tuple[str, ...]) -> str:
    text = " ｜ ".join(_x(field) for field in row)
    return f'<div style="margin:3px 0;">{_icon(row[0])} {text}</div>'


def _card_html(card: Card, detail: bool) -> str:
    parts = [
        '<div class="card" style="padding:14px 16px;border-top:1px solid #e6e9f0;">',
        f'<div style="font-size:17px;font-weight:700;margin-bottom:8px;">{_x(card.title)}</div>',
        '<div class="inner" style="background:#f6f7fb;border-radius:10px;padding:10px 12px;font-size:14px;line-height:1.55;">',
    ]
    parts += [_row_html(row) for row in card.rows]
    parts += [
        f'<div style="margin:3px 0;color:#b26a00;">⚠️ {_x(flag)}</div>' for flag in card.flags
    ]
    if card.ranking:
        parts.append(
            '<div style="margin:6px 0 2px;font-weight:600;">📊 动量排名（✅ 为目标持仓）</div>'
        )
        parts += [
            f'<div style="margin:2px 0 2px 18px;">{rank}. {_x(name)} ｜ {_x(mom)}{" ✅" if chosen else ""}</div>'
            for rank, name, mom, chosen in card.ranking
        ]
    if card.why:
        parts.append(f'<div style="margin:6px 0 2px;">🧭 <b>为什么：</b>{_x(card.why)}</div>')
    parts.append("</div>")
    if detail and card.extra_rows:
        parts.append(
            '<div class="muted" style="margin-top:8px;font-size:12.5px;line-height:1.55;color:#6b7280;">'
        )
        parts += [
            f'<div style="margin:2px 0;">{" ｜ ".join(_x(f) for f in row)}</div>'
            for row in card.extra_rows
        ]
        parts.append("</div>")
    parts.append("</div>")
    return "".join(parts)


def _zone_html(view: View, zone: str) -> str:
    cards = view.zones[zone]
    top, bottom = ZONE_COLORS[zone]
    bar = (
        f'<div class="zbar" style="background:{top};background:linear-gradient(135deg,{top},{bottom});color:#ffffff;'
        'text-align:center;font-weight:700;font-size:16px;padding:12px 10px;">'
        f"{ZONE_EMOJI[zone]} {_x(ZONE_TITLE[zone])}（{len(cards)}）</div>"
    )
    if cards:
        body = "".join(_card_html(card, detail=True) for card in cards)
    else:
        body = f'<div style="padding:20px 16px;text-align:center;font-style:italic;color:#9aa1ad;">{_x(EMPTY_TEXT[zone])}</div>'
    return (
        '<div class="zone" style="background:#ffffff;border:1px solid #e3e7ef;border-radius:12px;overflow:hidden;margin:14px 0;">'
        f"{bar}{body}</div>"
    )


def _warn_box(view: View) -> str:
    n = len(view.actions)
    headline = f"今天有 {n} 项需要操作" if n else "今天无需操作"
    rows = [
        f'<div style="font-size:18px;font-weight:800;color:#7a4b00;line-height:1.4;">🟡 {_x(headline)}</div>'
    ]
    for emoji, text in view.actions:
        rows.append(
            f'<div style="margin:6px 0 0;font-size:15px;color:#5b3a00;">{emoji} {_x(text)}</div>'
        )
    stamp = f"数据截至 {view.latest or '—'}" + (
        f" ｜ 生成于 {view.generated_at}" if view.generated_at else ""
    )
    rows.append(
        f'<div class="muted" style="margin-top:8px;font-size:13px;color:#6b7280;">{_x(stamp)}</div>'
    )
    for warning in view.warnings_full:  # 邮件版名字列全
        rows.append(
            '<div class="wline" style="margin-top:8px;background:#ffffff;border-radius:8px;padding:8px 10px;font-size:13.5px;color:#7a4b00;">'
            f"⚠️ {_x(warning)}</div>"
        )
    for line in view.metrics_lines:
        name, *fields = line
        boxes = "".join(
            '<span class="mbox" style="display:inline-block;background:#ffffff;border-radius:8px;padding:6px 10px;margin:4px 6px 0 0;font-size:13px;">'
            f"{_x(field)}</span>"
            for field in fields
        )
        rows.append(
            f'<div style="margin-top:10px;font-size:13px;color:#7a4b00;font-weight:600;">{_x(name)}</div><div>{boxes}</div>'
        )
    return (
        '<div class="warn" style="background:#fffdf0;border:2px solid #e6c84b;border-radius:14px;padding:14px 16px;margin:14px 0;">'
        + "".join(rows)
        + "</div>"
    )


def build_email_html(view: View, title: str = "") -> str:
    zones = "".join(_zone_html(view, zone) for zone in ZONES)
    boundary = f'<div class="muted" style="margin:16px 0 4px;font-size:12.5px;color:#6b7280;text-align:center;">{_x(PUSH_BOUNDARY)}</div>'
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">'
        f"<title>{_x(title or view.label)}</title>"
        "<style>:root{color-scheme:light dark}"
        "@media (prefers-color-scheme:dark){"
        "body{background:#0f1218!important;color:#e6e8eb!important}"
        ".zone,.card{background:#1a1e26!important;border-color:#2c323d!important}"
        ".inner,.mbox,.wline{background:#242a35!important;color:#e6e8eb!important}"
        ".warn{background:#2a2410!important;border-color:#8a6d1c!important}"
        ".warn div{color:#f3d98a!important}.muted{color:#9aa3b2!important}}"
        "</style></head>"
        '<body style="margin:0;padding:0;background:#eef1f7;color:#1f2328;">'
        "<div style=\"max-width:640px;margin:0 auto;padding:12px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
        "'PingFang SC','Microsoft YaHei',Roboto,Helvetica,Arial,sans-serif;font-size:15px;line-height:1.55;word-break:break-word;\">"
        '<div style="background:#4a90d9;background:linear-gradient(135deg,#5b9bdb,#4a82c4);color:#ffffff;text-align:center;'
        'border-radius:14px;padding:22px 12px;"><div style="font-size:14px;opacity:.9;">📈 交易信号日报</div>'
        f'<div style="font-size:30px;font-weight:800;margin-top:4px;">{_x(view.report_date)}</div></div>'
        f"{_warn_box(view)}"
        '<div style="font-size:20px;font-weight:800;margin:18px 4px 4px;">🎯 交易信号</div>'
        f"{zones}{boundary}</div></body></html>\n"
    )


def build_email_markdown(view: View) -> str:
    """邮件的纯文本版：与 iGotify 同一套分区卡片，再附完整细节。"""
    base = render_full(
        build_push_parts_from_view(replace(view, warnings=view.warnings_full))
    ).rstrip("\n")
    lines: list[str] = ["", "", "## 完整细节"]
    for zone in ZONES:
        for card in view.zones[zone]:
            if card.extra_rows:
                lines += ["", f"**{md_escape(card.title)}**"] + [
                    f"- {md_escape('｜'.join(row))}" for row in card.extra_rows
                ]
    return base + "\n".join(lines) + "\n"


def build_email(
    reports: list[ReportRecord],
    target_date: date,
    rotation_reports: list[dict] | None = None,
    warnings: Iterable[str] = (),
    generated_at: str | None = None,
    subject: str = "",
    now: dt.datetime | None = None,
) -> tuple[str, str]:
    """返回 (Markdown 文本, HTML)。"""
    view = build_view(
        reports, target_date, rotation_reports, warnings, generated_at, email_hint=False, now=now
    )
    return build_email_markdown(view), build_email_html(view, subject)
