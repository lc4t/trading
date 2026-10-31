"""行级清洗（不依赖数据库模型，供 repository 与 store 共用）。"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence

from loguru import logger

PRICE_FIELDS = ("open_price", "close_price", "high", "low")
OPTIONAL_FIELDS = ("volume", "amount", "change", "change_pct")


def _is_nan(value: Any) -> bool:
    return isinstance(value, float) and math.isnan(value)


def clean_rows(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """写入前清洗：开高低收缺失/NaN 的行直接跳过，可选字段 NaN 清成 None.

    MySQL 不接受 NaN 字面量，一条脏数据会让整批 INSERT 失败；缺一天好过丢一批。
    """
    cleaned: List[Dict[str, Any]] = []
    skipped: List[str] = []
    for item in rows:
        if any(item.get(f) is None or _is_nan(item.get(f)) for f in PRICE_FIELDS):
            skipped.append(str(item.get("date")))
            continue
        row = dict(item)
        for field in OPTIONAL_FIELDS:
            if _is_nan(row.get(field)):
                row[field] = None
        cleaned.append(row)
    if skipped:
        symbol = rows[0].get("symbol", "?") if rows else "?"
        logger.warning(
            f"{symbol} 有 {len(skipped)} 条记录开高低收缺失/NaN，已跳过"
            f"（不影响其余 {len(cleaned)} 条正常数据写入）: {skipped}"
        )
    return cleaned
