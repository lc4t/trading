"""iGotify 渠道（服务端是 Gotify 协议）：POST {IGOTIFY_URL}/message。

环境变量：IGOTIFY_URL、IGOTIFY_TOKEN（应用令牌）、可选 IGOTIFY_PRIORITY（默认 5）、
IGOTIFY_ALLOW_HTTP=1（仅本地测试允许 http）。令牌放请求头 X-Gotify-Key，不进 URL。
"""

from __future__ import annotations

import time
from typing import Mapping
from urllib.parse import urlsplit

from .chunking import MIN_BUDGET, DigestParts, byte_len, pack, parts_from_markdown
from .common import ChannelError, Message, log, post_json

REQUIRED_ENV = ("IGOTIFY_URL", "IGOTIFY_TOKEN")
DEFAULT_PRIORITY = 5
# Gotify 官方：client::display.contentType = text/markdown 时客户端按 Markdown 渲染、忽略 HTML。
MARKDOWN_EXTRAS = {"client::display": {"contentType": "text/markdown"}}
# iGotify 经 APNs 推送，助手加密转发时有大小上限：实测 3307 字节成功、3468 字节 RSA 加密报错，
# 具体上限未探测，默认保守取 3000 字节（含标题），IGOTIFY_MAX_BYTES 可覆盖。超长日报按字节分段顺序发送。
DEFAULT_MAX_BYTES = 3000
SEGMENT_INTERVAL = 1.0  # 分段之间的间隔秒数


def normalize_url(raw: str, allow_http: bool) -> str:
    url = raw.strip().rstrip("/")
    if url.endswith("/message"):
        url = url[: -len("/message")].rstrip("/")
    parts = urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and allow_http):
        if parts.netloc and not parts.query and not parts.fragment:
            return url
    if parts.scheme == "http":
        raise ChannelError("iGotify 地址必须是 https（本地测试可设 IGOTIFY_ALLOW_HTTP=1）")
    raise ChannelError("iGotify 地址无效：需要 https://host[/path]，且不带查询串")


def parse_priority(raw: str | None) -> int:
    if raw is None or not raw.strip():
        return DEFAULT_PRIORITY
    try:
        return int(raw.strip())
    except ValueError:
        log("WARN", f"IGOTIFY_PRIORITY 不是整数，改用默认值 {DEFAULT_PRIORITY}")
        return DEFAULT_PRIORITY


def parse_max_bytes(raw: str | None) -> int:
    if raw is None or not raw.strip():
        return DEFAULT_MAX_BYTES
    try:
        value = int(raw.strip())
    except ValueError:
        value = 0
    if value < MIN_BUDGET:
        log(
            "WARN",
            f"IGOTIFY_MAX_BYTES 无效（需为不小于 {MIN_BUDGET} 的整数），改用默认值 {DEFAULT_MAX_BYTES}",
        )
        return DEFAULT_MAX_BYTES
    return value


def segment_titles(parts: DigestParts, count: int) -> list[str]:
    head = f"{parts.label} {parts.headline}".strip()
    return [
        f"{head} ({index}/{count})" if index == 1 else f"{parts.label} ({index}/{count})"
        for index in range(1, count + 1)
    ]


def plan_segments(message: Message, max_bytes: int) -> list[tuple[str, str]]:
    """返回 [(标题, 正文)]；只有 1 条时沿用日报原标题，多条时标题带序号（第 1 条带结论摘要）。"""
    parts = message.parts or parts_from_markdown(message.markdown or message.body, message.title)
    worst_title = f"{parts.label} {parts.headline} (99/99)".strip()
    bodies = pack(parts, max_bytes - byte_len(worst_title))
    if len(bodies) <= 1:
        body = bodies[0] if (bodies and message.parts) else (message.markdown or message.body)
        return [(message.title, body)]
    return list(zip(segment_titles(parts, len(bodies)), bodies))


def send(message: Message, env: Mapping[str, str], secrets: tuple[str, ...], **http) -> None:
    allow_http = env.get("IGOTIFY_ALLOW_HTTP", "").strip() == "1"
    base = normalize_url(env["IGOTIFY_URL"], allow_http)
    priority = parse_priority(env.get("IGOTIFY_PRIORITY"))
    sleep_fn = http.get("sleep_fn") or time.sleep
    if message.markdown or message.parts:
        segments = plan_segments(message, parse_max_bytes(env.get("IGOTIFY_MAX_BYTES")))
        extras = MARKDOWN_EXTRAS
    else:
        segments, extras = [(message.title, message.body)], None
    total = len(segments)
    if total > 1:
        log("INFO", f"iGotify 消息较长，按 UTF-8 字节分 {total} 段顺序发送（每段独立重试）")
    failed: list[int] = []
    for index, (title, body) in enumerate(segments, start=1):
        if index > 1:
            sleep_fn(SEGMENT_INTERVAL)
        payload: dict[str, object] = {"title": title, "message": body, "priority": priority}
        if extras:
            payload["extras"] = extras
        try:
            post_json(
                channel="iGotify",
                url=f"{base}/message",
                headers={"X-Gotify-Key": env["IGOTIFY_TOKEN"].strip()},
                payload=payload,
                secrets=secrets,
                **http,
            )
        except ChannelError as exc:
            failed.append(index)
            log("ERROR", f"iGotify 第 {index}/{total} 段发送失败: {exc}", secrets)
    if failed:
        # 部分失败也算该渠道失败：缺的段落里可能有关键内容，不能让「渠道成功」掩盖它
        kind = "全部失败" if len(failed) == total else "部分失败"
        raise ChannelError(
            f"iGotify {kind}：第 {'、'.join(map(str, failed))} 段失败（共 {total} 段）"
        )
