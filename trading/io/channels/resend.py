"""Resend 邮件渠道：POST https://api.resend.com/emails，Bearer 鉴权。

环境变量：RESEND_API_KEY、RESEND_FROM、RESEND_TO（多个收件人用逗号分隔）。
邮件同时带 html 与 text 两个版本（html 由同一份 Markdown 转换，见 markup.py）。
"""

from __future__ import annotations

import hashlib
from typing import Mapping

from .common import ChannelError, Message, post_json

RESEND_URL = "https://api.resend.com/emails"
REQUIRED_ENV = ("RESEND_API_KEY", "RESEND_FROM", "RESEND_TO")


def parse_recipients(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def send(message: Message, env: Mapping[str, str], secrets: tuple[str, ...], **http) -> None:
    recipients = parse_recipients(env["RESEND_TO"])
    if not recipients:
        raise ChannelError("Resend 收件人为空")
    # 幂等键：同一份日报重试不会重复发信（Resend 支持 Idempotency-Key）
    digest_key = hashlib.sha256(
        f"{message.subject}\n{message.markdown or message.body}".encode()
    ).hexdigest()[:32]
    payload = {
        "from": env["RESEND_FROM"].strip(),
        "to": recipients,
        "subject": message.subject,
        "text": message.markdown or message.body,
    }
    if message.html:
        payload["html"] = message.html
    post_json(
        channel="Resend",
        url=RESEND_URL,
        headers={
            "Authorization": f"Bearer {env['RESEND_API_KEY'].strip()}",
            "Idempotency-Key": f"digest-{digest_key}",
        },
        payload=payload,
        secrets=secrets,
        **http,
    )
