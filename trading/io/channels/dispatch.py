"""多渠道分发：已配置的渠道各自独立发送，单个失败不影响其它渠道。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Mapping

from . import igotify, pushgo, resend
from .common import ChannelError, Message, log, sanitize

# 值属于令牌/密码/邮箱，永远不进日志
SECRET_ENV = (
    "PUSHGO_CHANNEL_ID",
    "PUSHGO_PASSWORD",
    "RESEND_API_KEY",
    "RESEND_FROM",
    "RESEND_TO",
    "IGOTIFY_TOKEN",
)


@dataclass(frozen=True)
class Channel:
    name: str
    required_env: tuple[str, ...]
    send: Callable[..., None]


CHANNELS: tuple[Channel, ...] = (
    Channel("PushGo", pushgo.REQUIRED_ENV, pushgo.send),
    Channel("Resend", resend.REQUIRED_ENV, resend.send),
    Channel("iGotify", igotify.REQUIRED_ENV, igotify.send),
)


@dataclass(frozen=True)
class DispatchResult:
    sent: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]  # (渠道名, 已脱敏原因)
    skipped: tuple[str, ...]

    @property
    def configured(self) -> bool:
        return bool(self.sent or self.failed)

    @property
    def ok(self) -> bool:
        """至少一个渠道发送成功。"""
        return bool(self.sent)


def _secret_values(env: Mapping[str, str]) -> tuple[str, ...]:
    values = []
    for name in SECRET_ENV:
        raw = env.get(name, "").strip()
        if raw:
            values.append(raw)
            if name == "RESEND_TO":
                values.extend(part.strip() for part in raw.split(",") if part.strip())
    return tuple(values)


def send_all(
    message: Message,
    env: Mapping[str, str] | None = None,
    channels: tuple[Channel, ...] = CHANNELS,
    **options,
) -> DispatchResult:
    """向所有已配置渠道发送；options 透传给各渠道（urlopen、sleep_fn、pushgo_url 等）。"""
    env = os.environ if env is None else env
    secrets = _secret_values(env)
    sent: list[str] = []
    failed: list[tuple[str, str]] = []
    skipped: list[str] = []
    for channel in channels:
        missing = [n for n in channel.required_env if not env.get(n, "").strip()]
        if missing:
            skipped.append(channel.name)
            log("INFO", f"渠道 {channel.name} 未配置（缺少 {', '.join(missing)}），已跳过")
            continue
        try:
            channel.send(message, env, secrets, **_options_for(channel, options))
        except Exception as exc:  # noqa: BLE001 - 渠道之间必须互不影响
            reason = exc if isinstance(exc, ChannelError) else f"{type(exc).__name__}"
            reason_text = sanitize(reason, secrets)
            failed.append((channel.name, reason_text))
            log("ERROR", f"渠道 {channel.name} 发送失败: {reason_text}", secrets)
        else:
            sent.append(channel.name)
            log("INFO", f"渠道 {channel.name} 发送成功")
    return DispatchResult(tuple(sent), tuple(failed), tuple(skipped))


def _options_for(channel: Channel, options: Mapping[str, object]) -> dict[str, object]:
    """只有 PushGo 认识 pushgo_url / max_retries / retry_delay；HTTP 通用选项给所有渠道。"""
    pushgo_only = {"pushgo_url", "max_retries", "retry_delay"}
    if channel.name == "PushGo":
        return dict(options)
    return {k: v for k, v in options.items() if k not in pushgo_only}
