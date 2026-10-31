"""单标的回测引擎。

行为与 1.0 的 `BacktestRunner.run_single_backtest` 一致；
仅依赖更轻量的接口，方便后续被新策略复用。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Type

import backtrader as bt
import pandas as pd
from loguru import logger

from ..analysis.metrics import PerformanceAnalyzer
from ..analysis.report import build_report
from ..data.repository import DBClient
from ..market.store import MarketStore
from ..strategies.dual_ma import DualMAStrategy


def _attach_analyzers(cerebro: bt.Cerebro) -> None:
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    cerebro.addanalyzer(bt.analyzers.VWR, _name="vwr")
    cerebro.addanalyzer(bt.analyzers.SQN, _name="sqn")
    # 日度组合收益率，用于准确的波动率 / 夏普 / Beta
    cerebro.addanalyzer(
        bt.analyzers.TimeReturn, _name="dailyret", timeframe=bt.TimeFrame.Days
    )


def _make_data_feed(data: pd.DataFrame) -> bt.feeds.PandasData:
    return bt.feeds.PandasData(
        dataname=data,
        datetime="date",
        open="open_price",
        high="high",
        low="low",
        close="close_price",
        volume="volume",
        openinterest=-1,
    )


def fetch_symbol_data(
    db_client: DBClient,
    symbol: str,
    start_date: datetime,
    end_date: datetime,
) -> pd.DataFrame:
    """从数据库读取单标的的 OHLCV，并做类型规整（取数统一走 trading.market.store）。"""
    return MarketStore(db_client.engine).ohlcv_frame(symbol, start_date, end_date)


def fetch_benchmark_daily_returns(
    db_client: DBClient,
    symbol: str,
    start_date: datetime,
    end_date: datetime,
) -> Dict[date, float]:
    """获取基准日收益率序列（与 1.0 算法一致）。"""
    benchmark_returns: Dict[date, float] = {}
    try:
        rows = MarketStore(db_client.engine).close_series(symbol, start_date, end_date)

        if not rows:
            logger.warning(f"未找到基准数据: {symbol}")
            return {}

        for i in range(1, len(rows)):
            prev_close = rows[i - 1][1]
            curr_close = rows[i][1]
            if prev_close > 0:
                benchmark_returns[rows[i][0]] = (curr_close / prev_close) - 1

        logger.info(f"成功获取基准数据: {symbol}, 数据点数: {len(benchmark_returns)}")
    except Exception as e:  # noqa: BLE001 — 与原行为保持一致：吞掉异常并打日志
        logger.error(f"获取基准数据失败: {e}")
        logger.exception(e)

    return benchmark_returns


@dataclass
class SingleBacktestEngine:
    """单标的回测引擎。"""

    db_client: DBClient

    def run(
        self,
        symbol: str,
        start_date: datetime,
        end_date: datetime,
        params: Dict[str, Any],
        strategy_cls: Type[bt.Strategy] = DualMAStrategy,
    ) -> Optional[Dict[str, Any]]:
        data = fetch_symbol_data(self.db_client, symbol, start_date, end_date)
        if data.empty:
            raise ValueError("No data available for backtesting")

        cerebro = bt.Cerebro()
        cerebro.adddata(_make_data_feed(data))

        # 拆分非策略参数
        initial_capital = params.pop("initial_capital")
        commission_rate = params.pop("commission_rate", 0.0001)
        benchmark = params.pop("benchmark", None)
        risk_free_rate = params.pop("risk_free_rate", 0.03)

        benchmark_returns: Dict[date, float] = {}
        if benchmark:
            benchmark_returns = fetch_benchmark_daily_returns(
                self.db_client, benchmark, start_date, end_date
            )

        cerebro.broker.setcash(initial_capital)
        cerebro.broker.setcommission(commission=commission_rate)
        cerebro.addstrategy(strategy_cls, **params)
        _attach_analyzers(cerebro)

        results = cerebro.run()
        if not results:
            return None

        strat = results[0]

        analyzers_results = {
            "trades": strat.analyzers.trades.get_analysis(),
            "sharpe": strat.analyzers.sharpe.get_analysis(),
            "drawdown": strat.analyzers.drawdown.get_analysis(),
            "vwr": strat.analyzers.vwr.get_analysis(),
            "sqn": strat.analyzers.sqn.get_analysis(),
            "last_date": strat.data.datetime.datetime(),
        }

        metrics = PerformanceAnalyzer.calculate_metrics(
            initial_capital=initial_capital,
            final_value=cerebro.broker.getvalue(),
            trade_records=strat.trade_records,
            analyzers_results=analyzers_results,
            benchmark_data=benchmark_returns,
            benchmark_symbol=benchmark,
            risk_free_rate=risk_free_rate,
            daily_returns_series=strat.analyzers.dailyret.get_analysis(),
        )

        return build_report(
            strat=strat,
            initial_capital=initial_capital,
            final_value=cerebro.broker.getvalue(),
            trade_records=strat.trade_records,
            metrics=metrics,
        )
