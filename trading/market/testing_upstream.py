"""仅用于演练 / 测试：把上游请求指向一个本地假服务.

设置 ``MCP_TEST_UPSTREAM_URL=http://127.0.0.1:<port>`` 后，东财（K 线 / 基金净值 / trend）、同花顺和 Yahoo
的请求都改发到该地址（路径 ``/eastmoney/...``、``/ths/...``、``/yahoo/...``），假服务可以按需返回 429 /
IP 限制，用来真实演练限流冷却。**只接受回环地址**，其它地址直接拒绝；不设置该变量时本模块完全不起作用。
"""
from __future__ import annotations

import os
from typing import Any, Optional
from urllib.parse import urlparse

from loguru import logger

from .errors import ConfigError

ENV = "MCP_TEST_UPSTREAM_URL"
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_warned = False


def base_url() -> Optional[str]:
    global _warned
    raw = (os.environ.get(ENV) or "").strip().rstrip("/")
    if not raw:
        return None
    host = urlparse(raw).hostname
    if host not in _LOOPBACK:
        raise ConfigError(f"{ENV} 只允许回环地址（127.0.0.1 / localhost / ::1）")
    if not _warned:
        _warned = True
        logger.warning(f"{ENV} 已设置：所有上游请求都指向 {raw}（仅用于演练，生产环境不要设置）")
    return raw


def endpoint(default: str, path: str) -> str:
    base = base_url()
    return f"{base}{path}" if base else default


def ths_frame(code: str) -> Any:
    """同花顺 ETF 日线：返回 DataFrame；假服务模拟 IP 限制时返回 Exception 对象（与 adata 的行为一致）."""
    import pandas as pd
    import requests

    response = requests.get(f"{base_url()}/ths/etf/{code}", timeout=10)
    if response.status_code == 429 or response.text.strip() == "IP_LIMIT":
        return Exception("ip limit")
    response.raise_for_status()
    return pd.DataFrame(response.json()["rows"])


def ticker_class() -> Optional[type]:
    """Yahoo：返回一个 ``yf.Ticker`` 的替身（HTTP 429 → ``YFRateLimitError``，与真实行为一致）；未启用返回 None."""
    if base_url() is None:
        return None
    import pandas as pd
    import requests
    from yfinance.exceptions import YFRateLimitError

    class FakeYahooTicker:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol

        def history(self, **kwargs: Any) -> Any:
            params = {"start": str(kwargs.get("start")), "end": str(kwargs.get("end"))}
            response = requests.get(f"{base_url()}/yahoo/{self.symbol}", params=params, timeout=10)
            if response.status_code == 429:
                raise YFRateLimitError()
            response.raise_for_status()
            rows = response.json()["rows"]
            frame = pd.DataFrame(rows)
            if frame.empty:
                return frame
            frame.index = pd.DatetimeIndex(pd.to_datetime(frame.pop("date")), tz="America/New_York")
            return frame.rename(columns={"splits": "Stock Splits", "dividends": "Dividends"})

    return FakeYahooTicker
