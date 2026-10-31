"""data_fingerprint 必须与既有 MCP 契约（quant-engine）的算法逐字一致；这里内联一份该算法做对照."""
import datetime as dt
import hashlib
import json

import pandas as pd
import pytest

from trading.market.service import MarketService, fingerprint, state_fingerprint

from .conftest import cn_time, make_row

_FIELD_COLUMNS = {
    "open": "open_price", "close": "close_price", "high": "high", "low": "low", "volume": "volume",
    "amount": "amount", "change": "change", "change_pct": "change_pct",
}  # fmt: skip


def reference_fingerprint(records):
    """既有实现：load_market_data 里的指纹那一段，原样照搬."""
    rows = pd.DataFrame.from_records(records)
    rows["symbol"] = rows["symbol"].astype(str).str.strip().str.upper()
    rows["date"] = pd.to_datetime(rows["date"])
    rows.sort_values(["date", "symbol"], inplace=True)
    rows.drop_duplicates(["date", "symbol"], keep="last", inplace=True)
    canonical_rows = []
    fingerprint_columns = ["symbol", "date", *_FIELD_COLUMNS.values()]
    for row in rows.reindex(columns=fingerprint_columns).itertuples(index=False):
        canonical_rows.append([v.isoformat() if isinstance(v, pd.Timestamp) else v for v in row])
    encoded = json.dumps(
        canonical_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def db_like(symbol, day, close, **extra):
    row = make_row(symbol, day, close)
    row.update(extra)
    row["date"] = day.isoformat()  # 既有实现拿到的是 serialize_row 之后的字符串日期
    return row


@pytest.mark.parametrize("with_amount", [False, True])
def test_matches_reference_algorithm_including_none_and_nan_columns(with_amount):
    records = []
    for i in range(5):
        for sym in ("159915.SZ", "513100.SS"):
            extra = {"amount": 1000.5 * i, "change": 0.01 * i, "change_pct": None} if with_amount and i % 2 else {}
            records.append(db_like(sym, dt.date(2024, 6, 3) + dt.timedelta(days=i), 1.0 + i / 10, **extra))
    mine = [dict(r, date=dt.date.fromisoformat(r["date"])) for r in records]  # 我们内部是 date 对象
    assert fingerprint(mine) == reference_fingerprint(records)


def test_row_order_and_duplicates_do_not_matter():
    a = [db_like("A.SZ", dt.date(2024, 6, d), 1.0) for d in (3, 4, 5)]
    shuffled = [dict(r, date=dt.date.fromisoformat(r["date"])) for r in reversed(a)]
    assert fingerprint(shuffled) == reference_fingerprint(a)


def test_state_fingerprint_changes_with_finality_but_data_fingerprint_does_not():
    base = [dict(db_like("A.SZ", dt.date(2024, 6, 3), 1.0), date=dt.date(2024, 6, 3), price_basis="raw")]
    nonfinal = [dict(r, is_final=False) for r in base]
    final = [dict(r, is_final=True) for r in base]
    assert fingerprint(nonfinal) == fingerprint(final)
    assert state_fingerprint(nonfinal) != state_fingerprint(final)


def test_service_payload_fingerprint_equals_reference(cache, fetcher, clock):
    clock.now = cn_time(2024, 6, 6, 9, 0)
    fetcher.set_rows("159915.SZ", [make_row("159915.SZ", dt.date(2024, 6, 4), 1.0), make_row("159915.SZ", dt.date(2024, 6, 5), 1.1)])
    out = MarketService(cache).price_history(["159915.SZ"], dt.date(2024, 6, 4), dt.date(2024, 6, 5))
    assert out["data_fingerprint"] == reference_fingerprint(out["data"])
    assert len(out["state_fingerprint"]) == 64
