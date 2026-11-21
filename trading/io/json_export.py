"""把回测结果序列化成前端可消费的 JSON 结构（与 1.0 一致）。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List

from ..data.repository import DBClient, SymbolInfo
from loguru import logger


def get_stock_name(db_client: DBClient, symbol: str) -> str:
    """从数据库获取股票名称，失败时回退到 symbol。"""
    try:
        with db_client.Session() as session:
            info = session.query(SymbolInfo).filter(SymbolInfo.symbol == symbol).first()
            if info:
                return info.name
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error getting stock name: {e}")
    return symbol


def signal_extras(next_signal: dict) -> dict:
    """latestSignal 的附加字段：止损位与触发条件（旧前端忽略未知字段，纯增量）。"""
    extras: Dict[str, Any] = {}
    stop = next_signal.get("stop_loss")
    if isinstance(stop, dict) and stop.get("price") is not None:
        extras["stopLoss"] = {
            "type": stop.get("type"),
            "price": round(float(stop["price"]), 3),
            "distancePct": round(float(stop.get("distance_pct") or 0), 2),
        }
    conditions = [str(c) for c in next_signal.get("conditions") or [] if c]
    if conditions:
        extras["conditions"] = conditions
    return extras


def format_for_json(
    metrics: dict,
    trades: list,
    next_signal: dict,
    params: dict,
    symbol: str,
    stock_name: str,
    initial_capital: float,
) -> dict:
    """格式化回测结果为 JSON。结构需与前端保持兼容。"""
    db_client = DBClient()
    latest_data = db_client.query_latest_by_symbol(symbol)

    if latest_data:
        latest_prices = {
            "open": round(latest_data["open_price"], 3),
            "close": round(latest_data["close_price"], 3),
            "high": round(latest_data["high"], 3),
            "low": round(latest_data["low"], 3),
        }
        latest_date = latest_data["date"].strftime("%Y-%m-%d")
    else:
        latest_prices = next_signal.get("prices", {})
        latest_date = next_signal.get("timestamp", datetime.now().strftime("%Y-%m-%d"))

    result: Dict[str, Any] = {
        "symbol": symbol,
        "name": stock_name,
        "reportDate": datetime.now().strftime("%Y-%m-%d"),
        "dateRange": {
            "start": metrics["start_date"].strftime("%Y-%m-%d"),
            "end": latest_date,
        },
        "latestSignal": {
            "action": next_signal["action"],
            "asset": stock_name,
            "timestamp": latest_date,
            "prices": latest_prices,
            **signal_extras(next_signal),
        },
        "positionInfo": next_signal.get("position_info"),
        "annualReturns": [
            {"year": int(year), "value": round(value, 3)}
            for year, value in metrics.get("yearly_returns", {}).items()
        ],
        "returnMetrics": [
            {"name": "最新净值", "value": round(metrics["latest_nav"], 3),
             "description": "当前投资组合价值相对于初始资金的比值，反映总体收益情况"},
            {"name": "年化收益率", "value": round(metrics["annual_return"], 2),
             "description": "将总收益按年度平均计算的收益率，用于评估策略的整体盈利能力"},
            {"name": "复合年化收益", "value": round(metrics["cagr"], 2),
             "description": "考虑收益再投资的年化收益率，更准确地反映长期投资回报"},
            {"name": "Alpha", "value": round(metrics.get("alpha", 0), 2),
             "description": "策略相对于市场基准的超额收益，反映策略的选股择时能力"},
            {"name": "总盈亏", "value": round(metrics["total_pnl"], 2),
             "description": "所有交易产生的盈亏总和，包括已实现和未实现盈亏"},
        ],
        "riskMetrics": [
            {"name": "最大回撤", "value": round(metrics["max_drawdown"], 2),
             "description": "任意时间段内净值的最大跌幅，反映策略最大可能损失，越小越好"},
            {"name": "当前回撤", "value": round(metrics["current_drawdown"], 2),
             "description": "当前净值距离历史最高点的跌幅，反映当前的风险状况"},
            {"name": "波动率", "value": round(metrics["volatility"], 2),
             "description": "收益率的标准差，反映策略的波动性和风险大小，越小越稳定"},
            {"name": "最大亏损金额", "value": round(metrics["max_loss_amount"], 2),
             "description": "单笔交易中的最大亏损金额，反映策略的风险控制能力"},
            {"name": "最大亏损比例", "value": round(metrics["max_loss_pct"], 2),
             "description": "单笔交易中的最大亏损比例，反映策略的风险控制能力"},
            {"name": "Beta系数", "value": round(metrics.get("beta", 0), 2),
             "description": "策略收益相对于市场基准的敏感度，=1表示与市场同步，<1表示波动小于市场"},
        ],
        "riskAdjustedMetrics": [
            {"name": "夏普比率", "value": round(metrics["sharpe_ratio"], 2),
             "description": "超额收益与波动率的比值，>1 较好，>2 优秀"},
            {"name": "索提诺比率", "value": round(metrics["sortino_ratio"], 2),
             "description": "类似夏普比率，仅考虑下行波动率。>2 较好"},
            {"name": "Calmar比率", "value": round(metrics["calmar_ratio"], 2),
             "description": "年化收益率除以最大回撤。>1 较好，>3 优秀"},
            {"name": "VWR", "value": round(metrics["vwr"], 2),
             "description": "波动率加权收益率，>5 较好"},
            {"name": "SQN", "value": round(metrics["sqn"], 2),
             "description": "系统质量指数，>2 较好，>4 优秀"},
        ],
        "tradingMetrics": [
            {"name": "总交易次数", "value": metrics["total_trades"],
             "description": "买入和卖出操作总次数"},
            {"name": "胜率", "value": round(metrics["win_rate"], 2),
             "description": "盈利交易占比"},
            {"name": "盈亏比", "value": round(metrics["profit_factor"], 2),
             "description": "总盈利除以总亏损"},
            {"name": "平均盈利", "value": round(metrics["avg_won"], 2),
             "description": "单次盈利交易的平均盈利金额"},
            {"name": "平均亏损", "value": round(metrics["avg_lost"], 2),
             "description": "单次亏损交易的平均亏损金额"},
            {"name": "盈利交易", "value": metrics["won_trades"],
             "description": "产生盈利的交易次数"},
            {"name": "亏损交易", "value": metrics["lost_trades"],
             "description": "产生亏损的交易次数"},
            {"name": "最大连胜", "value": metrics["max_consecutive_wins"],
             "description": "最大连续盈利的交易次数"},
            {"name": "最大连亏", "value": metrics["max_consecutive_losses"],
             "description": "最大连续亏损的交易次数"},
        ],
        "positionMetrics": [
            {"name": "持仓比例", "value": round(metrics["holding_ratio"], 2),
             "description": "持仓时间占总交易时间的百分比"},
            {"name": "平均持仓", "value": f"{metrics['avg_holding_period']}天",
             "description": "每笔交易的平均持有时间"},
        ],
        "benchmarkMetrics": [
            {"name": "Beta参考", "value": metrics.get("benchmark_symbol", "N/A"),
             "description": "用于计算 Beta 和 Alpha 的市场基准"},
            {"name": "Beta状态", "value": metrics.get("beta_status", "N/A"),
             "description": "Beta 系数的计算状态说明"},
        ],
        "timeMetrics": [
            {"name": "运行天数", "value": metrics["running_days"],
             "description": "回测的总天数"},
            {"name": "开始日期", "value": metrics["start_date"].strftime("%Y-%m-%d"),
             "description": "回测起始的交易日期"},
            {"name": "结束日期", "value": metrics["end_date"].strftime("%Y-%m-%d"),
             "description": "回测结束的交易日期"},
        ],
        "strategyParameters": None,
        "showStrategyParameters": False,
    }

    formatted_trades: List[Dict[str, Any]] = []
    buy_queue: List[Dict[str, Any]] = []

    for trade in trades:
        trade_date = trade["date"]
        if isinstance(trade_date, (datetime, date)):
            trade_date = trade_date.strftime("%Y-%m-%d")

        pnl_percentage = None
        entry_price = None

        if trade["action"] == "BUY":
            buy_queue.append(
                {"price": trade["price"], "size": trade["size"], "remaining": trade["size"]}
            )
        elif trade["action"] == "SELL" and buy_queue:
            total_size = trade["size"]
            total_cost = 0
            size_left = total_size
            for buy in buy_queue:
                if buy["remaining"] > 0:
                    matched_size = min(size_left, buy["remaining"])
                    total_cost += matched_size * buy["price"]
                    size_left -= matched_size
                    buy["remaining"] -= matched_size
                    if size_left == 0:
                        break
            if size_left == 0:
                entry_price = total_cost / total_size
                pnl_percentage = ((trade["price"] - entry_price) / entry_price) * 100
            buy_queue = [b for b in buy_queue if b["remaining"] > 0]

        formatted_trades.append(
            {
                "date": trade_date,
                "action": trade["action"],
                "price": round(trade["price"], 3),
                "quantity": trade["size"],
                "value": round(trade["value"], 2),
                "profitLoss": round(trade.get("pnl", 0), 2),
                "profitLossPercentage": (
                    round(pnl_percentage, 2) if pnl_percentage is not None else None
                ),
                "totalValue": round(trade["total_value"], 2),
                "reason": trade.get("signal_reason", ""),
                "entryPrice": round(entry_price, 3) if entry_price is not None else None,
            }
        )

    result["recentTrades"] = formatted_trades

    if next_signal.get("position_info"):
        position_info = next_signal["position_info"]
        result["positionInfo"] = {
            "entry_date": position_info["entry_date"],
            "entry_price": round(position_info["entry_price"], 3),
            "position_size": position_info["position_size"],
            "position_value": round(position_info["position_value"], 2),
            "current_price": round(position_info["current_price"], 3),
            "current_value": round(position_info["current_value"], 2),
            "cost": round(position_info["cost"], 2),
            "unrealized_pnl": round(position_info["unrealized_pnl"], 2),
            "unrealized_pnl_pct": round(position_info["unrealized_pnl_pct"], 2),
        }
    else:
        result["positionInfo"] = None

    return result
