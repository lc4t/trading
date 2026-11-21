"""iGotify 渠道（服务端是 Gotify 协议）：POST {IGOTIFY_URL}/message。

环境变量：IGOTIFY_URL、IGOTIFY_TOKEN（应用令牌）、可选 IGOTIFY_PRIORITY（默认 5）、
IGOTIFY_ALLOW_HTTP=1（仅本地测试允许 http）。令牌放请求头 X-Gotify-Key，不进 URL。
"""

from __future__ import annotations

from typing import Mapping
from urllib.parse import urlsplit

from .common import ChannelError, Message, log, post_json

REQUIRED_ENV = ("IGOTIFY_URL", "IGOTIFY_TOKEN")
DEFAULT_PRIORITY = 5


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


def send(message: Message, env: Mapping[str, str], secrets: tuple[str, ...], **http) -> None:
    allow_http = env.get("IGOTIFY_ALLOW_HTTP", "").strip() == "1"
    base = normalize_url(env["IGOTIFY_URL"], allow_http)
    post_json(
        channel="iGotify",
        url=f"{base}/message",
        headers={"X-Gotify-Key": env["IGOTIFY_TOKEN"].strip()},
        payload={
            "title": message.title,
            "message": message.body,
            "priority": parse_priority(env.get("IGOTIFY_PRIORITY")),
        },
        secrets=secrets,
        **http,
    )
