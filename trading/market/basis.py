"""价格口径锁与重叠日校验：备用上游的行能不能接到库里已有序列的尾部.

背景：港股 / 美股的首选上游是 Yahoo；Yahoo 不可达（限流）时改用备用源，而备用源的价格口径和 Yahoo 不同——

- ``unadjusted``：真·不复权（东财 fqt=0、新浪美股）。拆股前的价格也是当时的价格；
- ``qfq_tencent``：腾讯前复权，每次除权除息都会整体改写历史；
- 库里已有的 Yahoo 序列是 ``yahoo_adjusted`` / ``split_adjusted``，遗留行是 ``legacy_unknown``（港股/美股上等同 Yahoo auto_adjust）。

**不同口径绝不静默拼接**：一个标的已存序列的「序列口径」（最近一个已定稿行的口径）和候选行口径不同，就不追加。
唯一的例外是 ``yahoo_adjusted`` / ``legacy_unknown`` 序列接 ``unadjusted`` 行，前提是**重叠日校验**通过：
备用源在库中最后一个已定稿行那天的收盘价，与库里存的收盘价相对差在 ``OVERLAP_TOLERANCE`` 以内。
这说明「那一天」没有被除权除息改写过，之后追加的不复权行与它同价位，不会在接缝处出现价格跳变。

已知限制（README 有完整说明）：校验只能证明**已经入库的行**没有被改写；之后才发生的除权除息（含拆股）
在下一次 Yahoo 同步整段重写之前，接缝处会有一道等于分红额 / 拆股比例的台阶。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional

UNADJUSTED = "unadjusted"
QFQ_TENCENT = "qfq_tencent"
# 备用源写入的口径（不会由 Yahoo 写入）
BACKUP_BASES = frozenset({UNADJUSTED, QFQ_TENCENT})
# 可以接 unadjusted 行（需重叠日校验）的序列口径；legacy_unknown 在 Yahoo 路由的港美股上就是 Yahoo auto_adjust
YAHOO_FAMILY = frozenset({"yahoo_adjusted", "legacy_unknown"})

# 重叠日收盘价允许的相对差：0.01%
OVERLAP_TOLERANCE = 1e-4
# 库里价格只存 4 位小数：低价股（几毛钱）的舍入误差会超过 0.01% 相对差，所以再给半个最小报价单位的绝对容差
STORAGE_HALF_QUANTUM = 0.5e-4 * 1.01

# append_rule 的返回值
FREE = "free"  # 可以直接追加
OVERLAP = "needs_overlap_check"  # 追加前必须通过重叠日校验
MISMATCH = "basis_mismatch"  # 口径不同，不追加


def append_rule(series_basis: Optional[str], candidate_basis: str) -> str:
    """候选口径能否接到「序列口径」之后.

    ``series_basis`` 为 None 表示库里还没有这个标的（空序列，随便什么口径都可以起头）。
    """
    if series_basis is None:
        return FREE
    if candidate_basis == series_basis:
        # 腾讯前复权每次除权都会改写历史：同口径追加也要证明那一天没有被改写
        return OVERLAP if candidate_basis == QFQ_TENCENT else FREE
    if candidate_basis == UNADJUSTED and series_basis in YAHOO_FAMILY:
        return OVERLAP
    return MISMATCH


@dataclass(frozen=True)
class OverlapResult:
    ok: bool
    date: Any = None
    stored_close: Optional[float] = None
    candidate_close: Optional[float] = None
    rel_diff: Optional[float] = None
    tolerance: float = OVERLAP_TOLERANCE
    reason: Optional[str] = None  # ok=False 时：overlap_row_missing / invalid_close / close_differs

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "date": self.date.isoformat() if hasattr(self.date, "isoformat") else self.date,
            "stored_close": self.stored_close,
            "candidate_close": self.candidate_close,
            "rel_diff": None if self.rel_diff is None else round(self.rel_diff, 8),
            "tolerance": self.tolerance,
            "reason": self.reason,
        }


def overlap_check(
    stored_close: Optional[float],
    candidate_close: Optional[float],
    *,
    tolerance: float = OVERLAP_TOLERANCE,
    day: Any = None,
) -> OverlapResult:
    """同一天的收盘价是否一致：相对差 ≤ ``tolerance``（或不超过半个存储精度的绝对差）."""
    if candidate_close is None or stored_close is None:
        return OverlapResult(False, day, stored_close, candidate_close, None, tolerance, "overlap_row_missing")
    if stored_close <= 0 or candidate_close <= 0:
        return OverlapResult(False, day, stored_close, candidate_close, None, tolerance, "invalid_close")
    diff = abs(candidate_close - stored_close)
    rel = diff / stored_close
    ok = rel <= tolerance or diff <= STORAGE_HALF_QUANTUM
    return OverlapResult(ok, day, stored_close, candidate_close, rel, tolerance, None if ok else "close_differs")


def check_overlap_rows(
    anchor: Dict[str, Any], candidate_rows: Iterable[Dict[str, Any]], *, tolerance: float = OVERLAP_TOLERANCE
) -> OverlapResult:
    """用库中最后一个已定稿行（``anchor``）和候选行里**同一天**的收盘价做校验；候选里没有这一天就不通过."""
    day = anchor["date"]
    candidate = next((r for r in candidate_rows if r["date"] == day), None)
    return overlap_check(
        float(anchor["close_price"]),
        float(candidate["close_price"]) if candidate is not None else None,
        tolerance=tolerance,
        day=day,
    )
