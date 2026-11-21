"""把日报 Markdown 按 UTF-8 字节数分段（iGotify 经 APNs 推送，单条有大小上限）。

实测（VPS 会话）：3307 字节可推送，3468 字节触发 RSA 加密报错，上限在两者之间、具体值未探测，
所以默认保守取 3000 字节，可用 IGOTIFY_MAX_BYTES 覆盖。

切分原则（不丢任何内容）：
- 第 1 条固定放「摘要块」（标题、结论、状态总表）；总表放不下时依次换更精简的列组合；仍放不下才顺延到后面。
- 其余内容按「单元」（一个标的的细节、一个小节）整块装箱；单元之间保留 `---` 分隔。
- 单元放不下时按块切，块放不下时按行切（表格行、列表项、标题都不会被切断；表格在续段重复表头）；
  续段开头加「（续）标题」。
- 只有单行本身就超过上限（现实中不会发生）才按字符切，仍保证 UTF-8 不被截断、内容不丢。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SEP = "\n\n"
RULE = "---"
MIN_BUDGET = 400


def byte_len(text: str) -> int:
    """UTF-8 字节数（中文 3 字节，emoji 4 字节），不是字符数。"""
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class Block:
    """一个不应在中间切断的最小排版块（一个标题+列表，或一张表格）。

    lines[:body_start] 是必须和第一部分在一起的前缀（标题、表头）；超长时只对其后的行切分，
    `head` 是续段要重复的行（表格的表头与分隔行）。alternatives 是更精简的替代版本（仅用于第 1 条）。
    """

    lines: tuple[str, ...]
    body_start: int = 0
    head: tuple[str, ...] = ()
    alternatives: tuple[tuple[str, ...], ...] = ()
    grouped: bool = (
        False  # True：body 里用空行隔开的若干行组（如一个标的的 2~3 行）整组切分，不拆开
    )

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass(frozen=True)
class Unit:
    label: str  # 续段标题用
    blocks: tuple[Block, ...]
    rule_before: bool = False  # 与上一个单元之间是否画 ---
    break_before: bool = False  # True：从新的一条消息开始（用于固定「结论 / 总览 / 详情」各占一条）


@dataclass(frozen=True)
class DigestParts:
    label: str  # 例如 "trading 日报 2026-03-10"
    headline: str  # 例如 "换仓 1｜买入 1"，给第 1 条标题
    summary: tuple[Block, ...]
    units: tuple[Unit, ...]


def render_full(parts: DigestParts) -> str:
    """不分段的完整 Markdown（邮件用）。"""
    out = SEP.join(block.text for block in parts.summary)
    for unit in parts.units:
        joiner = f"{SEP}{RULE}{SEP}" if unit.rule_before else SEP
        out += joiner + SEP.join(block.text for block in unit.blocks)
    return out + "\n"


def parts_from_markdown(markdown: str, label: str = "", headline: str = "") -> DigestParts:
    """没有结构信息时的通用切法：按空行分块，表格识别出表头；整体作为一个单元。"""
    blocks: list[Block] = []
    for raw in re.split(r"\n\s*\n", markdown.strip("\n")):
        lines = tuple(raw.split("\n"))
        is_table = (
            len(lines) >= 3
            and lines[0].lstrip().startswith("|")
            and set(lines[1].replace("|", "").strip()) <= set("-: ")
        )
        blocks.append(Block(lines, body_start=2, head=lines[:2]) if is_table else Block(lines))
    return DigestParts(label, headline, (), (Unit("", tuple(blocks)),))


def _hard_split(line: str, limit: int) -> list[str]:
    """最后手段：单行超限时按字符切，保证每段 ≤ limit 字节且不截断多字节字符。"""
    parts, cur, size = [], [], 0
    for ch in line:
        width = byte_len(ch)
        if cur and size + width > limit:
            parts.append("".join(cur))
            cur, size = [], 0
        cur.append(ch)
        size += width
    if cur:
        parts.append("".join(cur))
    return parts


def _rows_of(block: Block) -> tuple[list[str], str]:
    body = list(block.lines[block.body_start :])
    if not block.grouped:
        return body, "\n"
    groups: list[list[str]] = [[]]
    for line in body:
        if line == "":
            if groups[-1]:
                groups.append([])
        else:
            groups[-1].append(line)
    return ["\n".join(g) for g in groups if g], "\n\n"


def _chunk_text(prefix: list[str], psep: str, rows: list[str], row_sep: str) -> str:
    lead = "\n".join(prefix)
    body = row_sep.join(rows)
    if not rows:
        return lead
    return f"{lead}{psep}{body}" if prefix else body


def _split_block(block: Block, room_first: int, room_rest: int) -> list[str]:
    prefix = list(block.lines[: block.body_start])
    while prefix and prefix[-1] == "":
        prefix.pop()
    psep = "\n\n" if block.body_start and block.lines[block.body_start - 1] == "" else "\n"
    rows, row_sep = _rows_of(block)
    head = list(block.head)
    hsep = "\n"
    chunks: list[str] = []
    cur_prefix, cur_psep, cur_rows, room = prefix, psep, [], room_first

    def text(extra: list[str] | None = None) -> str:
        return _chunk_text(cur_prefix, cur_psep, [*cur_rows, *(extra or [])], row_sep)

    def flush() -> None:
        nonlocal cur_prefix, cur_psep, cur_rows, room
        chunks.append(text())
        cur_prefix, cur_psep, cur_rows, room = head, hsep, [], room_rest

    for row in rows:
        if byte_len(text([row])) <= room:
            cur_rows.append(row)
            continue
        if cur_rows:
            flush()
            if byte_len(text([row])) <= room:
                cur_rows.append(row)
                continue
        # 一行（或一组）本身就放不下：按字符切（不会发生在正常日报里）
        space = max(
            min(room_first, room_rest)
            - byte_len(_chunk_text(cur_prefix, cur_psep, [], row_sep))
            - 2,
            50,
        )
        pieces = _hard_split(row, space)
        for piece in pieces[:-1]:
            cur_rows.append(piece)
            flush()
        cur_rows.append(pieces[-1])
    if cur_rows or not chunks:
        chunks.append(text())
    return chunks


def _split_unit(unit: Unit, budget: int) -> list[str]:
    header = f"**（续）{unit.label}**" if unit.label else "**（续）**"
    pieces: list[str] = []
    cur: list[str] = []

    def size(items: list[str]) -> int:
        return byte_len(SEP.join(items))

    def start() -> list[str]:
        return [header] if pieces else []

    def close() -> None:
        nonlocal cur
        if cur and cur != [header]:
            pieces.append(SEP.join(cur))
        cur = []

    for block in unit.blocks:
        text = block.text
        if size([*cur, text]) <= budget:
            cur.append(text)
            continue
        close()
        cur = start()
        if size([*cur, text]) <= budget:
            cur.append(text)
            continue
        close()  # 整块放不下：从新的一条开始，按行切
        reserve = byte_len(header) + len(SEP)
        first_room = budget - (reserve if pieces else 0)
        chunks = _split_block(block, first_room, budget - reserve)
        for index, chunk in enumerate(chunks):
            if index:
                close()
            cur = [*start(), chunk]
    close()
    return pieces


def pack(parts: DigestParts, budget: int) -> list[str]:
    """分成若干条正文，每条 ≤ budget 字节；拼起来不丢内容。第 1 条只放摘要块。"""
    budget = max(budget, MIN_BUDGET)
    whole = render_full(parts).rstrip("\n")
    if byte_len(whole) <= budget and not any(u.break_before for u in parts.units):
        return [whole]  # 整份放得下就只发一条
    first: list[str] = []
    spill: tuple[Block, ...] = ()
    for index, block in enumerate(parts.summary):
        for variant in (block.lines, *block.alternatives):
            text = "\n".join(variant)
            if byte_len(SEP.join([*first, text])) <= budget:
                first.append(text)
                break
        else:
            spill = parts.summary[index:]
            break

    messages: list[str] = [SEP.join(first)] if first else []
    units = list(parts.units)
    if spill:
        units.insert(0, Unit("概览", spill))

    cur: list[str] = []

    def flush() -> None:
        if cur:
            messages.append(SEP.join(cur))
            cur.clear()

    for unit in units:
        if unit.break_before:
            flush()
        text = SEP.join(block.text for block in unit.blocks)
        extra = [RULE, text] if (cur and unit.rule_before) else [text]
        if byte_len(SEP.join([*cur, *extra])) <= budget:
            cur.extend(extra)
            continue
        flush()
        if byte_len(text) <= budget:
            cur.append(text)
            continue
        pieces = _split_unit(unit, budget)
        messages.extend(pieces[:-1])
        cur.append(pieces[-1])
    flush()
    return messages
