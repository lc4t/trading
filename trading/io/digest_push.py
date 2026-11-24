"""手机推送（iGotify）版日报：按信号类型分区的 Markdown 卡片，内容与邮件同源（见 digest_view）。

版式（学策引邮件的结构，用 Markdown 重新设计）：
    > 顶部引用块：第一行结论「今天有 N 项需要操作」/「今天无需操作」，其后「数据截至｜生成于」、⚠️ 警示行、策略级关键指标
    ## 🟢 买入信号（n） / 🔴 卖出信号 / 🔄 换仓信号 / 🔵 持有状态 / ⚪ 空仓状态 —— 固定顺序；为空的区也写一行
        「当前无新增买入信号」之类，让读者确定「没有」而不是「漏了」
    每个标的一张卡片：**名称（代码）** + 若干短字段行 + 「为什么」整句理由；字段缺失整行省略
    末尾：1 行边界声明；仅当已配置邮件时再加「完整信号与回测见邮件」
默认一份日报一条消息；超限时按区边界回退分段，买入/卖出/换仓三区与顶部摘要一定在第 1 段。

版式约束：只用 ## 小标题 + 引用块 + 列表（不用表格）；字段行 ≤22 个全角字符，超出自己拆行；代码一律「名称（代码）」。
"""

from __future__ import annotations

import datetime as dt
import re
from datetime import date
from typing import Iterable

from .channels.chunking import Block, DigestParts, Unit
from .channels.markup import md_escape
from .digest import ReportRecord
from .digest_markdown import LINE_WIDTH, display_width, wrap_fields
from .digest_view import EMPTY_TEXT, ZONE_EMOJI, ZONE_TITLE, ZONES, Card, View, build_view

PUSH_BOUNDARY = "仅为策略信号提示，非投资建议，不自动下单。"
EMAIL_HINT = "完整信号与回测见邮件。"
ACTION_ZONES = ("买入", "卖出", "换仓")  # 一定放在第 1 段


def _e(value: object) -> str:
    return md_escape(value)


_TOKEN_RE = re.compile(r"[A-Za-z0-9.\-+%/_:]+|\s+|.", re.S)
_NO_BREAK_BEFORE = set("（）()，。；：、％")
_NO_BREAK_AFTER = set("（(")


def wrap_text(text: str, limit: int = LINE_WIDTH) -> list[str]:
    """按显示宽度折行：英文数字串（含代码 518880.SS）不拆开，也不在「（」「）」与标点前断开，
    所以「名称（代码）」永远在同一行。"""
    lines: list[str] = []
    current, width = "", 0
    for token in _TOKEN_RE.findall(text):
        w = display_width(token)
        can_break = (
            token[0] not in _NO_BREAK_BEFORE
            and not token.isspace()
            and not (current and current[-1] in _NO_BREAK_AFTER)
        )
        if current and width + w > limit and can_break:
            lines.append(current.rstrip())
            current, width = "", 0
            if token.isspace():
                continue
        current += token
        width += w
    if current.strip():
        lines.append(current.rstrip())
    return lines


def _row_lines(row: tuple[str, ...], limit: int = LINE_WIDTH) -> list[str]:
    out: list[str] = []
    for line in wrap_fields(list(row), limit):
        out += wrap_text(line, limit) if display_width(line) > limit else [line]
    return out


def card_lines(card: Card) -> list[str]:
    """一张卡片：粗体标题行 + 列表项（每个字段行一项）。"""
    lines = [f"**{_e(card.title)}**"]
    for row in card.rows:
        lines += [f"- {_e(text)}" for text in _row_lines(row)]
    for flag in card.flags:
        flag_lines = _row_lines(tuple(flag.split("｜")), LINE_WIDTH - 3)  # 给行首「⚠️ 」留宽度
        lines += [f"- {'⚠️ ' if i == 0 else ''}{_e(text)}" for i, text in enumerate(flag_lines)]
    if card.ranking:
        lines.append("- 动量排名（✅ 为目标持仓）：")
        lines += [
            f"- {rank} {_e(name)} {_e(mom)}{' ✅' if chosen else ''}"
            for rank, name, mom, chosen in card.ranking
        ]
    if card.why:
        lines.append(f"- 为什么：{_e(card.why)}")
    return lines


def zone_block(view: View, zone: str) -> Block:
    cards = view.zones[zone]
    heading = f"## {ZONE_EMOJI[zone]} {ZONE_TITLE[zone]}（{len(cards)}）"
    if not cards:
        return Block((heading, "", EMPTY_TEXT[zone]), body_start=2, grouped=True)
    lines: list[str] = [heading, ""]
    for index, card in enumerate(cards):
        if index:
            lines.append("")
        lines += card_lines(card)
    return Block(tuple(lines), body_start=2, grouped=True)


def banner_block(view: View) -> Block:
    """顶部引用块：结论 / 数据截至 / ⚠️ 警示 / 策略级关键指标。每段独立成段，避免软换行被并成一行。"""
    paras: list[list[str]] = []
    if view.actions:
        paras.append(
            [
                f"**今天有 {len(view.actions)} 项需要操作**",
                *[f"- {emoji} {_e(text)}" for emoji, text in view.actions],
            ]
        )
    elif view.zones and not any(view.zones[z] for z in ZONES):
        paras.append(["**今天未取得数据**"])
    else:
        paras.append(["**今天无需操作**"])
    stamp = f"数据截至 {view.latest or '—'}" + (
        f"｜生成于 {view.generated_at}" if view.generated_at else ""
    )
    paras.append([_e(stamp)])
    paras += [[f"⚠️ {_e(w)}"] for w in view.warnings]
    paras += [[_e("｜".join(line))] for line in view.metrics_lines]
    lines: list[str] = []
    for index, para in enumerate(paras):
        if index:
            lines.append(">")
        lines += [f"> {line}" for line in para]
    return Block(tuple(lines))


def build_push_parts_from_view(view: View) -> DigestParts:
    summary = [banner_block(view)] + [zone_block(view, zone) for zone in ACTION_ZONES]
    units = [Unit(f"{ZONE_TITLE[zone]}", (zone_block(view, zone),)) for zone in ("持有", "空仓")]
    footer = [Block((PUSH_BOUNDARY,))] + ([Block((EMAIL_HINT,))] if view.email_hint else [])
    units.append(Unit("说明", tuple(footer)))
    return DigestParts(
        label=view.label,
        headline=view.headline,
        summary=tuple(summary),
        units=tuple(units),
        keep_summary_together=True,  # 买入/卖出/换仓三区与顶部摘要一定在第 1 段，宁可偏大也不拆开
    )


def build_push_parts(
    reports: list[ReportRecord],
    target_date: date,
    rotation_reports: list[dict] | None = None,
    warnings: Iterable[str] = (),
    generated_at: str | None = None,
    email_hint: bool = False,
    now: dt.datetime | None = None,
) -> DigestParts:
    view = build_view(
        reports, target_date, rotation_reports, warnings, generated_at, email_hint, now
    )
    return build_push_parts_from_view(view)
