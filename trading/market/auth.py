"""Bearer token 鉴权：哈希存储、常量时间比较、权限范围、按 token 限流."""
from __future__ import annotations

import hashlib
import hmac
import datetime as dt
import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
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


DEFAULT_CONFIG_DIR = "/data/config"
TOKENS_FILENAME = "tokens.json"
Entry = tuple[str, FrozenSet[str], bytes]


class TokenStore:
    """配置目录里的 token 文件：只存哈希，权限 0600，原子写入."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / TOKENS_FILENAME

    def read_entries(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, NotADirectoryError):
            return []
        except (OSError, ValueError) as exc:
            raise ConfigError(f"无法读取 token 文件（{type(exc).__name__}）") from None
        if not isinstance(data, list):
            raise ConfigError("token 文件必须是 JSON 数组")
        return data

    def write_entries(self, entries: List[Dict[str, Any]]) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".tokens-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(entries, handle, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def writable(self) -> bool:
        try:
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".probe-")
            os.close(fd)
            os.unlink(tmp)
            return True
        except OSError:
            return False

    def signature(self) -> Optional[tuple[int, int]]:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    @staticmethod
    def new_entry(name: str, scopes: Iterable[str]) -> tuple[str, Dict[str, Any]]:
        """生成 (明文 token, 可落盘的条目)。"""
        scope_list = sorted(set(scopes))
        token = generate_token()
        entry = {
            "name": name,
            "scopes": scope_list,
            "sha256": hash_token(token).hex(),
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }
        _parse_entry(0, entry, set())  # 复用校验（name / scopes 合法性）
        return token, entry

    def add(self, name: str, scopes: Iterable[str]) -> str:
        entries = self.read_entries()
        if any(e.get("name") == name for e in entries):
            raise ConfigError(f"token 名字已存在: {name}（要更换请用 token rotate）")
        token, entry = self.new_entry(name, scopes)
        self.write_entries(entries + [entry])
        return token

    def rotate(self, name: str) -> str:
        entries = self.read_entries()
        for i, existing in enumerate(entries):
            if existing.get("name") == name:
                token, entry = self.new_entry(name, existing.get("scopes") or [SCOPE_READ])
                entries[i] = entry
                self.write_entries(entries)
                return token
        raise ConfigError(f"找不到名为 {name} 的 token")


class TokenRegistry:
    """env 里的静态条目 + 配置目录里的 token 文件（修改后自动热加载）."""

    def __init__(self, entries: Iterable[Entry] = (), store: Optional[TokenStore] = None) -> None:
        self._static: List[Entry] = list(entries)
        self._store = store
        self._file: List[Entry] = []
        self._signature: Any = object()  # 保证第一次一定加载
        self._lock = threading.Lock()
        self._refresh_file(initial=True)

    def __len__(self) -> int:
        self._refresh_file()
        return len(self._static) + len(self._file)

    def add_in_memory(self, name: str, scopes: Iterable[str], digest: bytes) -> None:
        self._static.append((name, frozenset(_expand(scopes)), digest))

    def describe(self) -> List[tuple[str, List[str], str]]:
        self._refresh_file()
        rows = [(n, sorted(sc), "env") for n, sc, _ in self._static]
        rows += [(n, sorted(sc), "file") for n, sc, _ in self._file]
        return rows

    def _refresh_file(self, initial: bool = False) -> None:
        if self._store is None:
            return
        signature = self._store.signature()
        if signature == self._signature:
            return
        with self._lock:
            try:
                names = {n for n, _, _ in self._static}
                parsed = [_parse_entry(i, item, names) for i, item in enumerate(self._store.read_entries())]
            except ConfigError:
                if initial:
                    raise
                return  # 热加载遇到坏文件：保留上一份有效内容
            self._file = parsed
            self._signature = signature

    def authenticate(self, token: str) -> Optional[Principal]:
        """对所有条目都做常量时间比较，不因命中位置提前返回."""
        self._refresh_file()
        digest = hash_token(token)
        match: Optional[Principal] = None
        for name, scopes, expected in (*self._static, *self._file):
            if hmac.compare_digest(digest, expected) and match is None:
                match = Principal(name, scopes)
        return match

    @classmethod
    def from_json(cls, *documents: str, store: Optional[TokenStore] = None) -> "TokenRegistry":
        entries: List[Entry] = []
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
        return cls(entries, store)


def _expand(scopes: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for scope in scopes:
        out |= _IMPLIED.get(scope, {scope})
    return out


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
        scopes |= _expand([scope])
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
                "建议改用 sha256（trading-mcp gen-token 生成）"
            )
        digest = hash_token(plain)  # 明文只在这里短暂存在，内存里只留哈希
    return name, frozenset(scopes), digest


@dataclass(frozen=True)
class Bootstrap:
    token: str
    persisted: bool
    path: Path


def bootstrap_token(registry: TokenRegistry, store: TokenStore) -> Optional[Bootstrap]:
    """没有任何 token 时自动生成一个 read token（鉴权无法关闭，所以不允许「没有 token」）.

    配置目录可写 → 写入哈希并持久化；不可写 → 仅内存有效（重启后会变），由调用方大声警告。
    已有 token（env 或文件）时什么都不做，因此重启后不会再次展示。
    """
    if len(registry) > 0:
        return None
    if store.writable():
        try:
            return Bootstrap(store.add("default", [SCOPE_READ]), True, store.path)
        except OSError:
            pass
    token, entry = TokenStore.new_entry("default", [SCOPE_READ])
    registry.add_in_memory("default", entry["scopes"], bytes.fromhex(entry["sha256"]))
    return Bootstrap(token, False, store.path)


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
