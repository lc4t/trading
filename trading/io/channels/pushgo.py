"""PushGo 渠道：沿用 trading.io.digest.send_pushgo_notification，变量名与默认行为不变。"""

from __future__ import annotations

from typing import Mapping
from urllib import request

from ..digest import DEFAULT_PUSHGO_URL, send_pushgo_notification
from .common import ChannelError, Message

REQUIRED_ENV = ("PUSHGO_CHANNEL_ID", "PUSHGO_PASSWORD")


def send(
    message: Message,
    env: Mapping[str, str],
    secrets: tuple[str, ...],
    *,
    pushgo_url: str | None = None,
    max_retries: int = 3,
    retry_delay: int = 5,
    urlopen=None,
    sleep_fn=None,
    **_ignored,
) -> None:
    kwargs = {"urlopen": urlopen or request.urlopen}  # 调用时再解析，便于测试替换
    if sleep_fn is not None:
        kwargs["sleep_fn"] = sleep_fn
    try:
        send_pushgo_notification(
            title=message.title,
            body=message.body,
            channel_id=env["PUSHGO_CHANNEL_ID"].strip(),
            password=env["PUSHGO_PASSWORD"].strip(),
            pushgo_url=pushgo_url or env.get("PUSHGO_URL", "").strip() or DEFAULT_PUSHGO_URL,
            max_retries=max_retries,
            retry_delay=retry_delay,
            **kwargs,
        )
    except RuntimeError as exc:
        raise ChannelError(str(exc)) from None
