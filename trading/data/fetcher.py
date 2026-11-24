from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Callable, Iterable, List, Optional, Any, Dict, Union
from concurrent.futures import ThreadPoolExecutor, as_completed
from argparse import ArgumentParser
from abc import ABC, abstractmethod
from tqdm import tqdm
import tenacity
from loguru import logger
import traceback
from .repository import DBClient
from ..market import corporate
from ..market.clock import (
    as_of_for,
    business_today,
    completed_session_end,
    is_final_snapshot,
    utcnow,
)
from ..market.fetchers import INDEX_ROUTES, SYNC_RETRY, FetchResult, RangePriceFetcher, ths_etf_rows
from ..market.reconcile import Finding, reconcile_rows
from ..market.store import MarketStore
import argparse
import adata  # noqa: F401  (保留模块级属性，测试按 trading.data.fetcher.adata 打补丁)
import pandas as pd
import sys
import time
from sqlalchemy.exc import SQLAlchemyError

# 基准（只用于算 Beta 等参考指标，不参与交易信号）。取不到新数据时只降级，不拖垮整条链路。
BENCHMARK_SYMBOLS = frozenset({"000300.SS", "000300.SH", "^HSI"})
# 最新数据距请求结束日超过这么多个交易日，就认为「不够新」：核心标的视为致命，基准仅告警
MAX_STALE_TRADING_DAYS = 2


class FetchStatus(str, Enum):
    OK = "ok"  # 拿到足够新的数据并写库
    DEGRADED = "degraded"  # 没拿到新数据（或不够新），但可降级继续：缓存可用，或只是基准
    FATAL = "fatal"  # 核心交易标的没有可用数据，或写库失败


@dataclass(frozen=True)
class FetchOutcome:
    symbol: str
    status: FetchStatus
    reason: str = ""

    @property
    def fatal(self) -> bool:
        return self.status is FetchStatus.FATAL


def _as_date(value: Any) -> date:
    return value.date() if isinstance(value, datetime) else value


class DataFetcher(ABC):
    """数据获取基类"""

    @abstractmethod
    def fetch_data(
        self,
        symbol: str,
        start_date: Union[str, datetime],
        end_date: Union[str, datetime],
    ) -> List[Dict[str, Any]]:
        """获取股票数据的抽象方法"""
        pass


class YFinanceFetcher(DataFetcher):
    """YFinance 数据获取（历史口径：auto_adjust=True）.

    实现已统一到 ``trading.market.fetchers``；这里只保留向后兼容的类名与
    ``fetch_data`` 签名。限流沿用批量同步的长退避（SYNC_RETRY）。
    """

    def fetch_data(
        self,
        symbol: str,
        start_date: Union[str, datetime],
        end_date: Union[str, datetime],
    ) -> List[Dict[str, Any]]:
        try:
            return RangePriceFetcher(retry=SYNC_RETRY).fetch_yahoo(
                symbol, start_date, end_date, retry=SYNC_RETRY, legacy=True
            ).rows
        except Exception as e:
            logger.error(f"Error fetching data from YFinance: {e}")
            traceback.print_exc()
            return []


class ADataFetcher(DataFetcher):
    """AData（同花顺）ETF 数据获取，前复权全量历史（忽略区间）."""

    def fetch_data(
        self,
        symbol: str,
        start_date: Union[str, datetime],
        end_date: Union[str, datetime],
    ) -> List[Dict[str, Any]]:
        try:
            logger.info(f"正在获取 {symbol} 的数据 ({start_date} 至 {end_date})")
            return ths_etf_rows(symbol)
        except Exception as e:
            logger.error(f"Error fetching data from AData: {e}")
            traceback.print_exc()
            return []


class StockDataManager:
    def __init__(
        self,
        max_workers: int = 1,
        max_retries: int = 5,
        benchmark_symbols: Optional[Iterable[str]] = None,
        now: Callable[[], datetime] = utcnow,
    ):
        self._now = now  # naive UTC；测试里可注入固定时刻
        self.benchmark_symbols = frozenset(
            BENCHMARK_SYMBOLS if benchmark_symbols is None else benchmark_symbols
        )
        self.db_client = DBClient()
        self.max_workers = max_workers
        self.max_retries = max_retries
        # 抓取 / 落库统一走 trading.market：同一套路由、口径判定与元数据
        self.market_fetcher = RangePriceFetcher(retry=SYNC_RETRY)
        self.market_store = MarketStore(self.db_client.engine)
        self.market_store.ensure_meta_tables()
        self._results: Dict[str, FetchResult] = {}
        self._pending_actions: Dict[str, List[Dict[str, Any]]] = {}

    @staticmethod
    def count_trading_days(start_date, end_date) -> int:
        """
        计算两个日期之间的工作日数量（使用 pandas 排除周末）
        注意：不考虑节假日，只排除周六和周日
        """
        if start_date >= end_date:
            return 0

        # 使用 pandas 的 bdate_range 计算工作日（自动排除周末）
        business_days = pd.bdate_range(start=start_date, end=end_date, freq='B')
        # bdate_range 包含结束日期，我们需要排除它（因为我们计算的是"之间"的天数）
        return len(business_days) - 1 if len(business_days) > 0 else 0

    @tenacity.retry(
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_exponential(multiplier=1, min=4, max=10),
        retry=tenacity.retry_if_exception_type(Exception),
        before_sleep=lambda retry_state: logger.warning(
            f"Retrying {retry_state.attempt_number}/3 after error: {retry_state.outcome.exception()}"
        ),
    )
    def _fetch_single_stock_data(
        self, symbol: str, start_date: datetime, end_date: datetime
    ) -> List[Dict[str, Any]]:
        """获取单个股票的数据，带重试机制"""
        logger.info(f"正在获取 {symbol} 的数据 ({start_date} 至 {end_date})")
        # 路由由 trading.market.fetchers 决定：已有历史行的标的沿用历史口径抓取
        # （A 股 ETF → 同花顺前复权全量，其余 → Yahoo auto_adjust），新标的走不复权口径。
        mode = self.market_store.series_mode(symbol)
        result = self.market_fetcher.fetch(
            symbol, start_date, end_date, mode=mode, retry=SYNC_RETRY
        )
        self._results[symbol] = result
        if result.price_basis in ("yahoo_adjusted", "split_adjusted"):
            # 复权口径下，新的分红/拆股会让此前所有行整体平移；只重写窗口会在窗口起点留下接缝，
            # 所以检测到新的公司行动时，把该标的整段历史一起重写（首次检测只建基线，不触发）
            verdict = corporate.assess(self.market_store, symbol, result, utcnow())
            first = self.market_store.first_date(symbol)
            window_start = start_date.date() if isinstance(start_date, datetime) else start_date
            if verdict.trigger and first is not None and first < window_start:
                logger.warning(
                    f"{symbol} 检测到新的公司行动 {[(a['kind'], str(a['ex_date'])) for a in verdict.new]}，"
                    f"整段历史重写（{first} 至 {end_date}）"
                )
                result = self.market_fetcher.fetch(symbol, first, end_date, mode=mode, retry=SYNC_RETRY)
                result.actions = verdict.new
                self._results[symbol] = result
            self._pending_actions[symbol] = verdict.new
        # 只保留已完成日线：有的上游（同花顺全量历史）不看 end，可能带出当天盘中那一根
        data = [r for r in result.rows if r["date"] <= _as_date(end_date)]
        if len(data) != len(result.rows):
            logger.info(
                f"{symbol} 丢弃 {len(result.rows) - len(data)} 行晚于 {end_date} 的数据（当日尚未收盘）"
            )

        if data:
            # 获取实际数据的时间范围
            actual_dates = [record["date"] for record in data]
            actual_start = min(actual_dates)
            actual_end = max(actual_dates)
            logger.info(
                f"成功获取 {symbol} 的数据：\n"
                f"  请求范围：{start_date} 至 {end_date}\n"
                f"  实际范围：{actual_start} 至 {actual_end}\n"
                f"  数据条数：{len(data)}"
            )
        else:
            logger.warning(
                f"未获取到 {symbol} 在 {start_date} 至 {end_date} 期间的数据"
            )

        return data

    def _persist(self, symbol: str, rows: List[Dict[str, Any]]) -> bool:
        """写 trading_data（语义与历史 upsert 一致）并记录价格口径等元数据."""
        result = self._results.get(symbol)
        fetched_at = utcnow()
        finals = {r["date"]: is_final_snapshot(symbol, r["date"], fetched_at) for r in rows}
        as_of = {d: as_of_for(symbol, d, fetched_at, f) for d, f in finals.items()}
        try:
            written = self.market_store.upsert(
                rows,
                price_basis=result.price_basis if result else None,
                source=result.source if result else None,
                fetched_at=fetched_at,
                final_flags=finals,
                as_of=as_of,
            )
        except SQLAlchemyError as e:
            logger.error(f"Error upserting data: {e}")
            return False
        if not written:
            logger.error("清洗后没有可写入的数据（全部记录都缺失开高低收）")
        else:
            # 写成功才记录新事件：写失败的话下次同步仍会判定为「新」，继续重试整段重写
            corporate.commit(self.market_store, symbol, self._pending_actions.pop(symbol, []), utcnow())
        return written > 0

    def _reconcile(
        self, symbol: str, rows: List[Dict[str, Any]], rejected: List[Finding]
    ) -> List[Dict[str, Any]]:
        """指数（不复权）新抓到的收盘价与库里已完成交易日对账；超过 0.5% 的行不覆盖，登记到 ``rejected``."""
        if symbol not in INDEX_ROUTES or not rows:
            return rows
        try:
            stored = self.market_store.query_range(
                symbol, min(r["date"] for r in rows), max(r["date"] for r in rows)
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"{symbol} 对账读取库内数据失败，跳过对账：{e}")
            return rows
        kept, findings = reconcile_rows(rows, stored)
        for item in findings:
            if item.level == "error":
                logger.error(f"{symbol} 对账失败（>0.5%，不覆盖）：{item.describe()}")
                rejected.append(item)
            else:
                logger.warning(f"{symbol} 对账差异（>0.05%，仍写入新值）：{item.describe()}")
        return kept

    def _cached_latest_date(self, symbol: str, end_date: Any) -> Optional[date]:
        """数据库里该标的、不晚于 ``end_date`` 的最新一行的日期；读失败按「没有缓存」处理.

        不晚于 ``end_date`` = 不把盘中写入的当天半根当成可用缓存。
        """
        try:
            row = self.market_store.query_latest(symbol, before=_as_date(end_date) + timedelta(days=1))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"读取 {symbol} 的缓存数据失败：{e}")
            return None
        return _as_date(row["date"]) if row else None

    def _classify(
        self,
        symbol: str,
        end_date: Any,
        *,
        fetch_failed: bool,
        stale_days: int,
        persist_failed: bool,
        detail: str,
    ) -> FetchOutcome:
        """把一次同步的结果归为 OK / DEGRADED / FATAL.

        - 写库失败：一律致命（库不可写，后面的信号也不可信）。
        - 取到数据且够新：OK。
        - 取不到（空或抛错）：缓存够新则沿用缓存（可降级）；缓存缺失/过旧时，基准降级、核心标的致命。
        - 取到了但不够新：基准降级，核心标的致命（与此前行为一致）。
        """
        is_benchmark = symbol in self.benchmark_symbols
        if persist_failed:
            return FetchOutcome(symbol, FetchStatus.FATAL, detail or "写库失败")
        if not fetch_failed and stale_days <= MAX_STALE_TRADING_DAYS:
            return FetchOutcome(symbol, FetchStatus.OK)

        if fetch_failed:
            cached = self._cached_latest_date(symbol, end_date)
            end_day = _as_date(end_date)
            cached_age = self.count_trading_days(cached, end_day) if cached else None
            if cached is not None and cached_age <= MAX_STALE_TRADING_DAYS:
                reason = (
                    f"{symbol} 本次未取到新数据（{detail}），沿用数据库缓存，最新日期 {cached}"
                    f"（距 {end_day} 约 {cached_age} 个交易日）"
                )
                return FetchOutcome(symbol, FetchStatus.DEGRADED, reason)
            have = f"缓存最新日期 {cached} 已过旧" if cached else "数据库也没有缓存"
            reason = f"{symbol} 没有可用的新数据（{detail}；{have}）"
        else:
            reason = f"{symbol} 数据过旧（距请求结束日 {stale_days} 个交易日）"

        if is_benchmark:
            return FetchOutcome(
                symbol, FetchStatus.DEGRADED, f"{reason}；{symbol} 仅作基准，继续后续流程（相关指标可能缺失或滞后）"
            )
        return FetchOutcome(symbol, FetchStatus.FATAL, reason)

    def sync_symbol(
        self,
        symbol: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        db_client: Optional[DBClient] = None,
    ) -> FetchOutcome:
        """同步单个标的并给出分级结果（OK / DEGRADED / FATAL）。"""
        if db_client is None:
            db_client = DBClient()

        fetch_failed = False
        persist_failed = False
        stale_days = 0
        detail = ""
        reconcile_errors: List[Finding] = []
        # 只用已完成的日线：按标的所属市场的收盘时间，把结束日限制到最近一个已定稿的交易日
        requested_end = _as_date(end_date)
        end_date = completed_session_end(symbol, requested_end, self._now())
        if end_date != requested_end:
            logger.info(
                f"{symbol} 结束日期 {requested_end} → {end_date}（只同步已完成的日线，不请求当日盘中数据）"
            )
        try:
            sym_start_date = start_date
            if not sym_start_date:
                # 优先使用 listing_date
                symbol_info = db_client.get_symbol_info(symbol)
                if symbol_info and symbol_info.get("listing_date"):
                    sym_start_date = symbol_info["listing_date"]
                else:
                    # 如果没有 listing_date，则使用最新数据日期
                    latest_date = db_client.get_latest_date(symbol)
                    sym_start_date = (
                        (latest_date + timedelta(days=1))
                        if latest_date
                        else (end_date - timedelta(days=30))
                    )
                    logger.warning(
                        f"未找到 {symbol} 的上市日期，使用 {sym_start_date} 作为起始日期"
                    )

            if _as_date(sym_start_date) > end_date:
                logger.info(f"{symbol} 起始日期 {sym_start_date} 晚于 {end_date}，没有新的已完成日线需要同步")
                return FetchOutcome(symbol, FetchStatus.OK)

            # 使用线程池并发获取数据
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(
                        self._fetch_single_stock_data, symbol, sym_start_date, end_date
                    ): symbol
                }

                with tqdm(total=len(futures), desc="Fetching data") as pbar:
                    for future in as_completed(futures):
                        try:
                            data = future.result()
                            if data:
                                # 输出最新一条数据的信息
                                latest_record = max(data, key=lambda x: x["date"])
                                logger.info(
                                    f"Symbol: {symbol} | "
                                    f"Latest Date: {latest_record['date']} | "
                                    f"Open: {latest_record['open_price']:.4f} | "
                                    f"Close: {latest_record['close_price']:.4f} | "
                                    f"High: {latest_record['high']:.4f} | "
                                    f"Low: {latest_record['low']:.4f} | "
                                    f"Volume: {latest_record['volume']:,}"
                                )

                                # 检查获取到的最新数据日期是否合理
                                # 如果请求的结束日期是今天或未来，而获取到的数据日期稍早，这是正常的
                                # （可能是还没开盘或数据还没更新）
                                latest_data_date = latest_record['date']
                                natural_days_diff = (end_date - latest_data_date).days
                                trading_days_diff = self.count_trading_days(latest_data_date, end_date)

                                if trading_days_diff > MAX_STALE_TRADING_DAYS:
                                    # 数据延迟超过 2 个交易日，视为不够新
                                    stale_days = trading_days_diff
                                    logger.error(
                                        f"{symbol} 数据过旧：最新数据日期为 {latest_data_date}，"
                                        f"距离请求的结束日期 {end_date} 已有 {trading_days_diff} 个交易日"
                                        f"（自然日：{natural_days_diff} 天）"
                                    )
                                elif trading_days_diff > 0:
                                    # 如果数据延迟在1-2个交易日内，记录信息但不视为失败
                                    logger.info(
                                        f"{symbol} 最新数据日期为 {latest_data_date}，"
                                        f"距离请求的结束日期 {end_date} 有 {trading_days_diff} 个交易日"
                                        f"（自然日：{natural_days_diff} 天），"
                                        f"可能是还没开盘或数据未更新"
                                    )

                                to_write = self._reconcile(symbol, data, reconcile_errors)
                                if to_write and not self._persist(symbol, to_write):
                                    persist_failed = True
                                    detail = "写库失败"
                                    logger.error(f"Failed to save data for {symbol}")
                            else:
                                # 盘中请求「到今天」时上游常常还没有当日数据，这里不直接判失败，
                                # 交给 _classify 看缓存是否够用
                                fetch_failed = True
                                detail = "上游返回空数据"
                                logger.warning(f"No data fetched for {symbol}")
                        except Exception as e:
                            fetch_failed = True
                            detail = f"取数异常 {type(e).__name__}"
                            logger.error(f"Error processing {symbol}: {e}")
                        finally:
                            pbar.update(1)
        except Exception as e:
            logger.error(f"Error fetching data: {e}")
            return FetchOutcome(symbol, FetchStatus.FATAL, f"同步异常 {type(e).__name__}: {e}")

        outcome = self._classify(
            symbol,
            end_date,
            fetch_failed=fetch_failed,
            stale_days=stale_days,
            persist_failed=persist_failed,
            detail=detail,
        )
        if reconcile_errors and outcome.status is FetchStatus.OK:
            shown = "；".join(item.describe() for item in reconcile_errors[:3])
            more = "" if len(reconcile_errors) <= 3 else f" 等 {len(reconcile_errors)} 行"
            outcome = FetchOutcome(
                symbol, FetchStatus.DEGRADED, f"{symbol} 对账未通过，未覆盖库内已有的收盘价：{shown}{more}"
            )
        return outcome

    def fetch_data(
        self,
        symbol: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        db_client: Optional[DBClient] = None,
    ) -> bool:
        """获取股票数据；返回 False 仅表示致命失败，可降级（沿用缓存 / 仅基准）返回 True。"""
        return not self.sync_symbol(symbol, start_date, end_date, db_client).fatal


def main():
    """命令行入口"""
    parser = argparse.ArgumentParser(description="股票数据获取工具")
    parser.add_argument("--symbol", help="股票代码，不指定则更新所有股票", default=None)
    parser.add_argument("--start-date", help="开始日期 (YYYY-MM-DD)", default=None)
    parser.add_argument(
        "--end-date", help="结束日期 (YYYY-MM-DD)，默认为今天", default=None
    )
    parser.add_argument("--workers", help="并发数", type=int, default=5)
    parser.add_argument("--retries", help="重试次数", type=int, default=3)
    parser.add_argument(
        "--benchmark-symbols",
        help="基准代码（逗号分隔）：取不到新数据时只告警、不让进程失败；默认 "
        + ",".join(sorted(BENCHMARK_SYMBOLS)),
        default=None,
    )

    args = parser.parse_args()

    # 处理日期格式
    if args.start_date:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    else:
        start_date = None

    # 结束日期默认使用今天
    # 业务日（Asia/Shanghai）；各标的实际结束日由 sync_symbol 按所属市场收盘时间再收紧
    today = business_today()
    end_date = today
    if args.end_date:
        end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date()
        if end_date > today:
            logger.warning("结束日期超过今天，将使用今天作为结束日期")
            end_date = today

    # 创建数据库客户端
    db_client = DBClient()

    # 获取要更新的股票列表
    if args.symbol:
        symbols = [args.symbol]
    else:
        symbols = db_client.get_active_symbols()
        if not symbols:
            logger.error("数据库中没有找到活跃的股票")
            return
        logger.info(f"找到 {len(symbols)} 个活跃股票需要更新")

    benchmark_symbols = (
        [item.strip() for item in args.benchmark_symbols.split(",") if item.strip()]
        if args.benchmark_symbols is not None
        else None
    )
    manager = StockDataManager(
        max_workers=args.workers, max_retries=args.retries, benchmark_symbols=benchmark_symbols
    )

    # 分级结果：致命 → 退出码 1；可降级（沿用缓存 / 仅基准）→ WARNING，退出码 0
    fatal: List[FetchOutcome] = []
    degraded: List[FetchOutcome] = []

    # 更新每个股票的数据
    for symbol in symbols:
        logger.info(f"\n开始更新 {symbol} 的数据...")
        outcome = manager.sync_symbol(
            symbol=symbol, start_date=start_date, end_date=end_date, db_client=db_client
        )
        if outcome.status is FetchStatus.FATAL:
            fatal.append(outcome)
            logger.error(f"{symbol} 数据更新失败：{outcome.reason}")
        elif outcome.status is FetchStatus.DEGRADED:
            degraded.append(outcome)
            logger.warning(f"{symbol} 数据更新降级：{outcome.reason}")

    # 关闭数据库连接
    db_client.close()

    if degraded:
        logger.warning(
            "以下标的降级处理（不影响退出码）：" + "；".join(o.reason for o in degraded)
        )
    if fatal:
        logger.error("部分数据更新失败：" + "；".join(o.reason for o in fatal))
        sys.exit(1)
    logger.info("所有数据更新完成" if not degraded else "数据更新完成（含降级标的，见上方告警）")


if __name__ == "__main__":
    main()
