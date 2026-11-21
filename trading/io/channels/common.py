"""通知渠道的公共部分：消息结构、带超时/重试的 HTTP POST、日志脱敏。

约定：
- 只对网络错误、5xx、429 重试；其它 4xx 与 3xx（不跟随重定向，避免令牌被带到别的主机）直接失败。
- 日志与异常文本一律经过 `sanitize`：令牌值、邮箱、URL 查询串都不会出现。
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping
from urllib import error, request

DEFAULT_TIMEOUT = 10.0
DEFAULT_RETRIES = 2  # 首次失败后最多再试 2 次
DEFAULT_BACKOFF = 1.0  # 指数退避基数：1s、2s
MAX_REASON_LEN = 300

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)\?[^\s]*")


class ChannelError(RuntimeError):
    """单个渠道发送失败；消息已脱敏，可直接写日志。"""


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    subject: str  # 邮件主题；其它渠道用 title


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


_opener = request.build_opener(_NoRedirect)


def default_urlopen(req: request.Request, timeout: float = DEFAULT_TIMEOUT):
    return _opener.open(req, timeout=timeout)


def sanitize(text: object, secrets: Iterable[str] = ()) -> str:
    """去掉令牌值、邮箱和 URL 查询串，并截断过长文本。"""
    result = str(text)
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        result = result.replace(secret, "***")
    result = _URL_QUERY_RE.sub(r"\1?<redacted>", result)
    result = _EMAIL_RE.sub("<email>", result)
    return result[:MAX_REASON_LEN]


def log(level: str, message: str, secrets: Iterable[str] = ()) -> None:
    print(f"[{level}] {sanitize(message, secrets)}", file=sys.stderr)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, (error.URLError, TimeoutError, ConnectionError, OSError))


def _describe(exc: BaseException) -> str:
    if isinstance(exc, error.HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, error.URLError):
        return f"网络错误: {exc.reason}"
    return f"{type(exc).__name__}: {exc}"


def post_json(
    *,
    channel: str,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, object],
    secrets: Iterable[str] = (),
    urlopen: Callable[..., object] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff: float = DEFAULT_BACKOFF,
) -> tuple[int, str]:
    """POST JSON，返回 (状态码, 响应文本)；失败抛 ChannelError。"""
    secrets = tuple(secrets)
    urlopen = urlopen or default_urlopen  # 调用时再解析，便于测试替换
    sleep_fn = sleep_fn or time.sleep
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    all_headers = {"Content-Type": "application/json", "User-Agent": "trading-digest/2.0"}
    all_headers.update(headers)
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        req = request.Request(url, data=data, headers=all_headers, method="POST")
        try:
            with urlopen(req, timeout=timeout) as response:
                status = getattr(response, "status", None) or response.getcode()
                text = response.read().decode("utf-8", errors="replace")
            if status >= 400:
                raise ChannelError(f"{channel} 返回状态码 {status}")
            return status, text
        except ChannelError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一归类后决定是否重试
            if isinstance(exc, error.HTTPError):
                exc.close()
            retryable = _is_retryable(exc)
            reason = sanitize(_describe(exc), secrets)
            if not retryable or attempt == attempts:
                suffix = f"，已尝试 {attempt} 次" if attempt > 1 else ""
                raise ChannelError(f"{channel} 发送失败: {reason}{suffix}") from None
            log("WARN", f"{channel} 第 {attempt}/{attempts} 次失败（{reason}），稍后重试", secrets)
            sleep_fn(backoff * (2 ** (attempt - 1)))
    raise ChannelError(f"{channel} 发送失败")  # pragma: no cover - 循环必然 return/raise
