"""日报的数据新鲜度：参照日 = 各市场「应有的最后完成交易日」，不再用「最大日期」。"""

import copy
import re
from datetime import date, datetime

import pytest
from test_digest_markdown import assert_codes_named
from test_digest_push import flat, records, rotation, sections

from trading.io.channels.chunking import render_full
from trading.io.digest_email import build_email_html, build_email_markdown
from trading.io.digest_freshness import classify, entry_symbols, reference_for, reference_label
from trading.io.digest_markdown import collect_entries
from trading.io.digest_push import build_push_parts_from_view
from trading.io.digest_view import build_view

pytestmark = pytest.mark.unit

TODAY = date(2026, 3, 10)  # 周二

# 全部是 naive UTC（北京时间 = UTC+8）
TUE_0000_BJ = datetime(2026, 3, 9, 16, 0)  # 日报定时运行：周二 00:00 → 应有数据日 周一 03-09
TUE_1100_BJ = datetime(2026, 3, 10, 3, 0)  # 周二盘中：A 股/港股都还没收盘 → 03-09
TUE_1635_BJ = datetime(2026, 3, 10, 8, 35)  # A 股已过 15:30，港股还没到 16:40 → A=03-10 港=03-09
TUE_1645_BJ = datetime(2026, 3, 10, 8, 45)  # 都已收盘 → 03-10
SAT_0000_BJ = datetime(2026, 3, 13, 16, 0)  # 周六 00:00 → 周五 03-13
MON_0000_BJ = datetime(2026, 3, 15, 16, 0)  # 周一 00:00 → 仍是周五 03-13（周末不产生新数据）


def view_at(now, singles, rotations=(), warnings=()):
    return build_view(
        records(singles), TODAY, list(rotations), list(warnings), "2026-03-10 13:45", False, now
    )


def with_end(raw, end):
    raw = copy.deepcopy(raw)
    raw["dateRange"]["end"] = end
    raw["latestSignal"]["timestamp"] = end
    return raw


def a_share(i, end="2026-03-09"):
    return with_end(flat(f"{159900 + i}.SZ", f"A股标的{i}"), end)


def hk(i, end="2026-03-09"):
    return with_end(flat(f"{700 + i:04d}.HK", f"港股标的{i}"), end)


def warning_texts(view):
    return [w for w in view.warnings if w.startswith(("数据落后", "含未收盘", "数据日期缺失"))]


# ---------- 参照日本身 ----------


def test_reference_dates_follow_each_markets_close_plus_buffer():
    assert reference_for(["159869.SZ"], TUE_0000_BJ)[0] == date(2026, 3, 9)
    assert reference_for(["159869.SZ"], TUE_1100_BJ)[0] == date(2026, 3, 9)
    assert reference_for(["159869.SZ"], datetime(2026, 3, 10, 7, 29))[0] == date(
        2026, 3, 9
    )  # 15:29
    assert reference_for(["159869.SZ"], datetime(2026, 3, 10, 7, 30))[0] == date(
        2026, 3, 10
    )  # 15:30
    assert reference_for(["0700.HK"], datetime(2026, 3, 10, 8, 39))[0] == date(2026, 3, 9)  # 16:39
    assert reference_for(["0700.HK"], datetime(2026, 3, 10, 8, 40))[0] == date(2026, 3, 10)  # 16:40
    assert reference_for(["159869.SZ"], SAT_0000_BJ)[0] == date(2026, 3, 13)
    assert reference_for(["159869.SZ"], MON_0000_BJ)[0] == date(2026, 3, 13)


def test_rotation_uses_the_most_conservative_reference_of_its_universe():
    entries = collect_entries(records([]), TODAY, [rotation("HOLD")]).entries
    assert entry_symbols(entries[0]) == ["159915.SZ", "513100.SS", "518880.SS"]
    assert classify(entries, TUE_0000_BJ)[id(entries[0])].ref == date(2026, 3, 9)
    mixed = copy.deepcopy(rotation("HOLD"))
    mixed["config"]["universe"] = ["159915.SZ", "0700.HK"]
    entry = collect_entries(records([]), TODAY, [mixed]).entries[0]
    assert classify([entry], TUE_1635_BJ)[id(entry)].ref == date(
        2026, 3, 9
    )  # 港股 16:35 还没定稿，取较早的


# ---------- 场景 ----------


def test_all_same_day_has_no_warning_and_header_shows_the_reference_day():
    view = view_at(TUE_0000_BJ, [a_share(0), a_share(1), hk(0)], [rotation("HOLD")])
    assert warning_texts(view) == [] and view.latest == "2026-03-09"
    text = render_full(build_push_parts_from_view(view))
    assert "数据截至 2026-03-09｜生成于" in text and "落后" not in text and "未收盘" not in text
    assert all(
        not any(r[0].startswith("数据截止") for r in c.rows) for z in view.zones.values() for c in z
    )


def test_an_individually_stale_symbol_is_named_with_days_and_the_expected_date():
    view = view_at(TUE_0000_BJ, [a_share(0), a_share(1, "2026-03-06"), hk(0)])
    assert warning_texts(view) == [
        "数据落后：1 项比应有日期（03-09）早 3 天：A股标的1（159901.SZ）"
    ]
    card = next(c for c in view.zones["空仓"] if c.title.startswith("A股标的1"))
    assert ("数据截止：2026-03-06（比应有日期 03-09 早 3 天）",) in card.rows
    other = next(c for c in view.zones["空仓"] if c.title.startswith("A股标的0"))
    assert not any(r[0].startswith("数据截止") for r in other.rows)  # 没问题的标的不加这一行


def test_stale_lines_group_by_lag_and_are_sorted_longest_first():
    view = view_at(
        TUE_0000_BJ,
        [a_share(0, "2026-03-06"), a_share(1, "2026-03-05"), a_share(2, "2026-03-06"), a_share(3)],
    )
    assert warning_texts(view) == [
        "数据落后：1 项比应有日期（03-09）早 4 天：A股标的1（159901.SZ）",
        "数据落后：2 项比应有日期（03-09）早 3 天：A股标的0（159900.SZ）、A股标的2（159902.SZ）",
    ]


def test_an_early_intraday_bar_is_reported_alone_and_does_not_make_the_others_stale():
    """真实事故：1 项带进了盘中那一根（data 03-10），其余 8 项是前一个完成交易日（03-09）。"""
    singles = [a_share(i) for i in range(8)] + [a_share(8, "2026-03-10")]
    view = view_at(TUE_1100_BJ, singles)
    assert warning_texts(view) == ["含未收盘数据：A股标的8（159908.SZ）（数据 03-10，应为 03-09）"]
    assert not any("落后" in w for w in view.warnings)
    assert view.latest == "2026-03-09"  # 顶部写参照日，不是最大日期 03-10
    card = next(c for z in view.zones.values() for c in z if c.title.startswith("A股标的8"))
    assert ("数据截止：2026-03-10（含未收盘数据，应为 03-09）",) in card.rows
    assert not any(
        "数据截止" in r[0]
        for z in view.zones.values()
        for c in z
        if not c.title.startswith("A股标的8")
        for r in c.rows
    )


def test_after_close_the_same_bar_is_normal_not_early():
    view = view_at(
        TUE_1645_BJ, [a_share(i, "2026-03-10") for i in range(3)] + [hk(0, "2026-03-10")]
    )
    assert warning_texts(view) == [] and view.latest == "2026-03-10"


def test_weekend_runs_do_not_flag_friday_data_as_stale():
    friday = [a_share(0, "2026-03-13"), hk(0, "2026-03-13")]
    for now in (SAT_0000_BJ, MON_0000_BJ):
        view = view_at(now, friday)
        assert warning_texts(view) == [] and view.latest == "2026-03-13"
    view = view_at(SAT_0000_BJ, [a_share(0, "2026-03-13"), a_share(1, "2026-03-11")])
    assert warning_texts(view) == [
        "数据落后：1 项比应有日期（03-13）早 2 天：A股标的1（159901.SZ）"
    ]


def test_hk_and_a_share_get_different_reference_days_when_their_closes_differ():
    view = view_at(TUE_1635_BJ, [a_share(0, "2026-03-10"), hk(0, "2026-03-09")])
    assert warning_texts(view) == [] and view.latest == "2026-03-10（A股）｜2026-03-09（港股）"
    assert "数据截至 2026-03-10（A股）｜2026-03-09（港股）" in render_full(
        build_push_parts_from_view(view)
    )
    # 港股提前拿到当天未收盘的一根 → 单独点名「含未收盘」，A 股不受影响
    view = view_at(TUE_1635_BJ, [a_share(0, "2026-03-10"), hk(0, "2026-03-10")])
    assert warning_texts(view) == ["含未收盘数据：港股标的0（0700.HK）（数据 03-10，应为 03-09）"]
    # 港股落后一天而 A 股正常：参照日各自独立
    view = view_at(TUE_1645_BJ, [a_share(0, "2026-03-10"), hk(0, "2026-03-09")])
    assert warning_texts(view) == ["数据落后：1 项比应有日期（03-10）早 1 天：港股标的0（0700.HK）"]


def test_phone_lists_at_most_three_names_with_deng_and_email_lists_all():
    singles = [a_share(i, "2026-03-06") for i in range(5)] + [a_share(5)]
    view = view_at(TUE_0000_BJ, singles)
    phone = warning_texts(view)[0]
    assert (
        phone
        == "数据落后：5 项比应有日期（03-09）早 3 天：A股标的0（159900.SZ）、A股标的1（159901.SZ）、A股标的2（159902.SZ）等"
    )
    assert "159903.SZ" not in phone and "159904.SZ" not in phone
    full = next(w for w in view.warnings_full if w.startswith("数据落后"))
    assert all(f"（15990{i}.SZ）" in full for i in range(5)) and not full.endswith("等")
    banner = render_full(build_push_parts_from_view(view)).split("\n## ")[0]
    assert "> ⚠️ 数据落后：5 项比应有日期（03-09）早 3 天：" in banner and "159903.SZ" not in banner
    email_text = build_email_markdown(view)
    assert all(
        f"（15990{i}.SZ）" in email_text.split("## 完整细节")[0].split("## 🟢")[0] for i in range(5)
    )
    html_doc = build_email_html(view)
    head = html_doc.split("交易信号</div>")[0]
    assert all(f"15990{i}.SZ" in head for i in range(5))  # 邮件警示框里列全


def test_missing_date_is_called_out_and_old_max_date_logic_is_gone():
    broken = flat("159915.SZ", "创业板ETF")
    broken["dateRange"] = {}
    broken["latestSignal"].pop("timestamp")
    view = view_at(TUE_0000_BJ, [a_share(0), broken])
    assert warning_texts(view) == ["数据日期缺失：创业板ETF（159915.SZ）"]
    # 唯一一个标的比参照日早也要报；全部同样早也报（不再有「以最大日期为准」的相对判断）
    all_old = view_at(TUE_0000_BJ, [a_share(0, "2026-03-04"), a_share(1, "2026-03-04")])
    assert warning_texts(all_old) == [
        "数据落后：2 项比应有日期（03-09）早 5 天：A股标的0（159900.SZ）、A股标的1（159901.SZ）"
    ]
    assert all_old.latest == "2026-03-09"


def test_reference_label_is_none_without_entries_and_names_stay_with_codes():
    assert reference_label({}) is None
    view = view_at(
        TUE_0000_BJ,
        [a_share(0, "2026-03-06"), hk(0, "2026-03-10")],
        [rotation("HOLD", since="2026-03-05")],
    )
    text = render_full(build_push_parts_from_view(view)) + build_email_markdown(view)
    assert assert_codes_named(text) > 5 and "|---" not in text
    assert [h for h in sections(render_full(build_push_parts_from_view(view)))] == [
        "🟢 买入信号",
        "🔴 卖出信号",
        "🔄 换仓信号",
        "🔵 持有状态",
        "⚪ 空仓状态",
    ]
    assert re.search(r"数据截至 2026-03-09｜生成于", text)
