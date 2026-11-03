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
