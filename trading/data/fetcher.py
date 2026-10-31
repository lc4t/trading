from datetime import datetime, timedelta
from typing import List, Optional, Any, Dict, Union
from concurrent.futures import ThreadPoolExecutor, as_completed
from argparse import ArgumentParser
from abc import ABC, abstractmethod
from tqdm import tqdm
import tenacity
from loguru import logger
import traceback
from .repository import DBClient
from ..market.clock import as_of_for, is_final_snapshot, utcnow
from ..market.fetchers import SYNC_RETRY, FetchResult, RangePriceFetcher, ths_etf_rows
from ..market.store import MarketStore
import argparse
import adata  # noqa: F401  (保留模块级属性，测试按 trading.data.fetcher.adata 打补丁)
import pandas as pd
import sys
import time
from sqlalchemy.exc import SQLAlchemyError


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
    def __init__(self, max_workers: int = 1, max_retries: int = 5):
        self.db_client = DBClient()
        self.max_workers = max_workers
        self.max_retries = max_retries
        # 抓取 / 落库统一走 trading.market：同一套路由、口径判定与元数据
        self.market_fetcher = RangePriceFetcher(retry=SYNC_RETRY)
        self.market_store = MarketStore(self.db_client.engine)
        self.market_store.ensure_meta_tables()
        self._results: Dict[str, FetchResult] = {}

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
        data = result.rows

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
        return written > 0

    def fetch_data(
        self,
        symbol: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        db_client: Optional[DBClient] = None,
    ) -> bool:
        """获取股票数据"""
        if db_client is None:
            db_client = DBClient()

        try:
            # 准备任务列表
            tasks = []
            for sym in [symbol]:
                sym_start_date = start_date
                if not sym_start_date:
                    # 优先使用 listing_date
                    symbol_info = db_client.get_symbol_info(sym)
                    if symbol_info and symbol_info.get("listing_date"):
                        sym_start_date = symbol_info["listing_date"]
                    else:
                        # 如果没有 listing_date，则使用最新数据日期
                        latest_date = db_client.get_latest_date(sym)
                        sym_start_date = (
                            (latest_date + timedelta(days=1))
                            if latest_date
                            else (end_date - timedelta(days=30))
                        )
                        logger.warning(
                            f"未找到 {sym} 的上市日期，使用 {sym_start_date} 作为起始日期"
                        )

                tasks.append((sym, sym_start_date, end_date))

            # 使用线程池并发获取数据
            success = True
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(self._fetch_single_stock_data, sym, start, end): sym
                    for sym, start, end in tasks
                }

                with tqdm(total=len(tasks), desc="Fetching data") as pbar:
                    for future in as_completed(futures):
                        symbol = futures[future]
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

                                if trading_days_diff > 2:
                                    # 如果数据延迟超过2个交易日，视为失败
                                    success = False
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

                                if not self._persist(symbol, data):
                                    success = False
                                    logger.error(f"Failed to save data for {symbol}")
                            else:
                                success = False
                                logger.warning(f"No data fetched for {symbol}")
                        except Exception as e:
                            success = False
                            logger.error(f"Error processing {symbol}: {e}")
                        finally:
                            pbar.update(1)

            return success
        except Exception as e:
            logger.error(f"Error fetching data: {e}")
            return False


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

    args = parser.parse_args()

    # 处理日期格式
    if args.start_date:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
    else:
        start_date = None

    # 结束日期默认使用今天
    end_date = datetime.now().date()
    if args.end_date:
        end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date()
        if end_date > datetime.now().date():
            logger.warning("结束日期超过今天，将使用今天作为结束日期")
            end_date = datetime.now().date()

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

    manager = StockDataManager(max_workers=args.workers, max_retries=args.retries)

    # 记录总体成功状态
    overall_success = True

    # 更新每个股票的数据
    for symbol in symbols:
        logger.info(f"\n开始更新 {symbol} 的数据...")
        success = manager.fetch_data(
            symbol=symbol, start_date=start_date, end_date=end_date, db_client=db_client
        )
        if not success:
            overall_success = False
            logger.error(f"{symbol} 数据更新失败")

    # 关闭数据库连接
    db_client.close()

    if overall_success:
        logger.info("所有数据更新完成")
    else:
        logger.error("部分数据更新失败")
        sys.exit(1)


if __name__ == "__main__":
    main()
