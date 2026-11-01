"""用 DBClient 往目标库灌虚构行情（含 symbol_info）。基线与当前分支共用这份夹具。

全部是随机游走生成的虚构数据，固定随机种子，不含任何真实行情。
"""
import datetime as dt

import numpy as np

from trading.data.repository import DBClient

SYMBOLS = {"159915.SZ": 0.0006, "513100.SS": 0.0008, "518880.SS": 0.0004, "000300.SS": 0.0003, "0700.HK": 0.0005}

db = DBClient()
db.init_db()
for i, (sym, drift) in enumerate(SYMBOLS.items()):
    rng = np.random.default_rng(100 + i)
    price, rows, day = 2.0 + i, [], dt.date(2021, 9, 1)
    while day <= dt.date(2026, 10, 30):
        if day.weekday() < 5:
            price *= 1 + rng.normal(drift, 0.012)
            rows.append(
                {
                    "symbol": sym,
                    "date": day,
                    "open_price": price * 0.999,
                    "close_price": price,
                    "high": price * 1.01,
                    "low": price * 0.99,
                    "volume": int(1e6 + rng.integers(0, 1e5)),
                    "amount": None,
                    "change": None,
                    "change_pct": None,
                }
            )
        day += dt.timedelta(days=1)
    assert db.upsert_trading_data(rows)
    db.insert_symbol_info({"symbol": sym, "name": f"虚构标的{i}", "listing_date": dt.date(2021, 9, 1), "status": "active"})
print("seeded")
