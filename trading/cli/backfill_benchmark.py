"""基准指数历史回补（默认 dry-run）.

    trading-backfill-benchmark                          # dry-run：只报告，不写库
    trading-backfill-benchmark --apply                  # 写入缺失 / 未定稿的日线
    trading-backfill-benchmark --symbol 000300.SS --symbol ^HSI --start-date 2024-12-01

用腾讯（失败回退东财）的交易日列表与库里该指数的日期做差集：
- 缺失：源端有、库里没有 → ``--apply`` 时写入；
- 未定稿：库里是盘中写入的半根（``is_final`` 为 False）→ ``--apply`` 时用完整日线覆盖；
- 对账差异：库里已完成的日子，收盘价与源端相差 >0.05% 记警告，>0.5% 记错误；**差异行一律不覆盖**，只报告；
- 库里多出：库里有、源端没有 → 只提示，不删除。

写入的都是已定稿行（结束日按市场收盘时间收紧到最近一个已完成的交易日），沿用 ``store.upsert`` 的防回退保护。
dry-run 的输出同时是 Markdown，设置了 ``GITHUB_STEP_SUMMARY`` 时会追加到 job summary。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from loguru import logger

from ..market.clock import as_of_for, business_today, completed_session_end, is_final_snapshot, utcnow
from ..market.errors import FetchError
from ..market.fetchers import INDEX_EASTMONEY, INDEX_ROUTES, INDEX_TENCENT, RangePriceFetcher
from ..market.reconcile import Finding, reconcile_rows

DEFAULT_SYMBOLS = ("000300.SS",)
FALLBACK_START = dt.date(2022, 1, 1)  # 库里还没有该指数时的回补起点
LIST_LIMIT = 60  # 报告里最多列出的日期数


@dataclass
class BackfillPlan:
    symbol: str
    source: str
    start: dt.date
    end: dt.date
    source_days: int
    stored_days: int
    missing: List[dt.date] = field(default_factory=list)
    incomplete: List[dt.date] = field(default_factory=list)
    extra: List[dt.date] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    rows_to_write: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.level == "warning"]


def plan_backfill(
    symbol: str,
    source: str,
    start: dt.date,
    end: dt.date,
    source_rows: Sequence[Dict[str, Any]],
    stored_rows: Sequence[Dict[str, Any]],
) -> BackfillPlan:
    """纯函数：源端行与库内行做差集 + 对账，给出要写入的行."""
    source_by_date = {r["date"]: r for r in source_rows}
    stored_by_date = {r["date"]: r for r in stored_rows}
    missing = sorted(set(source_by_date) - set(stored_by_date))
    extra = sorted(set(stored_by_date) - set(source_by_date))
    incomplete = sorted(
        d for d in set(source_by_date) & set(stored_by_date)
        if (stored_by_date[d].get("meta") or {}).get("is_final") is False
    )
    overlap_rows = [source_by_date[d] for d in sorted(set(source_by_date) & set(stored_by_date))]
    _, findings = reconcile_rows(overlap_rows, stored_rows)
    return BackfillPlan(
        symbol=symbol,
        source=source,
        start=start,
        end=end,
        source_days=len(source_by_date),
        stored_days=len(stored_by_date),
        missing=missing,
        incomplete=incomplete,
        extra=extra,
        findings=findings,
        rows_to_write=[source_by_date[d] for d in sorted(set(missing) | set(incomplete))],
    )


def _dates(days: List[dt.date]) -> str:
    if not days:
        return "无"
    shown = ", ".join(str(d) for d in days[:LIST_LIMIT])
    return shown + (f" …（共 {len(days)} 个）" if len(days) > LIST_LIMIT else "")


def render_plan(plan: BackfillPlan, *, apply: bool) -> str:
    lines = [
        f"### {plan.symbol}　{'APPLY' if apply else 'DRY-RUN'}",
        f"- 数据源：`{plan.source}`　区间：{plan.start} ~ {plan.end}",
        f"- 源端交易日 **{plan.source_days}**，库内 **{plan.stored_days}**",
        f"- 缺失 **{len(plan.missing)}** 个：{_dates(plan.missing)}",
        f"- 未定稿（库里是盘中半根，待完整日线覆盖）**{len(plan.incomplete)}** 个：{_dates(plan.incomplete)}",
        f"- 对账：警告（>0.05%）**{len(plan.warnings)}** 个，错误（>0.5%，不覆盖）**{len(plan.errors)}** 个",
    ]
    for item in plan.errors[:10]:
        lines.append(f"  - ERROR {item.describe()}")
    for item in plan.warnings[:5]:
        lines.append(f"  - WARN {item.describe()}")
    lines.append(f"- 库内多出（源端没有，仅提示、不删除）**{len(plan.extra)}** 个：{_dates(plan.extra)}")
    todo = len(plan.rows_to_write)
    lines.append(
        f"- 结论：{'已写入' if apply else 'dry-run 未写入，加 `--apply` 将写入'} **{todo}** 行"
        if todo
        else "- 结论：没有需要写入的行"
    )
    return "\n".join(lines)


def _write(store: Any, plan: BackfillPlan) -> int:
    fetched_at = utcnow()
    rows = plan.rows_to_write
    finals = {r["date"]: is_final_snapshot(plan.symbol, r["date"], fetched_at) for r in rows}
    as_of = {d: as_of_for(plan.symbol, d, fetched_at, f) for d, f in finals.items()}
    return store.upsert(
        rows,
        price_basis="raw",
        source=plan.source,
        fetched_at=fetched_at,
        final_flags=finals,
        as_of=as_of,
    )


def run(
    symbols: Sequence[str],
    start: Optional[dt.date],
    end: Optional[dt.date],
    *,
    apply: bool,
    store: Any,
    fetcher: Any,
    now: Callable[[], dt.datetime] = utcnow,
) -> int:
    """返回退出码：0 = 报告 / 写入完成；1 = 有指数取不到源端数据."""
    blocks: List[str] = []
    failed = False
    for symbol in symbols:
        sym_start = start or store.first_date(symbol) or FALLBACK_START
        sym_end = completed_session_end(symbol, end or business_today(now()), now())
        try:
            result = fetcher.fetch_index(
                symbol, sym_start, sym_end, sources=[INDEX_TENCENT, INDEX_EASTMONEY]
            )
        except FetchError as exc:
            failed = True
            blocks.append(f"### {symbol}\n- **取数失败**：{exc}")
            logger.error(f"{symbol} 回补取数失败：{exc}")
            continue
        if not result.rows:
            failed = True
            blocks.append(f"### {symbol}\n- **源端没有返回任何数据**（区间 {sym_start} ~ {sym_end}）")
            continue
        stored = store.query_range(symbol, sym_start, sym_end)
        plan = plan_backfill(symbol, result.source, sym_start, sym_end, result.rows, stored)
        if apply and plan.rows_to_write:
            store.ensure_meta_tables()
            written = _write(store, plan)
            logger.info(f"{symbol} 已写入 {written} 行")
        blocks.append(render_plan(plan, apply=apply))
    report = "## 基准指数回补\n\n" + "\n\n".join(blocks) + "\n"
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(report)
    return 1 if failed else 0


def _parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def main(argv: Optional[Sequence[str]] = None, *, store: Any = None, fetcher: Any = None) -> None:
    parser = argparse.ArgumentParser(description="基准指数历史回补（默认 dry-run，--apply 才写库）")
    parser.add_argument(
        "--symbol", action="append", dest="symbols",
        help=f"指数代码，可重复；默认 {','.join(DEFAULT_SYMBOLS)}；可选 {','.join(INDEX_ROUTES)}",
    )
    parser.add_argument("--start-date", type=_parse_date, default=None, help="起始日期 YYYY-MM-DD；默认库内最早日期")
    parser.add_argument("--end-date", type=_parse_date, default=None, help="结束日期 YYYY-MM-DD；默认最近一个已完成交易日")
    parser.add_argument("--apply", action="store_true", help="写入缺失 / 未定稿的日线（默认只报告）")
    args = parser.parse_args(argv)

    symbols = args.symbols or list(DEFAULT_SYMBOLS)
    unknown = [s for s in symbols if s not in INDEX_ROUTES]
    if unknown:
        parser.error(f"不支持的指数：{', '.join(unknown)}（可选 {', '.join(INDEX_ROUTES)}）")

    owned_db = None
    if store is None:
        from ..data.repository import DBClient
        from ..market.store import MarketStore

        owned_db = DBClient()
        store = MarketStore(owned_db.engine)
    try:
        code = run(
            symbols, args.start_date, args.end_date,
            apply=args.apply, store=store, fetcher=fetcher or RangePriceFetcher(),
        )
    finally:
        if owned_db is not None:
            owned_db.close()
    if code:
        sys.exit(code)


if __name__ == "__main__":
    main()
