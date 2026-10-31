"""Phase B：把 Trading 已有的回测能力以薄封装暴露为 MCP 工具（需 ``backtest`` scope）.

只读取库里已缓存的日线，不回源、不画图；回测引擎（backtrader）在第一次调用时才惰性导入，
trading-mcp 启动本身不加载它。重 CPU 的回测放在线程里跑，并发数、区间长度、标的数都有上限。
"""
from __future__ import annotations

import datetime as dt
import math
import re
import threading
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Literal, Optional

from .cache import SYMBOL_PATTERN, MarketCache
from .config import ServerConfig
from .errors import RequestError
from .store import MarketStore

MomentumFnName = Literal["simple_return", "log_return", "sharpe", "weighted", "dual_12_1"]
SCHEDULE_PATTERN = re.compile(r"^(daily|(every_n|weekly|weekly_nth|monthly|monthly_nth):\d{1,3})$")
MAX_TRADE_LIMIT = 500


class BusyError(RequestError):
    """回测并发已满."""


def jsonable(value: Any) -> Any:
    """numpy / pandas / datetime → 纯 JSON 类型；NaN/Inf → None."""
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if hasattr(value, "isoformat"):  # pandas.Timestamp / NaT
        try:
            return value.isoformat()
        except (ValueError, TypeError):
            return None
    return value


def _symbol(value: str) -> str:
    normalized = str(value).strip().upper()
    if not SYMBOL_PATTERN.fullmatch(normalized):
        raise RequestError(f"Invalid symbol: {value[:24]!r}")
    return normalized


class BacktestTools:
    def __init__(self, store: MarketStore, cache: MarketCache, config: ServerConfig) -> None:
        self.store = store
        self.cache = cache
        self.config = config
        self._slots = threading.BoundedSemaphore(config.backtest_concurrency)

    # ------------------------------------------------------------- shared checks
    def _window(self, start_date: str, end_date: Optional[str]) -> tuple[dt.date, dt.date]:
        try:
            start = dt.date.fromisoformat(start_date)
            end = dt.date.fromisoformat(end_date) if end_date else self.cache.today()
        except ValueError as exc:
            raise RequestError("dates must use YYYY-MM-DD") from exc
        if start > end:
            raise RequestError("start_date must be on or before end_date")
        if (end - start).days > self.config.backtest_max_span_days:
            raise RequestError(
                f"date range too long: at most {self.config.backtest_max_span_days} days per backtest"
            )
        return start, end

    def _symbols(self, symbols: List[str]) -> List[str]:
        unique = list(dict.fromkeys(_symbol(s) for s in symbols))
        if not unique:
            raise RequestError("at least one symbol is required")
        if len(unique) > self.config.backtest_max_symbols:
            raise RequestError(f"at most {self.config.backtest_max_symbols} symbols per backtest")
        return unique

    @staticmethod
    def _bounded(name: str, value: float, lo: float, hi: float) -> float:
        if not (lo <= value <= hi):
            raise RequestError(f"{name} must be between {lo} and {hi}")
        return value

    def _db(self) -> Any:
        return SimpleNamespace(engine=self.store.engine, Session=self.store.Session)

    def _data_quality(self, symbols: List[str], start: dt.date, end: dt.date) -> Dict[str, Any]:
        """回测只读缓存，把输入数据的口径与定稿情况如实带回去."""
        quality: Dict[str, Any] = {}
        for symbol in symbols:
            views = [self.cache.view(symbol, r) for r in self.store.query_range(symbol, start, end)]
            bases = sorted({v["price_basis"] for v in views})
            quality[symbol] = {
                "rows": len(views),
                "first_date": views[0]["date"].isoformat() if views else None,
                "last_date": views[-1]["date"].isoformat() if views else None,
                "price_bases": bases,
                "price_basis": bases[0] if len(bases) == 1 else ("mixed" if bases else None),
                "last_row_is_final": views[-1]["is_final"] if views else None,
                "nonfinal_rows": sum(1 for v in views if not v["is_final"]),
            }
        return quality

    def _guarded(self, fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
        if not self._slots.acquire(blocking=False):
            raise BusyError("too many concurrent backtests; retry shortly")
        try:
            return fn()
        finally:
            self._slots.release()

    @staticmethod
    def _trim(trades: List[Dict[str, Any]], limit: int) -> Dict[str, Any]:
        limit = int(BacktestTools._bounded("trade_limit", limit, 0, MAX_TRADE_LIMIT))
        kept = trades[-limit:] if limit else []
        return {
            "trade_count": len(trades),
            "trades_truncated": len(trades) > limit,
            "trades": jsonable(kept),
        }

    # ----------------------------------------------------------- momentum rotation
    def run_momentum(
        self,
        *,
        kind: str,
        weights: Optional[Dict[str, float]],
        universe: Optional[List[str]],
        top_n: int,
        cash_buffer: float,
        start_date: str,
        end_date: Optional[str],
        initial_capital: float,
        commission_rate: float,
        momentum_fn: str,
        lookback: int,
        threshold: float,
        schedule: str,
        cooldown_days: int,
        trend_filter_ma: Optional[int],
        benchmark: Optional[str],
        risk_free_rate: float,
        trade_limit: int,
    ) -> Dict[str, Any]:
        start, end = self._window(start_date, end_date)
        self._bounded("initial_capital", initial_capital, 1000, 1e9)
        self._bounded("commission_rate", commission_rate, 0, 0.05)
        self._bounded("lookback", lookback, 2, 1000)
        self._bounded("cooldown_days", cooldown_days, 0, 365)
        self._bounded("risk_free_rate", risk_free_rate, 0, 1)
        if trend_filter_ma is not None:
            self._bounded("trend_filter_ma", trend_filter_ma, 2, 1000)
        if not SCHEDULE_PATTERN.fullmatch(schedule):
            raise RequestError(
                "schedule must be daily or one of every_n:N, weekly:D, weekly_nth:N, monthly:D, monthly_nth:N"
            )
        bench = _symbol(benchmark) if benchmark else None

        if kind == "basket":
            if not weights:
                raise RequestError("weights is required")
            norm = {_symbol(k): float(v) for k, v in weights.items()}
            symbols = self._symbols(list(norm))
        else:
            symbols = self._symbols(universe or [])

        def run() -> Dict[str, Any]:
            # 重依赖在这里才导入
            from ..api import MomentumBasket, MomentumTopN, run_backtest
            from ..cli.rotation_backtest import _parse_schedule
            from ..strategies.momentum import MOMENTUM_FUNCTIONS

            if momentum_fn not in MOMENTUM_FUNCTIONS:
                raise RequestError(f"unknown momentum_fn: {momentum_fn}")
            common = dict(
                momentum_fn=momentum_fn,
                lookback=lookback,
                threshold=threshold,
                schedule=_parse_schedule(schedule),
                cooldown_days=cooldown_days,
                trend_filter_ma=trend_filter_ma,
            )
            if kind == "basket":
                strategy: Any = MomentumBasket(weights=norm, **common)
            else:
                strategy = MomentumTopN(universe=symbols, top_n=top_n, cash_buffer=cash_buffer, **common)
            try:
                result = run_backtest(
                    strategy,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    initial_capital=initial_capital,
                    commission_rate=commission_rate,
                    benchmark=bench,
                    risk_free_rate=risk_free_rate,
                    db_client=self._db(),
                )
            except (TypeError, ValueError) as exc:
                raise RequestError(f"backtest rejected: {exc}") from None
            raw = result.raw
            out = {
                "strategy": "momentum_basket" if kind == "basket" else "momentum_topn",
                "universe": symbols,
                "period": {"start": start.isoformat(), "end": end.isoformat()},
                "initial_capital": raw.initial_capital,
                "final_value": raw.final_value,
                "total_return_pct": raw.total_return_pct,
                "metrics": jsonable(raw.metrics),
                "next_signal": jsonable(raw.next_signal),
                "data_quality": self._data_quality(list({*symbols, *([bench] if bench else [])}), start, end),
            }
            out.update(self._trim(raw.trades, trade_limit))
            return out

        return self._guarded(run)

    # --------------------------------------------------------------- dual MA
    def run_dual_ma(
        self,
        *,
        symbol: str,
        start_date: str,
        end_date: Optional[str],
        initial_capital: float,
        commission_rate: float,
        use_ma: bool,
        ma_short: int,
        ma_long: int,
        use_chandelier: bool,
        chandelier_period: int,
        chandelier_multiplier: float,
        use_adr: bool,
        adr_period: int,
        adr_multiplier: float,
        benchmark: Optional[str],
        risk_free_rate: float,
        trade_limit: int,
    ) -> Dict[str, Any]:
        sym = _symbol(symbol)
        start, end = self._window(start_date, end_date)
        self._bounded("initial_capital", initial_capital, 1000, 1e9)
        self._bounded("commission_rate", commission_rate, 0, 0.05)
        self._bounded("risk_free_rate", risk_free_rate, 0, 1)
        for name, value in (("ma_short", ma_short), ("ma_long", ma_long),
                            ("chandelier_period", chandelier_period), ("adr_period", adr_period)):
            self._bounded(name, value, 1, 500)
        self._bounded("chandelier_multiplier", chandelier_multiplier, 0.1, 20)
        self._bounded("adr_multiplier", adr_multiplier, 0.1, 20)
        if not (use_ma or use_chandelier or use_adr):
            raise RequestError("enable at least one of use_ma / use_chandelier / use_adr")
        if use_ma and ma_short >= ma_long:
            raise RequestError("ma_short must be smaller than ma_long")
        bench = _symbol(benchmark) if benchmark else None

        def run() -> Dict[str, Any]:
            from ..engine.single import SingleBacktestEngine

            params: Dict[str, Any] = {
                "initial_capital": initial_capital,
                "commission_rate": commission_rate,
                "use_ma": use_ma,
                "use_chandelier": use_chandelier,
                "use_adr": use_adr,
                "trade_start_time": None,
                "trade_end_time": None,
                "benchmark": bench,
                "risk_free_rate": risk_free_rate,
            }
            if use_ma:
                params.update(short_period=ma_short, long_period=ma_long)
            if use_chandelier:
                params.update(chandelier_period=chandelier_period, chandelier_multiplier=chandelier_multiplier)
            if use_adr:
                params.update(adr_period=adr_period, adr_multiplier=adr_multiplier)
            try:
                result = SingleBacktestEngine(self._db()).run(
                    symbol=sym,
                    start_date=dt.datetime.combine(start, dt.time()),
                    end_date=dt.datetime.combine(end, dt.time()),
                    params=params,
                )
            except (TypeError, ValueError) as exc:
                raise RequestError(f"backtest rejected: {exc}") from None
            if not result:
                raise RequestError(f"no cached data for {sym} in the requested range")
            out = {
                "strategy": "dual_ma",
                "universe": [sym],
                "period": {"start": start.isoformat(), "end": end.isoformat()},
                "initial_capital": result["initial_capital"],
                "final_value": result["final_value"],
                "total_return_pct": result["total_return"],
                "metrics": jsonable(result["metrics"]),
                "next_signal": jsonable(result["next_signal"]),
                "data_quality": self._data_quality(list({sym, *([bench] if bench else [])}), start, end),
            }
            out.update(self._trim(result["all_trades"], trade_limit))
            return out

        return self._guarded(run)

    # --------------------------------------------------------------- 注册
    def register(self, mcp: Any, run: Callable[..., Any]) -> None:
        """把三个回测工具挂到 FastMCP 上；``run`` 是在线程里执行同步函数的助手."""

        @mcp.tool()
        async def run_momentum_basket_backtest(
            weights: dict[str, float],
            start_date: str,
            end_date: str | None = None,
            initial_capital: float = 50000,
            commission_rate: float = 0.0001,
            momentum_fn: MomentumFnName = "simple_return",
            lookback: int = 63,
            threshold: float = -0.99,
            schedule: str = "daily",
            cooldown_days: int = 0,
            trend_filter_ma: int | None = None,
            benchmark: str | None = "000300.SS",
            risk_free_rate: float = 0.03,
            trade_limit: int = 100,
        ) -> dict[str, Any]:
            """Backtest a momentum basket (fixed target weights per symbol, filtered by momentum).

            Reads cached daily rows only (call get_price_history first to fill the cache).
            Returns metrics, the last trade_limit trades, next_signal and per-symbol data_quality.
            schedule: daily | every_n:N | weekly:D | weekly_nth:N | monthly:D | monthly_nth:N.
            Requires the backtest scope.
            """
            return await run(
                lambda: self.run_momentum(
                    kind="basket", weights=weights, universe=None, top_n=1, cash_buffer=0.0,
                    start_date=start_date, end_date=end_date, initial_capital=initial_capital,
                    commission_rate=commission_rate, momentum_fn=momentum_fn, lookback=lookback,
                    threshold=threshold, schedule=schedule, cooldown_days=cooldown_days,
                    trend_filter_ma=trend_filter_ma, benchmark=benchmark,
                    risk_free_rate=risk_free_rate, trade_limit=trade_limit,
                )
            )

        @mcp.tool()
        async def run_momentum_topn_backtest(
            universe: list[str],
            start_date: str,
            end_date: str | None = None,
            top_n: int = 1,
            cash_buffer: float = 0.0,
            initial_capital: float = 50000,
            commission_rate: float = 0.0001,
            momentum_fn: MomentumFnName = "simple_return",
            lookback: int = 63,
            threshold: float = -0.99,
            schedule: str = "daily",
            cooldown_days: int = 0,
            trend_filter_ma: int | None = None,
            benchmark: str | None = "000300.SS",
            risk_free_rate: float = 0.03,
            trade_limit: int = 100,
        ) -> dict[str, Any]:
            """Backtest a Top-N equal-weight momentum rotation over a universe of symbols.

            Reads cached daily rows only. Returns metrics, the last trade_limit trades,
            next_signal and per-symbol data_quality. Requires the backtest scope.
            """
            return await run(
                lambda: self.run_momentum(
                    kind="topn", weights=None, universe=universe, top_n=top_n, cash_buffer=cash_buffer,
                    start_date=start_date, end_date=end_date, initial_capital=initial_capital,
                    commission_rate=commission_rate, momentum_fn=momentum_fn, lookback=lookback,
                    threshold=threshold, schedule=schedule, cooldown_days=cooldown_days,
                    trend_filter_ma=trend_filter_ma, benchmark=benchmark,
                    risk_free_rate=risk_free_rate, trade_limit=trade_limit,
                )
            )

        @mcp.tool()
        async def run_dual_ma_backtest(
            symbol: str,
            start_date: str,
            end_date: str | None = None,
            initial_capital: float = 50000,
            commission_rate: float = 0.0001,
            use_ma: bool = True,
            ma_short: int = 5,
            ma_long: int = 20,
            use_chandelier: bool = False,
            chandelier_period: int = 22,
            chandelier_multiplier: float = 3.0,
            use_adr: bool = False,
            adr_period: int = 20,
            adr_multiplier: float = 1.0,
            benchmark: str | None = "000300.SS",
            risk_free_rate: float = 0.03,
            trade_limit: int = 100,
        ) -> dict[str, Any]:
            """Backtest the single-symbol dual moving-average strategy with optional Chandelier/ADR stops.

            Reads cached daily rows only. Returns metrics, the last trade_limit trades,
            next_signal and data_quality. Requires the backtest scope.
            """
            return await run(
                lambda: self.run_dual_ma(
                    symbol=symbol, start_date=start_date, end_date=end_date,
                    initial_capital=initial_capital, commission_rate=commission_rate, use_ma=use_ma,
                    ma_short=ma_short, ma_long=ma_long, use_chandelier=use_chandelier,
                    chandelier_period=chandelier_period, chandelier_multiplier=chandelier_multiplier,
                    use_adr=use_adr, adr_period=adr_period, adr_multiplier=adr_multiplier,
                    benchmark=benchmark, risk_free_rate=risk_free_rate, trade_limit=trade_limit,
                )
            )
