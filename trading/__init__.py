"""trading — 量化交易数据同步与回测工具集.

公共 API:

    from trading import DBClient, SingleBacktestEngine
    from trading.strategies.dual_ma import DualMAStrategy

后续 2.0 会加入 `run_backtest()` facade + 动量策略。

公共名字按需（惰性）导入：``tt-serve`` 行情服务只用 ``trading.market``，
不应该为了启动而加载 backtrader / 回测引擎等重依赖。
"""
from __future__ import annotations

import importlib
from typing import Any

_LAZY = {
    "DBClient": ".data.repository",
    "SymbolInfo": ".data.repository",
    "TradingData": ".data.repository",
    "DataFetcher": ".data.fetcher",
    "ADataFetcher": ".data.fetcher",
    "StockDataManager": ".data.fetcher",
    "YFinanceFetcher": ".data.fetcher",
    "SingleBacktestEngine": ".engine.single",
    "MultiBacktestEngine": ".engine.multi",
    "DualMAStrategy": ".strategies.dual_ma",
    "Schedule": ".strategies.momentum",
    "MOMENTUM_FUNCTIONS": ".strategies.momentum",
    "BacktestResult": ".api",
    "MomentumBasket": ".api",
    "MomentumTopN": ".api",
    "run_backtest": ".api",
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module 'trading' has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
