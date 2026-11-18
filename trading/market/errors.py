"""行情服务的异常类型."""


class MarketError(RuntimeError):
    """行情服务基类异常."""


class ConfigError(MarketError):
    """启动配置缺失或非法（消息里只写变量名，绝不回显值）."""


class RequestError(ValueError):
    """调用方入参非法."""


class FetchError(MarketError):
    """上游抓取失败."""


class RateLimited(FetchError):
    """上游限流（429、Yahoo rate limit、同花顺 IP 限制）或本进程对该来源的冷却/排队保护.

    ``retry_after`` 是建议调用方至少等待的秒数；冷却期内同一来源不会再发起任何上游请求.
    """

    def __init__(self, source: str, retry_after: float, message: str = "", hinted: bool = False) -> None:
        self.source = source
        self.retry_after = max(float(retry_after), 1.0)
        # hinted=True 表示 retry_after 是上游明确给出的（Retry-After 头）；否则只是占位值，冷却时长由本地策略决定
        self.hinted = hinted
        super().__init__(message or f"upstream {source} is rate limited; retry after {int(self.retry_after)}s")


class ReadOnlyError(MarketError):
    """数据库账号没有写权限（或显式只读模式）：只能读缓存，不回源."""


class BasisMismatch(FetchError):
    """备用源的价格口径与库里已有序列不同（或重叠日校验没通过）：不追加、不拼接.

    ``detail`` 是给调用方看的结构化说明（序列口径、候选口径、原因、重叠日对照）。
    """

    def __init__(self, detail: dict, attempts: list | None = None) -> None:
        self.detail = detail
        self.attempts = attempts or []
        super().__init__(
            f"basis_mismatch: series is {detail.get('series_basis')}, candidate {detail.get('candidate_basis')} "
            f"({detail.get('reason')}); not appended"
        )


class BackupFailed(FetchError):
    """序列已锁在备用口径上、而所有备用源都失败了（不是限流）；``attempts`` 逐个列出失败原因."""

    def __init__(self, attempts: list) -> None:
        self.attempts = attempts
        super().__init__("all backup upstream sources failed: " + summarize_attempts(attempts))


class BackupExhausted(RateLimited):
    """Yahoo 限流 / 冷却，备用源也都没能给出可追加的数据：对外仍是 ``rate_limited``，同时带上每个备用源的结果."""

    def __init__(
        self, source: str, retry_after: float, attempts: list, hinted: bool = False, basis_mismatch: dict | None = None
    ) -> None:
        self.attempts = attempts
        self.basis_mismatch = basis_mismatch  # 有备用源因口径锁被拒绝时附带说明；对外错误仍是 rate_limited
        super().__init__(
            source,
            retry_after,
            f"upstream {source} is rate limited and backup sources failed: " + summarize_attempts(attempts),
            hinted=hinted,
        )


def summarize_attempts(attempts: list) -> str:
    return "; ".join(
        f"{a.get('source')}={a.get('result') or a.get('error')}" for a in attempts
    ) or "no backup source available"
