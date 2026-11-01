"""行情服务的异常类型."""


class MarketError(RuntimeError):
    """行情服务基类异常."""


class ConfigError(MarketError):
    """启动配置缺失或非法（消息里只写变量名，绝不回显值）."""


class RequestError(ValueError):
    """调用方入参非法."""


class FetchError(MarketError):
    """上游抓取失败."""


class ReadOnlyError(MarketError):
    """数据库账号没有写权限（或显式只读模式）：只能读缓存，不回源."""
