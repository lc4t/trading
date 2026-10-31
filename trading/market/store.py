"""trading_data + 元数据旁表的统一读写层.

回测引擎取数、tt-fetcher 同步、MCP 服务三者都经过这里。``trading_data`` 的列语义不变；
元数据（价格口径 / 来源 / 数据时点 / 终值标记）写在 ``market_data_meta`` 旁表。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence

import pandas as pd
from loguru import logger
from sqlalchemy import and_, func, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ..data.repository import Base, TradingData
from .clock import business_naive_now
from .errors import ReadOnlyError
from .rows import PRICE_FIELDS, clean_rows
from .models import (
    META_TABLES,
    STABLE_BASES,
    MarketCorporateAction,
    MarketDataMeta,
)

_QUANT = Decimal(f"0.{'0' * TradingData.STORAGE_SCALE}")
EPOCH = dt.datetime(1970, 1, 1)
# MySQL：1044/1142/1143/1227 = 库/表/列/操作权限不足，1290 = 服务器 --read-only
PRIVILEGE_ERRNOS = frozenset({1044, 1142, 1143, 1227, 1290, 1370})


def is_privilege_error(exc: BaseException) -> bool:
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", ())
    return bool(args) and isinstance(args[0], int) and args[0] in PRIVILEGE_ERRNOS


def _quantize(row: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(row)
    for field in PRICE_FIELDS:
        if field in out:
            out[field] = Decimal(str(out[field])).quantize(_QUANT)
    return out


def _insert_for(engine: Engine):
    dialect = engine.dialect.name
    if dialect == "mysql":
        from sqlalchemy.dialects.mysql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover
        raise NotImplementedError(f"不支持的数据库方言: {dialect}")
    return insert


class MarketStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.Session = sessionmaker(bind=engine)
        self._insert = _insert_for(engine)
        # 旁表建不出来（例如 DB 账号没有 DDL 权限）时降级为只写 trading_data，
        # 保证 tt-fetcher 每日同步不受影响；MCP 服务启动时则要求必须可用。
        self.meta_enabled = True
        # 只读：账号没有写权限（显式配置或运行时探测到）。读照常，写一律拒绝
        self.read_only = False

    # ------------------------------------------------------------------ schema
    def configure_read_only(self) -> None:
        """进入只读模式：不建表、不写库。旁表能读就继续用，读不了就退回遗留行推断."""
        self.read_only = True
        self.meta_enabled = True
        try:
            with self.engine.connect() as conn:
                for table in META_TABLES:
                    conn.execute(text(f"SELECT 1 FROM {table.name} LIMIT 1"))
        except SQLAlchemyError:
            self.meta_enabled = False

    def _writing(self):
        """写操作的统一入口：只读直接拒绝；执行中遇到权限错误则转为只读."""
        if self.read_only:
            raise ReadOnlyError("database is read-only")
        return _WriteGuard(self)

    def init_schema(self) -> None:
        """建表（含元数据旁表），失败直接抛出——MCP 服务启动用."""
        Base.metadata.create_all(self.engine)

    def ensure_meta_tables(self) -> bool:
        """尽力建旁表；失败则关闭元数据功能并返回 False（tt-fetcher 用）."""
        try:
            for table in META_TABLES:
                table.create(self.engine, checkfirst=True)
            self.meta_enabled = True
        except SQLAlchemyError as exc:
            logger.warning(f"无法创建元数据旁表（{type(exc).__name__}），本次同步只写 trading_data")
            self.meta_enabled = False
        return self.meta_enabled

    def ping(self) -> None:
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    # ------------------------------------------------------------------ reads
    @staticmethod
    def _row_dict(data: TradingData, meta: Optional[MarketDataMeta]) -> Dict[str, Any]:
        return {
            "symbol": data.symbol,
            "date": data.date,
            "open_price": float(data.open_price),
            "close_price": float(data.close_price),
            "high": float(data.high),
            "low": float(data.low),
            "volume": data.volume,
            "amount": float(data.amount) if data.amount is not None else None,
            "change": float(data.change) if data.change is not None else None,
            "change_pct": float(data.change_pct) if data.change_pct is not None else None,
            "create_time": data.create_time,
            "update_time": data.update_time,
            "meta": None
            if meta is None
            else {
                "price_basis": meta.price_basis,
                "source": meta.source,
                "as_of": meta.as_of,
                "fetched_at": meta.fetched_at,
                "is_final": bool(meta.is_final),
            },
        }

    def _joined_query(self, session: Session, symbol: str):
        return (
            session.query(TradingData, MarketDataMeta)
            .outerjoin(
                MarketDataMeta,
                and_(
                    MarketDataMeta.symbol == TradingData.symbol,
                    MarketDataMeta.date == TradingData.date,
                ),
            )
            .filter(TradingData.symbol == symbol)
        )

    def query_range(self, symbol: str, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
        with self.Session() as session:
            if self.meta_enabled:
                query = (
                    self._joined_query(session, symbol)
                    .filter(TradingData.date.between(start, end))
                    .order_by(TradingData.date)
                )
                return [self._row_dict(d, m) for d, m in query.all()]
            query = (
                session.query(TradingData)
                .filter(TradingData.symbol == symbol, TradingData.date.between(start, end))
                .order_by(TradingData.date)
            )
            return [self._row_dict(d, None) for d in query.all()]

    def query_latest(self, symbol: str) -> Optional[Dict[str, Any]]:
        with self.Session() as session:
            if self.meta_enabled:
                pair = (
                    self._joined_query(session, symbol)
                    .order_by(TradingData.date.desc())
                    .first()
                )
                return self._row_dict(*pair) if pair else None
            data = (
                session.query(TradingData)
                .filter(TradingData.symbol == symbol)
                .order_by(TradingData.date.desc())
                .first()
            )
            return self._row_dict(data, None) if data else None

    def first_date(self, symbol: str) -> Optional[dt.date]:
        with self.Session() as session:
            return session.query(func.min(TradingData.date)).filter(
                TradingData.symbol == symbol
            ).scalar()

    def series_mode(self, symbol: str) -> str:
        """该标的下一次写入应使用哪种抓取口径.

        - ``raw``：没有历史行，或历史行全部是不随分红改写的口径（raw / split_adjusted）。
        - ``legacy``：存在遗留行（无元数据）或前复权/Yahoo 调整口径的行；沿用与历史一致的
          抓取方式，避免同一条序列里混入两种价格口径。
        """
        with self.Session() as session:
            total = session.query(func.count()).select_from(TradingData).filter(
                TradingData.symbol == symbol
            ).scalar()
            if not total:
                return "raw"
            if not self.meta_enabled:
                return "legacy"
            stable = (
                session.query(func.count())
                .select_from(TradingData)
                .join(
                    MarketDataMeta,
                    and_(
                        MarketDataMeta.symbol == TradingData.symbol,
                        MarketDataMeta.date == TradingData.date,
                    ),
                )
                .filter(
                    TradingData.symbol == symbol,
                    MarketDataMeta.price_basis.in_(sorted(STABLE_BASES)),
                )
                .scalar()
            )
            return "raw" if stable == total else "legacy"

    def ohlcv_frame(self, symbol: str, start: Any, end: Any) -> pd.DataFrame:
        """回测引擎取数：与历史上 ``fetch_symbol_data`` 返回的 DataFrame 完全一致."""
        with self.Session() as session:
            df = pd.read_sql(
                session.query(TradingData)
                .filter(
                    TradingData.symbol == symbol,
                    TradingData.date >= start,
                    TradingData.date <= end,
                )
                .statement,
                session.bind,
            )
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"])
        for col in ("open_price", "close_price", "high", "low"):
            df[col] = df[col].astype(float)
        df["volume"] = df["volume"].astype(float)
        return df

    def close_series(self, symbol: str, start: Any, end: Any) -> List[Any]:
        """基准日收益用：[(date, close_price)]，按日期升序."""
        with self.Session() as session:
            rows = (
                session.query(TradingData)
                .filter(
                    TradingData.symbol == symbol,
                    TradingData.date >= start,
                    TradingData.date <= end,
                )
                .order_by(TradingData.date)
                .all()
            )
            return [(r.date, float(r.close_price)) for r in rows]

    # ----------------------------------------------------------------- writes
    def upsert(
        self,
        rows: Sequence[Dict[str, Any]],
        *,
        price_basis: Optional[str] = None,
        source: Optional[str] = None,
        fetched_at: Optional[dt.datetime] = None,
        final_flags: Optional[Dict[dt.date, bool]] = None,
        as_of: Optional[Dict[dt.date, dt.datetime]] = None,
    ) -> int:
        """写入 trading_data（语义同历史的 ON DUPLICATE KEY UPDATE），并在同一事务里写元数据.

        ``price_basis`` 为 None 时只写 trading_data（元数据降级模式）。
        返回实际写入的行数。
        """
        if self.read_only:
            raise ReadOnlyError("database is read-only")
        cleaned = clean_rows(rows)
        if not cleaned:
            return 0
        now_local = business_naive_now()
        values = []
        for row in cleaned:
            item = _quantize(row)
            item = {k: v for k, v in item.items() if k not in {"create_time", "update_time", "meta"}}
            item["create_time"] = now_local
            item["update_time"] = now_local
            values.append(item)

        table = TradingData.__table__
        stmt = self._insert(table).values(values)
        # 方言差异：MySQL 用 inserted，SQLite 用 excluded
        ref = stmt.excluded if self.engine.dialect.name == "sqlite" else stmt.inserted
        update_cols = {
            "open_price": ref.open_price,
            "close_price": ref.close_price,
            "high": ref.high,
            "low": ref.low,
            "volume": ref.volume,
            "amount": ref.amount,
            "change": ref.change,
            "change_pct": ref.change_pct,
            "update_time": now_local,
        }
        stmt = self._apply_conflict(stmt, update_cols, ["symbol", "date"])

        with self._writing(), self.Session() as session:
            session.execute(stmt)
            if price_basis is not None and self.meta_enabled:
                fetched = fetched_at or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
                meta_values = []
                for row in cleaned:
                    day = row["date"]
                    meta_values.append(
                        {
                            "symbol": row["symbol"],
                            "date": day,
                            "price_basis": price_basis,
                            "source": source,
                            "as_of": (as_of or {}).get(day),
                            "fetched_at": fetched,
                            "is_final": bool((final_flags or {}).get(day, False)),
                        }
                    )
                mstmt = self._insert(MarketDataMeta.__table__).values(meta_values)
                mref = mstmt.excluded if self.engine.dialect.name == "sqlite" else mstmt.inserted
                mstmt = self._apply_conflict(
                    mstmt,
                    {
                        "price_basis": mref.price_basis,
                        "source": mref.source,
                        "as_of": mref.as_of,
                        "fetched_at": mref.fetched_at,
                        "is_final": mref.is_final,
                    },
                    ["symbol", "date"],
                )
                session.execute(mstmt)
            session.commit()
        return len(cleaned)

    def _apply_conflict(self, stmt, update_cols: Dict[str, Any], index_elements: List[str]):
        if self.engine.dialect.name == "sqlite":
            return stmt.on_conflict_do_update(index_elements=index_elements, set_=update_cols)
        return stmt.on_duplicate_key_update(**update_cols)

    def invalidate_before(self, symbol: str, before: dt.date) -> int:
        """让 ``before`` 之前的已缓存行失效：标记为非终值，下次读取会触发重抓.

        没有元数据的遗留行不会被改动（遗留序列不走拆分检测）。
        """
        if not self.meta_enabled:
            return 0
        with self._writing(), self.Session() as session:
            result = session.execute(
                update(MarketDataMeta)
                .where(MarketDataMeta.symbol == symbol, MarketDataMeta.date < before)
                .values(is_final=False, fetched_at=EPOCH)
            )
            session.commit()
            return result.rowcount or 0

    # ---------------------------------------------------------- corporate actions
    def record_actions(
        self, symbol: str, actions: Iterable[Dict[str, Any]], detected_at: dt.datetime
    ) -> List[Dict[str, Any]]:
        """记录分红/拆分事件，返回其中此前未见过的新事件."""
        actions = list(actions)
        if not actions or not self.meta_enabled:
            return []
        new: List[Dict[str, Any]] = []
        with self._writing(), self.Session() as session:
            known = {
                (a.ex_date, a.kind)
                for a in session.query(MarketCorporateAction).filter(
                    MarketCorporateAction.symbol == symbol
                )
            }
            for action in actions:
                key = (action["ex_date"], action["kind"])
                if key in known:
                    continue
                known.add(key)
                new.append(action)
                session.add(
                    MarketCorporateAction(
                        symbol=symbol,
                        ex_date=action["ex_date"],
                        kind=action["kind"],
                        value=Decimal(str(action["value"])),
                        detected_at=detected_at,
                    )
                )
            session.commit()
        return new

    def list_actions(self, symbol: str) -> List[Dict[str, Any]]:
        if not self.meta_enabled:
            return []
        with self.Session() as session:
            return [
                {"ex_date": a.ex_date, "kind": a.kind, "value": float(a.value)}
                for a in session.query(MarketCorporateAction)
                .filter(MarketCorporateAction.symbol == symbol)
                .order_by(MarketCorporateAction.ex_date)
            ]

    def close(self) -> None:
        self.engine.dispose()


class _WriteGuard:
    def __init__(self, store: "MarketStore") -> None:
        self.store = store

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        if isinstance(exc, DBAPIError) and is_privilege_error(exc):
            self.store.read_only = True
            logger.warning("数据库账号没有写权限，切换为只读模式")
            raise ReadOnlyError("database is read-only") from None
        return False
