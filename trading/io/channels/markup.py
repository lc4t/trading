"""Markdown 转义与 Markdown→HTML（只覆盖日报用到的子集）。

- `md_escape`：把用户数据（标的名、原因文本等）写进 Markdown 前先转义，防止被解析成链接/图片/HTML/强调。
  Gotify 官方文档提醒：text/markdown 会自动下载 `![](url)` 图片，来源不可信的文本可被注入。
- `markdown_to_html`：日报 Markdown 子集（# 标题、---、- 列表、1. 列表、> 引用、**粗体**、段落）→ 内联样式 HTML。
  所有文本都经 `html.escape`，不允许原样输出 HTML；不支持图片与链接语法（按字面显示）。
"""

from __future__ import annotations

import html
import re

_MD_SPECIAL = set("\\`*_[]<~")
_ESCAPABLE = set("\\`*_[]<>~!#()-.|&+{}")
_ORDERED_RE = re.compile(r"^(\d+)\.\s+(.*)$")


def md_escape(text: object) -> str:
    """转义会触发 Markdown 语法的字符，并把换行压成空格（一条用户数据只占一行）。"""
    flat = " ".join(str(text).split())
    return "".join(f"\\{ch}" if ch in _MD_SPECIAL else ch for ch in flat)


def _inline(text: str) -> str:
    """行内：反斜杠转义 + **粗体**；其余一律按字面输出（HTML 转义）。"""
    out: list[str] = []
    bold = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] in _ESCAPABLE:
            out.append(html.escape(text[i + 1]))
            i += 2
        elif text.startswith("**", i):
            out.append("</strong>" if bold else "<strong>")
            bold = not bold
            i += 2
        else:
            out.append(html.escape(ch))
            i += 1
    if bold:  # 未闭合的 ** 补齐，保证标签平衡
        out.append("</strong>")
    return "".join(out)


STYLES = {
    "h1": "font-size:20px;line-height:1.3;margin:0 0 8px;",
    "h2": "font-size:17px;line-height:1.3;margin:20px 0 8px;",
    "h3": "font-size:15px;line-height:1.35;margin:16px 0 6px;",
    "p": "margin:6px 0;",
    "ul": "margin:6px 0;padding-left:20px;",
    "li": "margin:3px 0;",
    "hr": "border:0;border-top:1px solid #d0d7de;margin:16px 0;",
    "blockquote": "margin:8px 0;padding:4px 12px;border-left:3px solid #d0d7de;",
}


def _tag(name: str, content: str) -> str:
    return f'<{name} style="{STYLES[name]}">{content}</{name}>'


def markdown_to_html(markdown: str, title: str = "") -> str:
    blocks: list[str] = []
    list_kind: str | None = None
    items: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(_tag("p", "<br>".join(_inline(line) for line in paragraph)))
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_kind
        if list_kind:
            body = "".join(_tag("li", item) for item in items)
            blocks.append(f'<{list_kind} style="{STYLES["ul"]}">{body}</{list_kind}>')
            items.clear()
            list_kind = None

    for raw in markdown.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        ordered = _ORDERED_RE.match(stripped)
        if not stripped:
            flush_paragraph()
            flush_list()
        elif stripped == "---":
            flush_paragraph()
            flush_list()
            blocks.append(f'<hr style="{STYLES["hr"]}">')
        elif stripped.startswith("#"):
            flush_paragraph()
            flush_list()
            level = len(stripped) - len(stripped.lstrip("#"))
            text = stripped[level:].strip()
            if 1 <= level <= 6 and text:
                blocks.append(_tag(f"h{min(level, 3)}", _inline(text)))
            else:
                paragraph.append(stripped)
        elif stripped.startswith("- "):
            flush_paragraph()
            if list_kind != "ul":
                flush_list()
                list_kind = "ul"
            items.append(_inline(stripped[2:].strip()))
        elif ordered:
            flush_paragraph()
            if list_kind != "ol":
                flush_list()
                list_kind = "ol"
            items.append(_inline(ordered.group(2).strip()))
        elif stripped.startswith(">"):
            flush_paragraph()
            flush_list()
            blocks.append(_tag("blockquote", _inline(stripped.lstrip(">").strip())))
        else:
            flush_list()
            paragraph.append(stripped)
    flush_paragraph()
    flush_list()

    content = "\n".join(blocks)
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="supported-color-schemes" content="light dark">'
        f"<title>{html.escape(title)}</title>"
        "<style>"
        ":root{color-scheme:light dark}"
        "@media (prefers-color-scheme:dark){"
        "body,.wrap{background:#111418!important;color:#e6e8eb!important}"
        "hr{border-top-color:#3a4048!important}"
        "blockquote{border-left-color:#3a4048!important}}"
        "</style></head>"
        '<body style="margin:0;padding:0;background:#ffffff;color:#1f2328;">'
        '<div class="wrap" style="max-width:640px;margin:0 auto;padding:16px;'
        "background:#ffffff;color:#1f2328;font-family:-apple-system,BlinkMacSystemFont,"
        "'Segoe UI','PingFang SC','Microsoft YaHei',Roboto,Helvetica,Arial,sans-serif;"
        'font-size:15px;line-height:1.55;word-break:break-word;">\n'
        f"{content}\n</div></body></html>\n"
    )
