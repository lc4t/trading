"""指数日线对账：新抓到的收盘价与库里已完成交易日的收盘价比较.

指数不复权，已完成交易日的收盘价不会再变；新旧来源对同一天给出明显不同的收盘价，说明某一边出了问题，
此时宁可保留库里的值也不要静默覆盖。

- 相对差 > ``WARN_THRESHOLD``（0.05%）：记 WARNING，仍按新值写入；
- 相对差 > ``ERROR_THRESHOLD``（0.5%）：记 ERROR，**不覆盖**该行。

只对账「已完成」的日子：库里该日的行是盘中写入的（``meta.is_final`` 为 False）时不比较，直接让完整日线覆盖；
没有元数据的遗留行视为已完成。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Tuple

WARN_THRESHOLD = 0.0005
ERROR_THRESHOLD = 0.005


@dataclass(frozen=True)
class Finding:
    date: dt.date
    stored_close: float
    new_close: float
    rel_diff: float
    level: str  # "warning" | "error"

    def describe(self) -> str:
        return (
            f"{self.date} 库内 {self.stored_close:.4f} / 新值 {self.new_close:.4f}"
            f"（相对差 {self.rel_diff * 100:.3f}%）"
        )


def _is_complete(stored: Dict[str, Any]) -> bool:
    meta = stored.get("meta")
    return meta is None or bool(meta.get("is_final"))


def reconcile_rows(
    new_rows: Iterable[Dict[str, Any]],
    stored_rows: Iterable[Dict[str, Any]],
    *,
    warn: float = WARN_THRESHOLD,
    error: float = ERROR_THRESHOLD,
) -> Tuple[List[Dict[str, Any]], List[Finding]]:
    """返回 ``(可写入的行, 对账发现)``：超过 ``error`` 的行不在可写入列表里."""
    stored_by_date = {r["date"]: r for r in stored_rows}
    kept: List[Dict[str, Any]] = []
    findings: List[Finding] = []
    for row in new_rows:
        stored = stored_by_date.get(row["date"])
        if stored is None or not _is_complete(stored):
            kept.append(row)
            continue
        old, new = float(stored["close_price"]), float(row["close_price"])
        rel = abs(new - old) / old if old > 0 else 0.0
        if rel > error:
            findings.append(Finding(row["date"], old, new, rel, "error"))
            continue
        if rel > warn:
            findings.append(Finding(row["date"], old, new, rel, "warning"))
        kept.append(row)
    return kept, findings
