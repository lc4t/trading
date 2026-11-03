"""遗留价格口径重建：用与每晚同步相同的口径，把「口径未知」标的的**整段历史**重写成同一口径.

背景：每晚同步只重写 ``--start-date`` 之后的窗口（Yahoo 标的），更早的行停在上次抓取时的复权口径；
期间发生过分红/拆股的标的，窗口起点处就有一道价格「接缝」。本命令一次性消除它。

    trading-mcp rebuild                       # dry-run：只抓取、比对、出报告，不写库
    trading-mcp rebuild --symbol 0700.HK      # 只看某个标的
    trading-mcp rebuild --apply               # 先备份受影响的行，再写入，并输出执行前后指纹
    trading-mcp rebuild --rollback DIR        # 用备份目录回滚

- 只处理 price_basis 为 legacy_unknown（没有旁表记录）的标的；场外基金净值不动。
- 默认 dry-run；``--apply`` 才写库。写库前必先把受影响标的的 trading_data / 旁表全部行完整备份到 ``--backup-dir``。
- 每个标的在**一个事务**里写入；写入后重新读库校验指纹，不一致会明确报错并提示回滚。
- 需要有写权限的 DB 账号；dry-run 只需要读权限（和上游网络）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..market.clock import as_of_for, business_today, is_final_snapshot, is_off_market_fund, utcnow
from ..market.errors import RateLimited
from ..market.fetchers import REQUEST_RETRY, RangePriceFetcher
from ..market.rows import clean_rows
from ..market.service import fingerprint
from ..market.store import _QUANT, MarketStore

CHANGE_EPS = 1e-4  # 相对差异超过它才算「价格变了」（库里是 4 位小数）
MIN_KEEP_RATIO = 0.9  # 抓回来的行数少于现有的 90% 视为异常，拒绝写入（除非 --force）


def _quantized(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按写库的方式清洗并量化到 4 位小数，这样才能和库里读出来的行逐位比较."""
    out = []
    for r in clean_rows(rows):
        item = dict(r)
        for f in ("open_price", "close_price", "high", "low"):
            item[f] = float(Decimal(str(item[f])).quantize(_QUANT))
        out.append(item)
    return out


def compare(existing: List[Dict[str, Any]], fetched: List[Dict[str, Any]]) -> Dict[str, Any]:
    """现有行 vs 抓回来的行：多少行变了、变了多少、接缝在哪、两边指纹."""
    by_date = {r["date"]: r for r in existing}
    new_by_date = {r["date"]: r for r in fetched}
    shared = sorted(set(by_date) & set(new_by_date))
    diffs = []
    for d in shared:
        old, new = by_date[d]["close_price"], new_by_date[d]["close_price"]
        if old:
            diffs.append((d, new / old - 1.0))
    changed = [(d, x) for d, x in diffs if abs(x) > CHANGE_EPS]
    report: Dict[str, Any] = {
        "existing_rows": len(existing),
        "fetched_rows": len(fetched),
        "shared_dates": len(shared),
        "inserted_dates": sorted(set(new_by_date) - set(by_date)),
        "stale_dates": sorted(set(by_date) - set(new_by_date)),
        "changed_rows": len(changed),
        "fingerprint_before": fingerprint([by_date[d] for d in shared]) if shared else None,
        "fingerprint_fetched": fingerprint([new_by_date[d] for d in shared]) if shared else None,
    }
    if changed:
        rel = [abs(x) for _, x in changed]
        report.update(
            changed_first=changed[0][0].isoformat(),
            changed_last=changed[-1][0].isoformat(),
            rel_diff_median=statistics.median(rel),
            rel_diff_max=max(rel),
            sample=[(d.isoformat(), round(x * 100, 4)) for d, x in (changed[:2] + changed[-1:])],
        )
    return report


def _dump(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def _safe(symbol: str) -> str:
    return symbol.replace("^", "_").replace("/", "_")


def select_targets(store: MarketStore, only: Sequence[str]) -> List[Dict[str, Any]]:
    wanted = {s.upper() for s in only}
    targets = []
    for item in store.legacy_summary():
        if wanted and item["symbol"].upper() not in wanted:
            continue
        if is_off_market_fund(item["symbol"]) or item["legacy_rows"] == 0:
            continue
        targets.append(item)
    return targets


def run(
    store: MarketStore,
    fetcher: Any,
    *,
    symbols: Sequence[str] = (),
    apply: bool = False,
    backup_dir: Optional[Path] = None,
    force: bool = False,
    pause: float = 2.0,
    today: Optional[dt.date] = None,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """返回进程退出码：0 = 全部成功（或 dry-run 正常），1 = 有标的失败."""
    today = today or business_today()
    store.probe_meta()
    if apply and store.read_only:
        out("错误：数据库是只读的，无法 --apply")
        return 1
    targets = select_targets(store, symbols)
    if not targets:
        out("没有口径未知（legacy_unknown）的标的，无需重建。")
        return 0
    mode = "APPLY（写库）" if apply else "DRY-RUN（不写库）"
    out(f"== 遗留价格口径重建 · {mode} · 目标 {len(targets)} 个标的 ==")
    manifest: Dict[str, Any] = {"created_at": utcnow().isoformat(), "symbols": {}}
    if apply:
        backup_dir = Path(backup_dir or f"rebuild-backup-{utcnow():%Y%m%d-%H%M%S}")
        backup_dir.mkdir(parents=True, exist_ok=True)
        out(f"备份目录：{backup_dir}")
    failures = 0

    for i, item in enumerate(targets):
        symbol = item["symbol"]
        out(f"\n[{symbol}] 现有 {item['rows']} 行（{item['first']} ~ {item['last']}），口径未知 {item['legacy_rows']} 行")
        if i and pause:
            sleep(pause)
        try:
            result = fetcher.fetch(symbol, item["first"], today, mode="legacy")
        except RateLimited as exc:
            out(f"  上游 {exc.source} 正在限流（冷却约 {int(exc.retry_after)}s，状态与服务共享）——跳过，库未改动；稍后重跑即可")
            failures += 1
            continue
        except Exception as exc:  # noqa: BLE001
            out(f"  抓取失败（{type(exc).__name__}: {exc}）——跳过，库未改动")
            failures += 1
            continue
        fetched = _quantized(result.rows)
        existing = store.query_range(symbol, item["first"], today)
        cmp = compare(existing, fetched)
        out(f"  上游 {result.source}（口径 {result.price_basis}）返回 {cmp['fetched_rows']} 行，与现有同日期 {cmp['shared_dates']} 行比对：")
        out(f"  价格变化的行 {cmp['changed_rows']}；新增日期 {len(cmp['inserted_dates'])}；上游没有的现有日期 {len(cmp['stale_dates'])}（保留不动）")
        if cmp["changed_rows"]:
            out(
                f"  变化范围 {cmp['changed_first']} ~ {cmp['changed_last']}，相对差异 中位 {cmp['rel_diff_median']:.4%} / 最大 {cmp['rel_diff_max']:.4%}；"
                f"样例(日期, %): {cmp['sample']}"
            )
        out(f"  指纹（同日期）：现有 {cmp['fingerprint_before'] and cmp['fingerprint_before'][:16]} → 重建后 {cmp['fingerprint_fetched'] and cmp['fingerprint_fetched'][:16]}")
        entry = {"first": item["first"].isoformat(), "rows_before": item["rows"], "applied": False}
        manifest["symbols"][symbol] = entry
        if not apply:
            continue

        if not fetched or len(fetched) < MIN_KEEP_RATIO * item["rows"]:
            if not force:
                out(f"  拒绝写入：上游只返回 {len(fetched)} 行，少于现有 {item['rows']} 行的 {MIN_KEEP_RATIO:.0%}（确认无误可加 --force）")
                failures += 1
                continue
        snapshot = store.export_snapshot(symbol)
        all_rows_fp = fingerprint(store.query_range(symbol, item["first"], today))
        snap_file = backup_dir / f"{_safe(symbol)}.snapshot.json"
        _dump(snap_file, snapshot)
        entry.update(
            fingerprint_all_rows_before=all_rows_fp,
            inserted_dates=[d.isoformat() for d in cmp["inserted_dates"]],
            snapshot_file=snap_file.name,
        )
        _dump(backup_dir / "manifest.json", manifest)
        out(f"  已备份 {len(snapshot['trading_data'])} 行 trading_data + {len(snapshot['meta'])} 行旁表 → {snap_file.name}")

        fetched_at = utcnow()
        finals = {r["date"]: is_final_snapshot(symbol, r["date"], fetched_at) for r in fetched}
        as_of = {d: as_of_for(symbol, d, fetched_at, f) for d, f in finals.items()}
        store.upsert(
            fetched, price_basis=result.price_basis, source=result.source,
            fetched_at=fetched_at, final_flags=finals, as_of=as_of,
        )
        # 基线：之后的每晚同步只在出现**新的**分红/拆股时才整段重写
        store.record_actions(
            symbol,
            [{"ex_date": dt.date(1970, 1, 1), "kind": "baseline", "value": 0}, *result.actions],
            fetched_at,
        )
        after = store.query_range(symbol, item["first"], today)
        after_fp = fingerprint([r for r in after if r["date"] in {x["date"] for x in fetched}])
        expect_fp = fingerprint(fetched)
        ok = after_fp == expect_fp
        entry.update(applied=True, fingerprint_all_rows_after=fingerprint(after), verified=ok)
        _dump(backup_dir / "manifest.json", manifest)
        out(f"  已写入 {len(fetched)} 行；写后校验：{'通过' if ok else '失败！指纹与上游不一致'}（{after_fp[:16]} vs {expect_fp[:16]}）")
        if not ok:
            out(f"  → 请立即回滚：trading-mcp rebuild --rollback {backup_dir}")
            failures += 1

    if apply:
        out(f"\n清单：{backup_dir / 'manifest.json'}；回滚：trading-mcp rebuild --rollback {backup_dir}")
    else:
        out("\ndry-run：没有写入任何数据。确认报告无误后加 --apply 执行（会先备份）。")
    return 1 if failures else 0


def rollback(store: MarketStore, backup_dir: Path, out: Callable[[str], None] = print) -> int:
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    store.probe_meta()
    failures = 0
    for symbol, entry in manifest["symbols"].items():
        if not entry.get("applied"):
            continue
        snapshot = json.loads((backup_dir / entry["snapshot_file"]).read_text(encoding="utf-8"))
        store.restore_snapshot(symbol, snapshot, [dt.date.fromisoformat(d) for d in entry.get("inserted_dates", [])])
        today = business_today()
        fp = fingerprint(store.query_range(symbol, dt.date.fromisoformat(entry["first"]), today))
        ok = fp == entry["fingerprint_all_rows_before"]
        out(f"[{symbol}] 已回滚；指纹 {fp[:16]} {'==' if ok else '!='} 执行前 {entry['fingerprint_all_rows_before'][:16]}  {'通过' if ok else '失败'}")
        failures += 0 if ok else 1
    return 1 if failures else 0


def main(argv: Optional[Sequence[str]] = None) -> None:
    ap = argparse.ArgumentParser(prog="trading-mcp rebuild", description="遗留价格口径重建（默认 dry-run）")
    ap.add_argument("--symbol", action="append", default=[], help="只处理这个标的（可重复）")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只做 dry-run）")
    ap.add_argument("--backup-dir", help="备份目录，默认 ./rebuild-backup-<时间戳>")
    ap.add_argument("--rollback", metavar="DIR", help="用备份目录回滚")
    ap.add_argument("--force", action="store_true", help="上游行数明显少于现有时仍写入")
    ap.add_argument("--pause", type=float, default=2.0, help="标的之间的间隔秒数，避免触发上游限流")
    args = ap.parse_args(argv)

    from ..market.config import DatabaseConfig
    import os

    missing = [k for k in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME") if not os.environ.get(k)]
    if missing:
        print("缺少 MySQL 配置: " + ", ".join(missing), file=sys.stderr)
        sys.exit(2)
    cfg = DatabaseConfig(
        os.environ["DB_HOST"], int(os.environ.get("DB_PORT") or 3306), os.environ["DB_USER"],
        os.environ["DB_PASSWORD"], os.environ["DB_NAME"],
    )
    store = MarketStore(cfg.create_engine())
    if args.rollback:
        sys.exit(rollback(store, Path(args.rollback)))
    if args.apply:
        store.ensure_meta_tables()
    # 与服务共用同一套限流保护：读取/写入共享的冷却状态，命中限流立即放弃（不重试、不死等），避免把上游越撞越死
    from ..market.upstream import GuardedFetcher, UpstreamGuard

    fetcher = GuardedFetcher(RangePriceFetcher(retry=REQUEST_RETRY), UpstreamGuard(store=store))
    sys.exit(
        run(store, fetcher, symbols=args.symbol, apply=args.apply,
            backup_dir=Path(args.backup_dir) if args.backup_dir else None, force=args.force, pause=args.pause)
    )


if __name__ == "__main__":
    main()
