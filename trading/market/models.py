"""行情缓存的元数据旁表.

``trading_data`` 的列语义保持不变（回测、前端都在用）。数据时点、终值标记、价格口径
等信息放在旁表里，主键与 ``trading_data`` 对齐。
"""
from __future__ import annotations

from sqlalchemy import DATETIME, DECIMAL, Boolean, Column, Date, Integer, String

from ..data.repository import Base

PRICE_BASES = ("raw", "split_adjusted", "ths_qfq", "yahoo_adjusted", "legacy_unknown")
# 不随分红改写历史价的口径（可以安全地只增量抓尾部）
STABLE_BASES = frozenset({"raw", "split_adjusted"})


class MarketDataMeta(Base):
    __tablename__ = "market_data_meta"

    symbol = Column(String(20), primary_key=True)
    date = Column(Date, primary_key=True)
    price_basis = Column(String(24), nullable=False, comment="价格口径")
    source = Column(String(64), nullable=True, comment="上游来源")
    as_of = Column(DATETIME, nullable=True, comment="数据所属时点(UTC)")
    fetched_at = Column(DATETIME, nullable=False, comment="抓取时刻(UTC)")
    is_final = Column(Boolean, nullable=False, default=False, comment="是否收盘后的最终值")

    __table_args__ = ({"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},)


class MarketCorporateAction(Base):
    __tablename__ = "market_corporate_actions"

    symbol = Column(String(20), primary_key=True)
    ex_date = Column(Date, primary_key=True)
    kind = Column(String(12), primary_key=True, comment="dividend / split")
    value = Column(DECIMAL(20, 8), nullable=False, comment="每股分红额 / 拆分比例")
    detected_at = Column(DATETIME, nullable=False, comment="发现时刻(UTC)")

    __table_args__ = ({"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},)


class MarketUpstreamState(Base):
    """上游来源的冷却状态：重启/多实例后限流冷却仍然生效."""

    __tablename__ = "market_upstream_state"

    source = Column(String(32), primary_key=True)
    cooldown_until = Column(DATETIME, nullable=False, comment="冷却截止(UTC)")
    failures = Column(Integer, nullable=False, default=0, comment="连续触发限流次数")
    updated_at = Column(DATETIME, nullable=False, comment="更新时刻(UTC)")

    __table_args__ = ({"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},)


META_TABLES = (
    MarketDataMeta.__table__,
    MarketCorporateAction.__table__,
    MarketUpstreamState.__table__,
)
