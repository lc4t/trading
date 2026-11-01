"""用固定的虚构上游数据驱动 tt-fetcher 的 main()，不访问网络。

    python sync_mock.py 159915.SZ   # 同花顺格式（A 股 ETF，前复权全量）
    python sync_mock.py 000300.SS   # Yahoo 格式（auto_adjust 路径）
"""
import datetime as dt
import sys
from unittest import mock

import adata
import numpy as np
import pandas as pd


def ths_frame(seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    price, rows, day = 3.0, [], dt.date(2021, 9, 1)
    while day <= dt.date(2026, 10, 30):
        if day.weekday() < 5:
            price *= 1 + rng.normal(0.0004, 0.011)
            rows.append(
                {
                    "trade_date": day.isoformat(),
                    "open": f"{price * 0.998:.3f}",
                    "high": f"{price * 1.01:.3f}",
                    "low": f"{price * 0.99:.3f}",
                    "close": f"{price:.3f}",
                    "volume": str(int(1e6 + rng.integers(0, 9e5))),
                    "amount": float(price * 1e6),
                    "change": 0.01,
                    "change_pct": 0.3,
                }
            )
        day += dt.timedelta(days=1)
    rows[-1]["amount"] = float("nan")  # 边界：尾行有 NaN
    rows[-1]["change"] = float("nan")
    return pd.DataFrame(rows)


def yahoo_frame(seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range("2024-12-02", "2026-10-30", tz="Asia/Shanghai")
    close = 3000 * np.cumprod(1 + rng.normal(0.0003, 0.01, len(days)))
    return pd.DataFrame(
        {"Open": close * 0.999, "High": close * 1.01, "Low": close * 0.99, "Close": close,
         "Volume": rng.integers(1e8, 2e8, len(days))},
        index=days,
    )


class FakeTicker:
    def __init__(self, symbol):
        self.frame = yahoo_frame(sum(map(ord, symbol)))

    def history(self, **kwargs):
        return self.frame


symbol = sys.argv[1]
sys.argv = ["tt-fetcher", "--symbol", symbol, "--start-date", "2024-12-01", "--end-date", "2026-10-30"]
with mock.patch.object(adata.fund.market, "get_market_etf", return_value=ths_frame(sum(map(ord, symbol)))), \
        mock.patch("yfinance.Ticker", FakeTicker):
    from trading.data.fetcher import main

    main()
