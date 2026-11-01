"""公司行动（分红 / 拆股）检测：什么时候必须重写某标的的整段历史.

复权口径（``yahoo_adjusted``）下，每次除权除息都会让**此前所有行**的价格整体平移一个比例；
只重写最近窗口会在窗口起点处留下一道「接缝」。所以检测到**新的**公司行动时，要把该标的整段历史一并重写。
不复权但按拆股调整的口径（``split_adjusted``）只有拆股才改变历史。

「新的」以 ``market_corporate_actions`` 里已记录的事件为准。每个标的第一次被检测时只写一条
``baseline`` 标记并记录当时已有的事件，**不触发**重写——存量接缝由一次性的 ``trading.cli.rebuild``
（默认 dry-run，带备份和回滚）处理，而不是在某天夜里的同步里悄悄改写历史。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List

BASELINE_DATE = dt.date(1970, 1, 1)
BASELINE_KIND = "baseline"
_TRIGGERS = {
    "yahoo_adjusted": frozenset({"dividend", "split"}),
    "ths_qfq": frozenset(),  # 同花顺每次给全量历史，本来就是整段重写
}
_DEFAULT_TRIGGERS = frozenset({"split"})


@dataclass
class Assessment:
    trigger: bool = False
    new: List[Dict[str, Any]] = field(default_factory=list)  # 触发时需要在重写成功后再记录的新事件


def trackable(store: Any) -> bool:
    return bool(getattr(store, "meta_enabled", False)) and not getattr(store, "read_only", False)


def assess(store: Any, symbol: str, result: Any, now: dt.datetime) -> Assessment:
    """评估这次抓取结果是否带来了需要重写整段历史的新公司行动."""
    if not trackable(store):
        return Assessment()
    known = {(a["ex_date"], a["kind"]) for a in store.list_actions(symbol)}
    if (BASELINE_DATE, BASELINE_KIND) not in known:
        store.record_actions(
            symbol, [{"ex_date": BASELINE_DATE, "kind": BASELINE_KIND, "value": 0}, *result.actions], now
        )
        return Assessment()
    new = [a for a in result.actions if (a["ex_date"], a["kind"]) not in known]
    triggers = _TRIGGERS.get(result.price_basis, _DEFAULT_TRIGGERS)
    return Assessment(trigger=any(a["kind"] in triggers for a in new), new=new)


def commit(store: Any, symbol: str, actions: List[Dict[str, Any]], now: dt.datetime) -> None:
    if actions and trackable(store):
        store.record_actions(symbol, actions, now)
