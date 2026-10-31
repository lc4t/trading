"""Bearer token 鉴权：哈希存储、常量时间比较、权限范围、按 token 限流."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, FrozenSet, Iterable, List, Optional

from .errors import ConfigError

SCOPE_READ = "read"
SCOPE_REFRESH = "refresh"
SCOPE_BACKTEST = "backtest"
ALL_SCOPES = (SCOPE_READ, SCOPE_REFRESH, SCOPE_BACKTEST)
# refresh 包含 read；backtest 是独立范围
_IMPLIED = {SCOPE_REFRESH: {SCOPE_READ, SCOPE_REFRESH}}

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MIN_PLAINTEXT_TOKEN_LENGTH = 24


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def generate_token() -> str:
    return "ttm_" + secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Principal:
    name: str
    scopes: FrozenSet[str]

    def allows(self, scope: str) -> bool:
        return scope in self.scopes


class TokenRegistry:
    def __init__(self, entries: Iterable[tuple[str, FrozenSet[str], bytes]]) -> None:
        self._entries = list(entries)
        if not self._entries:
            raise ConfigError("未配置任何 token（MCP_TOKENS / MCP_TOKENS_FILE），拒绝无鉴权运行")

    def __len__(self) -> int:
        return len(self._entries)

    def authenticate(self, token: str) -> Optional[Principal]:
        """对所有条目都做常量时间比较，不因命中位置提前返回."""
        digest = hash_token(token)
        match: Optional[Principal] = None
        for name, scopes, expected in self._entries:
            if hmac.compare_digest(digest, expected) and match is None:
                match = Principal(name, scopes)
        return match

    @classmethod
    def from_json(cls, *documents: str) -> "TokenRegistry":
        entries: List[tuple[str, FrozenSet[str], bytes]] = []
        names: set[str] = set()
        for document in documents:
            try:
                items = json.loads(document)
            except ValueError as exc:
                raise ConfigError("token 配置不是合法 JSON") from exc
            if not isinstance(items, list):
                raise ConfigError("token 配置必须是 JSON 数组")
            for index, item in enumerate(items):
                entries.append(_parse_entry(index, item, names))
        return cls(entries)


def _parse_entry(index: int, item: Any, names: set[str]) -> tuple[str, FrozenSet[str], bytes]:
    where = f"token 配置第 {index + 1} 项"
    if not isinstance(item, dict):
        raise ConfigError(f"{where} 必须是对象")
    name = item.get("name")
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise ConfigError(f"{where} 的 name 必须匹配 [A-Za-z0-9_.-]{{1,40}}")
    if name in names:
        raise ConfigError(f"token 名字重复: {name}")
    names.add(name)
    raw_scopes = item.get("scopes")
    if not isinstance(raw_scopes, list) or not raw_scopes:
        raise ConfigError(f"{where}（{name}）的 scopes 必须是非空数组")
    scopes: set[str] = set()
    for scope in raw_scopes:
        if scope not in ALL_SCOPES:
            raise ConfigError(f"{where}（{name}）含未知 scope，可选: {', '.join(ALL_SCOPES)}")
        scopes |= _IMPLIED.get(scope, {scope})
    sha, plain = item.get("sha256"), item.get("token")
    if (sha is None) == (plain is None):
        raise ConfigError(f"{where}（{name}）必须且只能提供 sha256 或 token 之一")
    if sha is not None:
        if not isinstance(sha, str) or not SHA256_PATTERN.fullmatch(sha.lower()):
            raise ConfigError(f"{where}（{name}）的 sha256 必须是 64 位十六进制")
        digest = bytes.fromhex(sha.lower())
    else:
        if not isinstance(plain, str) or len(plain) < MIN_PLAINTEXT_TOKEN_LENGTH:
            raise ConfigError(
                f"{where}（{name}）的明文 token 至少 {MIN_PLAINTEXT_TOKEN_LENGTH} 个字符；"
                "建议改用 sha256（tt-serve gen-token 生成）"
            )
        digest = hash_token(plain)  # 明文只在这里短暂存在，内存里只留哈希
    return name, frozenset(scopes), digest


class RateLimiter:
    """按 key 的滑动窗口限流（进程内）."""

    def __init__(self, limit: int, window: float = 60.0, *, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window
        self._clock = clock
        self._events: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str) -> Optional[float]:
        """允许则返回 None，超限则返回需要等待的秒数."""
        now = self._clock()
        with self._lock:
            events = self._events.setdefault(key, deque())
            while events and now - events[0] >= self.window:
                events.popleft()
            if len(events) >= self.limit:
                return max(self.window - (now - events[0]), 0.0)
            events.append(now)
            return None
