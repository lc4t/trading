"""日报推送渠道：PushGo、Resend（邮件）、iGotify（Gotify 协议）。

所有已配置渠道都会尝试发送，彼此独立；只要有一个成功即视为成功。
"""

from .common import ChannelError, Message
from .dispatch import CHANNELS, DispatchResult, send_all

__all__ = ["CHANNELS", "ChannelError", "DispatchResult", "Message", "send_all"]
