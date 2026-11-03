"""trading.cli.rebuild：默认 dry-run、--apply 先备份再写、写后指纹校验、可回滚."""
import datetime as dt
import json


from trading.cli import rebuild
from trading.market.fetchers import FetchResult
from trading.market.service import fingerprint

from .conftest import make_row
from .test_sync_and_store import insert_legacy

D = dt.date
SYM = "0700.HK"
TODAY = D(2024, 12, 31)
UPDATED = dt.datetime(2024, 11, 30, 0, 0)


class UpstreamFake:
    def __init__(self, rows, actions=()):
        self.rows, self.actions, self.calls, self.fail = rows, list(actions), [], None

    def fetch(self, symbol, start, end, *, mode="raw", retry=None):
        self.calls.append((symbol, start, end, mode))
        if self.fail:
            raise self.fail
        return FetchResult([dict(r) for r in self.rows if r["symbol"] == symbol], "yahoo_auto_adjust", "yahoo_adjusted", actions=list(self.actions))


def seed(store):
    # 窗口起点前后各两行：中间有一次分红造成的接缝（旧口径 100/101，新口径应是 98/99）
    for day, close in [(D(2024, 11, 28), 100.0), (D(2024, 11, 29), 101.0), (D(2024, 12, 2), 50.0), (D(2024, 12, 3), 51.0)]:
        insert_legacy(store, day, close, UPDATED, SYM)


def upstream():
    rows = [make_row(SYM, D(2024, 11, 28), 98.0), make_row(SYM, D(2024, 11, 29), 99.0),
            make_row(SYM, D(2024, 12, 2), 50.0), make_row(SYM, D(2024, 12, 3), 51.0),
            make_row(SYM, D(2024, 12, 4), 52.0)]  # 多一个新日期
    return UpstreamFake(rows, [{"ex_date": D(2024, 12, 2), "kind": "dividend", "value": 2.0}])


def run(store, fetcher, tmp_path, **kw):
    lines = []
    code = rebuild.run(store, fetcher, today=TODAY, pause=0, out=lines.append, backup_dir=tmp_path / "bk", **kw)
    return code, "\n".join(lines)


def table(store):
    return json.dumps(store.export_snapshot(SYM), sort_keys=True)


def test_dry_run_reports_the_seam_and_writes_nothing(store, tmp_path):
    seed(store)
    before = table(store)
    code, text = run(store, upstream(), tmp_path)
    assert code == 0 and "DRY-RUN" in text and "没有写入任何数据" in text
    assert "价格变化的行 2" in text and "新增日期 1" in text and "最大" in text
    assert table(store) == before and not (tmp_path / "bk").exists()  # 库和文件系统都没动


def test_apply_backs_up_first_rewrites_whole_history_and_verifies_fingerprint(store, tmp_path):
    seed(store)
    before = table(store)
    code, text = run(store, upstream(), tmp_path, apply=True)
    assert code == 0 and "写后校验：通过" in text
    rows = store.query_range(SYM, D(2024, 11, 1), D(2024, 12, 31))
    assert [r["close_price"] for r in rows] == [98.0, 99.0, 50.0, 51.0, 52.0]
    assert {r["meta"]["price_basis"] for r in rows} == {"yahoo_adjusted"}  # 重建后口径明确，不再是 legacy_unknown
    # 备份在写入之前完成，且内容就是执行前的完整表
    backup = json.loads((tmp_path / "bk" / "0700.HK.snapshot.json").read_text())
    assert len(backup["trading_data"]) == 4 and backup["meta"] == []
    manifest = json.loads((tmp_path / "bk" / "manifest.json").read_text())
    entry = manifest["symbols"][SYM]
    assert entry["applied"] and entry["verified"] and entry["fingerprint_all_rows_before"] != entry["fingerprint_all_rows_after"]
    assert entry["inserted_dates"] == ["2024-12-04"]
    # 基线已建立：之后的每晚同步不会因为这次已吸收的分红再整段重写
    assert any(a["kind"] == "baseline" for a in store.list_actions(SYM))
    assert any(a["kind"] == "dividend" for a in store.list_actions(SYM))
    assert table(store) != before


def test_rollback_restores_every_row_and_removes_inserted_rows_and_meta(store, tmp_path):
    seed(store)
    before = table(store)
    fp_before = fingerprint(store.query_range(SYM, D(2024, 11, 1), D(2024, 12, 31)))
    run(store, upstream(), tmp_path, apply=True)
    lines = []
    assert rebuild.rollback(store, tmp_path / "bk", out=lines.append) == 0
    assert table(store) == before  # 逐字段一致（含 create_time / update_time、旁表为空）
    assert fingerprint(store.query_range(SYM, D(2024, 11, 1), D(2024, 12, 31))) == fp_before
    assert "通过" in lines[0]


def test_suspiciously_small_upstream_response_is_refused_unless_forced(store, tmp_path):
    seed(store)
    small = UpstreamFake([make_row(SYM, D(2024, 12, 3), 51.0)])
    before = table(store)
    code, text = run(store, small, tmp_path, apply=True)
    assert code == 1 and "拒绝写入" in text and table(store) == before
    code, _ = run(store, small, tmp_path, apply=True, force=True)
    assert code == 0


def test_upstream_failure_leaves_database_untouched(store, tmp_path):
    seed(store)
    before = table(store)
    fake = upstream()
    fake.fail = RuntimeError("rate limited")
    code, text = run(store, fake, tmp_path, apply=True)
    assert code == 1 and "抓取失败" in text and table(store) == before


def test_funds_and_already_tagged_symbols_are_not_targets(store, tmp_path):
    seed(store)
    insert_legacy(store, D(2024, 12, 2), 1.5, UPDATED, "005698")  # 场外基金：不动
    store.upsert([make_row("159915.SZ", D(2024, 12, 2), 1.0)], price_basis="ths_qfq")  # 已有明确口径
    fake = upstream()
    code, text = run(store, fake, tmp_path)
    assert code == 0 and [c[0] for c in fake.calls] == [SYM]


def test_symbol_filter_and_no_targets(store, tmp_path):
    code, text = run(store, upstream(), tmp_path)
    assert code == 0 and "无需重建" in text
    seed(store)
    fake = upstream()
    run(store, fake, tmp_path, symbols=["MSFT"])
    assert fake.calls == []


def test_read_only_database_refuses_apply(store, tmp_path):
    seed(store)
    store.configure_read_only()
    code, text = run(store, upstream(), tmp_path, apply=True)
    assert code == 1 and "只读" in text


def test_rate_limited_upstream_is_reported_not_retried_and_leaves_database_untouched(store, tmp_path):
    from trading.market.errors import RateLimited

    seed(store)
    before = table(store)
    fake = upstream()
    fake.fail = RateLimited("yahoo", 90)
    code, text = run(store, fake, tmp_path, apply=True)
    assert code == 1 and "正在限流" in text and "稍后重跑" in text and table(store) == before
    assert len(fake.calls) == 1  # 命中限流后不重试
