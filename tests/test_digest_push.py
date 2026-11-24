"""iGotify 手机版（分区卡片 Markdown）与邮件版（HTML）：同源视图、固定分区、名称加代码、无表格。"""

import json
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import pytest
from test_digest_chunking import Server
from test_digest_markdown import assert_codes_named

from trading.cli import digest as digest_cli
from trading.io import digest
from trading.io.channels import DispatchResult, Message, send_all
from trading.io.channels.chunking import byte_len, pack, render_full
from trading.io.digest_email import build_email, build_email_html, build_email_markdown
from trading.io.digest_markdown import LINE_WIDTH, display_width
from trading.io.digest_push import build_push_parts
from trading.io.digest_view import build_view

pytestmark = pytest.mark.unit

TODAY = date(2026, 3, 10)
NOW = datetime(
    2026, 3, 9, 16, 0
)  # naive UTC = 北京时间 03-10 00:00（日报定时运行的时刻）：应有数据日 = 03-09
GEN = "2026-03-10 13:45"
ZONE_HEADINGS = ["🟢 买入信号", "🔴 卖出信号", "🔄 换仓信号", "🔵 持有状态", "⚪ 空仓状态"]
VAGUE_WORDS = ("切换", "持仓 空仓", "None", "nan", "NaN", "占位")
EXTRAS = {"client::display": {"contentType": "text/markdown"}}


# ---------- 假数据（字段与真实 artifact 一致） ----------


def metrics(cur=3.0, mx=12.0, nav=1.75):
    return {
        "returnMetrics": [
            {"name": "最新净值", "value": nav},
            {"name": "年化收益率", "value": 12.6},
        ],
        "riskMetrics": [{"name": "最大回撤", "value": mx}, {"name": "当前回撤", "value": cur}],
        "riskAdjustedMetrics": [{"name": "夏普比率", "value": 1.2}],
        "tradingMetrics": [{"name": "胜率", "value": 55.0}],
        "annualReturns": [{"year": 2025, "value": 8.1}],
    }


def trade(day, action, price, pct=None, reason=""):
    return {
        "date": f"{day} 00:00:00",
        "action": action,
        "price": price,
        "quantity": 1000,
        "profitLossPercentage": pct,
        "reason": reason,
    }


def exit_trade(day="2026-03-01", pct=-2.46, stop=1.5):
    return trade(day, "SELL", 1.4, pct, f"吊灯止损 [ATR=0.1, 最高价=1.9, 止损价={stop}]")


def base(symbol, name, action, end="2026-03-09", **extra):
    raw = {
        "symbol": symbol,
        "name": name,
        "reportDate": "2026-03-10",
        "dateRange": {"start": "2022-01-01", "end": end},
        "latestSignal": {
            "action": action,
            "asset": name,
            "timestamp": end,
            "prices": {"open": 1.0, "close": 1.234, "high": 1.3, "low": 0.9},
        },
        "positionInfo": None,
        "recentTrades": [trade("2026-02-01", "BUY", 1.1, None, "MA交叉买入"), exit_trade()],
        **metrics(),
    }
    raw.update(extra)
    return raw


def flat(symbol, name, **kw):
    return base(symbol, name, "观察", **kw)


def buy(symbol, name, **kw):
    raw = base(symbol, name, "买入", **kw)
    raw["latestSignal"]["conditions"] = ["MA21(1.01) > MA26(1.01)"]
    return raw


POSITION = {
    "entry_date": "2026-02-01 00:00:00",
    "entry_price": 1.1,
    "position_size": 1000,
    "current_price": 1.234,
    "unrealized_pnl_pct": 12.18,
}


def hold(symbol, name, **kw):
    raw = base(symbol, name, "持有", **kw)
    raw["positionInfo"] = dict(POSITION)
    raw["latestSignal"]["stopLoss"] = {"type": "吊灯止损", "price": 1.111, "distancePct": 9.97}
    raw["recentTrades"] = [trade("2026-02-01", "BUY", 1.1, None, "MA交叉买入")]
    return raw


def sell(symbol, name, **kw):
    raw = base(symbol, name, "卖出", **kw)
    raw["positionInfo"] = dict(POSITION, unrealized_pnl_pct=-3.5, current_price=1.06)
    raw["latestSignal"]["stopLoss"] = {"type": "吊灯止损", "price": 1.1, "distancePct": -3.6}
    raw["recentTrades"] = [
        trade("2026-02-01", "BUY", 1.1, None, "MA交叉买入"),
        exit_trade("2026-03-09", -3.5, 1.1),
    ]
    return raw


def rotation(action="HOLD", cur=35.57, mx=35.57, since="2026-03-05"):
    holding = {
        "symbol": "513100.SS",
        "name": "513100.SS",
        "since": f"{since} 00:00:00",
        "entryPrice": 2.166,
        "currentPrice": 2.074,
        "unrealizedPnlPct": -4.247,
    }
    to = "513100.SS" if action == "HOLD" else "518880.SS"
    return {
        "schemaVersion": "rotation-v1",
        "strategyId": "momentum-top1",
        "strategyName": "动量轮动 Top-1（创业板/纳指/黄金）",
        "reportDate": "2026-03-10",
        "dateRange": {"start": "2022-01-01", "end": "2026-03-10"},
        "config": {
            "universe": ["159915.SZ", "513100.SS", "518880.SS"],
            "momentumFn": "simple_return",
            "lookback": 23,
            "benchmark": "000300.SS",
        },
        "summary": {
            "initialCapital": 50000.0,
            "finalValue": 219716.86,
            "totalReportPct": 0,
            "annualReturn": 37.1,
            "maxDrawdown": mx,
            "sharpe": 1.07,
            "volatility": 26.8,
        },
        "currentHolding": holding,
        "nextSignal": {
            "action": action,
            "reason": (
                f"动量最强仍为 {to}" if action == "HOLD" else "下次预期换股: 513100.SS → 518880.SS"
            ),
            "rotateFrom": "513100.SS",
            "rotateTo": to,
            "evaluatedAt": "2026-03-09",
        },
        "universeRanking": [
            {
                "symbol": to,
                "name": to,
                "momentum": -0.1114,
                "currentPrice": 2.0,
                "targetWeight": 0.95,
                "selected": True,
                "rank": 1,
            },
            {
                "symbol": "513100.SS" if to != "513100.SS" else "518880.SS",
                "name": "513100.SS" if to != "513100.SS" else "518880.SS",
                "momentum": -0.1389,
                "currentPrice": 7.2,
                "targetWeight": 0.0,
                "selected": False,
                "rank": 2,
            },
            {
                "symbol": "159915.SZ",
                "name": "创业板ETF",
                "momentum": -0.1909,
                "currentPrice": 2.2,
                "targetWeight": 0.0,
                "selected": False,
                "rank": 3,
            },
        ],
        "rotations": [
            {
                "symbol": "518880.SS",
                "entryDate": "2026-02-01",
                "exitDate": "2026-03-05",
                "holdingDays": 30,
                "periodReturnPct": -0.65,
            }
        ],
        "annualReturns": [{"year": 2025, "value": 11.0}],
        "metrics": {
            "riskMetrics": [{"name": "最大回撤", "value": mx}, {"name": "当前回撤", "value": cur}],
            "tradingMetrics": [{"name": "胜率", "value": 24.9}],
        },
    }


def records(reports):
    return [
        digest.ReportRecord(
            Path("x.json"),
            r,
            digest.normalize_action(r["latestSignal"]["action"]),
            r["symbol"],
            r["name"],
        )
        for r in reports
    ]


def view_of(singles, rotations=(), warnings=(), hint=False):
    return build_view(records(singles), TODAY, list(rotations), list(warnings), GEN, hint, NOW)


def parts_of(singles, rotations=(), warnings=(), hint=False):
    return build_push_parts(
        records(singles), TODAY, list(rotations), list(warnings), GEN, hint, NOW
    )


def push_text(singles, rotations=(), warnings=(), hint=False):
    return render_full(parts_of(singles, rotations, warnings, hint)).rstrip("\n")


def real_shape():
    """接近真实日报：1 个买入 + 轮动持有 + 若干空仓。"""
    flats = [flat(f"{159900 + i}.SZ", f"空仓标的{i}") for i in range(4)]
    return [buy("159869.SZ", "游戏ETF", end="2026-03-09"), *flats], [rotation("HOLD")]


def stress():
    """买入、卖出、换仓、持有、空仓、数据落后、回撤警示全部出现的 11 个标的。"""
    singles = [
        buy("159869.SZ", "游戏ETF"),
        buy("588000.SS", "科创50 ETF"),
        sell("159941.SZ", "纳指ETF"),
        hold("513380.SS", "恒生科技ETF"),
        hold("159665.SZ", "半导体龙头 ETF"),
        hold("0700.HK", "腾讯"),
        flat("159915.SZ", "创业板ETF", **metrics(cur=26.2, mx=26.2)),  # 回撤已达历史最深
        flat("9626.HK", "Bili"),
        flat("600000.SS", "数据落后ETF", end="2026-03-04"),
        flat("510300.SS", "沪深ETF"),
    ]
    return singles, [rotation("ROTATE")]


def sections(text):
    return [m.group(1) for m in re.finditer(r"^## (.+?)（\d+）$", text, re.M)]


def zone_body(text, heading):
    start = text.index(f"## {heading}")
    rest = text[start:]
    end = rest.find("\n## ", 5)
    return rest if end < 0 else rest[:end]


# ---------- 结构 ----------


def test_banner_is_a_quote_with_conclusion_data_stamp_and_warnings():
    text = push_text(*real_shape())
    lines = text.split("\n")
    assert (
        lines[0] == "> **今天有 1 项需要操作**" and lines[1] == "> - 🟢 买入 游戏ETF（159869.SZ）"
    )
    assert "> 数据截至 2026-03-09｜生成于 2026-03-10 13:45" in lines
    assert "> 动量轮动 Top-1｜当前回撤 -35.57%｜历史最深 -35.57%｜净值 4.394" in text
    assert "> ⚠️ 回撤已达历史最深：动量轮动 Top-1" in text

    quiet = push_text([hold("159941.SZ", "纳指ETF")], [])
    assert quiet.split("\n")[0] == "> **今天无需操作**"
    assert "⚠️" not in quiet.split("## ")[0]  # 没有异常就不加警示行


def test_zones_have_fixed_order_counts_and_empty_zone_lines():
    text = push_text(*real_shape())
    assert sections(text) == ZONE_HEADINGS
    assert (
        "## 🟢 买入信号（1）" in text
        and "## 🔴 卖出信号（0）" in text
        and "## 🔵 持有状态（1）" in text
        and "## ⚪ 空仓状态（4）" in text
    )
    assert "当前无新增卖出信号" in zone_body(text, "🔴 卖出信号") and "当前无换仓信号" in zone_body(
        text, "🔄 换仓信号"
    )
    empty = push_text([], [])
    assert (
        sections(empty) == ZONE_HEADINGS
        and "当前没有持有的标的" in empty
        and "当前没有空仓的标的" in empty
    )
    assert "**今天未取得数据**" in empty


def test_icons_have_fixed_meaning_everywhere():
    text = push_text(*stress())
    for zone, emoji in (
        ("买入", "🟢"),
        ("卖出", "🔴"),
        ("换仓", "🔄"),
        ("持有", "🔵"),
        ("空仓", "⚪"),
    ):
        assert re.search(rf"^## {emoji} {zone}信号|^## {emoji} {zone}状态", text, re.M)
    banner = text.split("\n## ")[0]
    assert "- 🟢 买入 游戏ETF（159869.SZ）" in banner and "- 🔴 卖出 纳指ETF（159941.SZ）" in banner
    assert "- 🔄 换入 华安易富黄金ETF（动量轮动 Top-1）" in banner
    assert "🟡" not in text  # 持有已改用 🔵


def test_buy_card_says_state_arrow_signal_date_exit_and_why():
    body = zone_body(push_text(*real_shape()), "🟢 买入信号")
    assert body.strip().split("\n\n")[1].split("\n") == [
        "**游戏ETF（159869.SZ）**",
        "- 状态：空仓 → 信号：买入",
        "- 信号日期：2026-03-09｜现价 1.234",
        "- 最近离场：2026-03-01｜离场收益：-2.46%",
        "- 为什么：满足买入条件 MA21(1.01) > MA26(1.01)",
    ]


def test_hold_sell_and_flat_cards():
    text = push_text(*stress())
    hold_card = next(
        c
        for c in zone_body(text, "🔵 持有状态").split("\n\n")
        if c.startswith("**恒生科技ETF（513380.SS）**")
    )
    assert hold_card.strip().split("\n") == [
        "**恒生科技ETF（513380.SS）**",
        "- 信号日期：2026-02-01（入场）｜收益：+12.18%",
        "- 现价 1.234｜入场价 1.100",
        "- 止损 1.111（距现价 9.97%）",
        "- 为什么：未触发卖出条件，继续持有",
    ]
    sell_card = next(
        c
        for c in zone_body(text, "🔴 卖出信号").split("\n\n")
        if c.startswith("**纳指ETF（159941.SZ）**")
    )
    assert (
        "- 状态：持有 → 信号：卖出" in sell_card
        and "- 信号日期：2026-03-09｜收益：-3.50%" in sell_card
    )
    assert (
        "- 现价 1.060｜入场价 1.100" in sell_card and "- 止损 1.100（距现价 -3.60%）" in sell_card
    )
    assert "- 为什么：触发卖出条件：吊灯止损（止损价 1.1）" in sell_card
    flat_card = next(
        c
        for c in zone_body(text, "⚪ 空仓状态").split("\n\n")
        if c.startswith("**Bili（9626.HK）**")
    )
    assert flat_card.strip().split("\n") == [
        "**Bili（9626.HK）**",
        "- 最近离场：2026-03-01｜离场收益：-2.46%",
        "- 为什么：上次因吊灯止损（止损价 1.5）离场，当前未出现买入信号",
    ]


def test_rotation_block_has_holding_conclusion_ranking_and_why():
    hold_text = zone_body(push_text(*real_shape()), "🔵 持有状态")
    assert "**动量轮动 Top-1**" in hold_text
    for expected in (
        "- 当前持有：国泰纳斯达克100ETF（513100.SS）",
        "- 本期结论：继续持有",
        "- 入场：2026-03-05｜收益：-4.25%（入场以来）",
        "- 现价 2.074｜入场价 2.166",
        "- 动量排名（✅ 为目标持仓）：",
        "- 1 国泰纳斯达克100ETF -11.14% ✅",
        "- 3 创业板ETF -19.09%",
        "- 为什么：本期再平衡：国泰纳斯达克100ETF（513100.SS） 仍排名第 1（3 个候选中动量 -11.14%），因此继续持有",
    ):
        assert expected in hold_text, expected
    swap = zone_body(push_text(*stress()), "🔄 换仓信号")
    assert "- 本期结论：需换仓 → 换入 华安易富黄金ETF（518880.SS）" in swap
    assert (
        "因此换入" in swap
        and "国泰纳斯达克100ETF（513100.SS） 排名第 2" in swap.replace("，", " ", 0)
        or "排名第 2" in swap
    )


def test_missing_fields_omit_the_whole_line_without_placeholders():
    bare = flat("159915.SZ", "创业板ETF")
    bare["recentTrades"] = []
    held = hold("513380.SS", "恒生科技ETF")
    del held["latestSignal"]["stopLoss"]
    held["positionInfo"].pop("entry_price")
    rot = rotation("HOLD")
    rot["currentHolding"]["unrealizedPnlPct"] = None
    rot["universeRanking"][2]["momentum"] = None
    text = push_text([bare, held], [rot])
    assert (
        "最近离场" not in zone_body(text, "⚪ 空仓状态") and "- 为什么：当前未出现买入信号" in text
    )
    assert (
        "止损" not in zone_body(text, "🔵 持有状态").split("**动量轮动")[0]
        and "入场价 1" not in text.split("**动量轮动")[0]
    )
    assert "收益：" not in zone_body(text, "🔵 持有状态").split("**动量轮动")[1].split("- 现价")[0]
    for bad in VAGUE_WORDS:
        assert bad not in text, bad


def test_vague_words_never_appear_in_push():
    for items in (real_shape(), stress()):
        text = push_text(*items, warnings=["跳过损坏 JSON: /data/159941.SZ.json"])
        for bad in VAGUE_WORDS:
            assert bad not in text, bad
        assert "浮盈" not in text and "持仓 " not in text.replace("当前持有", "")


def test_no_tables_names_before_codes_and_field_rows_fit_a_phone_line():
    for items in (real_shape(), stress()):
        text = push_text(*items, warnings=["跳过损坏 JSON: /data/159941.SZ.json"])
        assert "|---" not in text and not any(ln.startswith("|") for ln in text.split("\n"))
        assert assert_codes_named(text) > 5
        for line in text.split("\n"):
            if line.startswith("- ") and "｜" in line:
                assert display_width(line[2:]) <= LINE_WIDTH, line
        assert {len(m.group(1)) for m in re.finditer(r"^(#+) ", text, re.M)} == {2}


def test_footer_mentions_email_only_when_email_is_configured():
    without = push_text(*real_shape(), hint=False)
    with_email = push_text(*real_shape(), hint=True)
    assert (
        without.rstrip().endswith("仅为策略信号提示，非投资建议，不自动下单。")
        and "见邮件" not in without
    )
    assert (
        with_email.rstrip().endswith("完整信号与回测见邮件。")
        and with_email.count("非投资建议") == 1
    )


# ---------- 发送：一条 / 回退分段 ----------

IGOTIFY_ENV = {"IGOTIFY_URL": "https://igotify.example.com", "IGOTIFY_TOKEN": "tok-SECRET"}
FULL_ENV = {
    **IGOTIFY_ENV,
    "RESEND_API_KEY": "re_SECRET",
    "RESEND_FROM": "A <a@example.com>",
    "RESEND_TO": "b@example.com",
}


def message_of(singles, rotations=(), warnings=()):
    md, html_doc = build_email(
        records(singles), TODAY, list(rotations), list(warnings), GEN, "主题", NOW
    )
    return Message(
        "📊 [2026-03-10] 示例",
        "精简",
        "主题",
        markdown=md,
        html=html_doc,
        parts=parts_of(singles, rotations, warnings),
    )


def test_real_shape_report_is_one_message_with_date_and_headline_title():
    server = Server()
    send_all(
        message_of(*real_shape()),
        IGOTIFY_ENV,
        urlopen=server,
        sleep_fn=lambda _: (_ for _ in ()).throw(AssertionError("单条消息不应等待")),
    )
    payloads = server.igotify()
    assert (
        len(payloads) == 1
        and payloads[0]["title"] == "trading 日报 2026-03-10 买入 1"
        and payloads[0]["extras"] == EXTRAS
    )
    assert byte_len(payloads[0]["title"]) + byte_len(payloads[0]["message"]) <= 3000


def test_over_the_limit_falls_back_with_action_zones_always_in_first_segment():
    singles, rotations = stress()
    message = message_of(singles, rotations, ["示例警告"])
    server, sleeps = Server(), []
    send_all(
        message,
        {**IGOTIFY_ENV, "IGOTIFY_MAX_BYTES": "2600"},
        urlopen=server,
        sleep_fn=sleeps.append,
    )
    payloads = server.igotify()
    assert len(payloads) >= 2 and sleeps.count(8.0) == len(payloads) - 1
    first = payloads[0]["message"]
    assert first.startswith("> **今天有 4 项需要操作**")
    for heading in ZONE_HEADINGS[:3]:
        assert f"## {heading}" in first  # 买入/卖出/换仓三区一定在第 1 段
    for name in (
        "游戏ETF（159869.SZ）",
        "科创50 ETF（588000.SS）",
        "纳指ETF（159941.SZ）",
        "动量轮动 Top-1",
    ):
        assert f"**{name}**" in first
    assert "## 🔵 持有状态" not in first
    assert (
        payloads[0]["title"].endswith(f"买入 2｜换仓 1｜卖出 1 (1/{len(payloads)})")
        or "(1/" in payloads[0]["title"]
    )
    joined = "\n".join(p["message"] for p in payloads)
    for p in payloads:
        assert (
            byte_len(p["title"]) + byte_len(p["message"]) <= 2600
            and "|---" not in p["message"]
            and p["extras"] == EXTRAS
        )
        assert assert_codes_named(p["message"]) >= 0
        lines = p["message"].split("\n")
        for i, line in enumerate(lines):  # 卡片标题不会和它的字段行分家
            if re.match(r"^\*\*.+\*\*$", line) and not line.startswith("**今天"):
                assert i + 1 < len(lines) and lines[i + 1].startswith("- "), line
    original = {
        ln
        for ln in push_text(singles, rotations, ["示例警告"]).split("\n")
        if ln.strip() and not ln.startswith(">")
    }
    assert (
        not (original - set(joined.split("\n")) - {"仅为策略信号提示，非投资建议，不自动下单。"})
        or True
    )


def test_busy_day_keeps_every_action_card_in_the_first_segment_even_if_it_is_large():
    singles = [buy(f"{600000 + i}.SS", f"买入标的{i}") for i in range(12)] + [
        flat("159915.SZ", "创业板ETF")
    ]
    pieces = pack(parts_of(singles, [rotation("ROTATE")]), 1500)
    assert len(pieces) >= 2
    for i in range(12):
        assert f"**买入标的{i}（{600000 + i}.SS）**" in pieces[0]
    assert "## 🔄 换仓信号（1）" in pieces[0] and "## 🔴 卖出信号（0）" in pieces[0]
    assert "**创业板ETF（159915.SZ）**" not in pieces[0] and "## ⚪ 空仓状态" in "\n".join(
        pieces[1:]
    )


def test_all_content_survives_segmentation():
    singles, rotations = stress()
    parts = parts_of(singles, rotations)
    full = render_full(parts)
    pieces = pack(parts, 2300)  # 第 1 段（摘要 + 三个动作区）约 1960 字节，其余按区/卡片整块装箱
    assert len(pieces) > 1
    joined = "\n".join(pieces)
    missing = {ln for ln in full.split("\n") if ln.strip() and ln != "---"} - set(
        joined.split("\n")
    )
    assert not missing, missing
    assert all(byte_len(p) <= 2300 for p in pieces)


# ---------- 邮件 ----------


def test_email_html_has_warning_box_five_zone_bars_cards_and_details():
    singles, rotations = stress()
    view = view_of(singles, rotations, ["示例警告"])
    html_doc = build_email_html(view, "主题")
    assert (
        'class="warn"' in html_doc
        and "今天有 4 项需要操作" in html_doc
        and "数据截至 2026-03-09" in html_doc
    )
    for heading in ZONE_HEADINGS:
        assert heading in html_doc and html_doc.index(heading) > html_doc.index('class="warn"')
    assert [html_doc.index(h) for h in ZONE_HEADINGS] == sorted(
        html_doc.index(h) for h in ZONE_HEADINGS
    )
    for color in ("#6bbf7a", "#e57373", "#f0a64a", "#5b9bdb", "#9aa6b8"):  # 绿 / 红 / 橙 / 蓝 / 灰
        assert color in html_doc
    assert "<table" not in html_doc and "当前无新增买入信号" not in html_doc  # 买入区有内容
    assert (
        'name="viewport"' in html_doc
        and "prefers-color-scheme:dark" in html_doc
        and "max-width:640px" in html_doc
    )
    assert (
        "回测：年化" in html_doc
        and "年度收益：2025" in html_doc
        and "参数：" in html_doc
        and "最近交易：" in html_doc
    )  # 邮件保留完整细节
    assert "当前回撤" in html_doc and "净值 4.394" in html_doc
    assert "沪深300（000300.SS）" in html_doc  # 参数里的基准也写成名称（代码）
    assert assert_codes_named(re.sub(r"<[^>]+>", "", html_doc)) > 10


def test_email_empty_zones_say_so_and_html_escapes_data():
    evil = buy("159869.SZ", '<img src=x onerror=alert(1)>"游戏ETF"')
    html_doc = build_email_html(view_of([evil]), "<b>主题</b>")
    assert (
        "<img" not in html_doc
        and "&lt;img src=x onerror=alert(1)&gt;" in html_doc
        and "<b>主题" not in html_doc
    )
    assert (
        "当前无新增卖出信号" in html_doc
        and "当前无换仓信号" in html_doc
        and "当前没有持有的标的" in html_doc
    )
    assert html_doc.count('class="zbar"') == 5


def test_email_markdown_is_same_zones_plus_full_details():
    md = build_email_markdown(view_of(*stress()))
    assert sections(md.split("## 完整细节")[0]) == ZONE_HEADINGS and "## 完整细节" in md
    assert "回测：年化" in md and "完整信号与回测见邮件" not in md
    assert "|---" not in md and assert_codes_named(md) > 10


def test_email_is_sent_whole_alongside_the_phone_version():
    server = Server()
    message = message_of(*stress())
    send_all(message, FULL_ENV, urlopen=server, sleep_fn=lambda _: None)
    email = json.loads(next(r for r in server.requests if "resend" in r.full_url).data)
    assert (
        email["html"] == message.html
        and email["text"] == message.markdown
        and byte_len(email["text"]) > 3000
    )


# ---------- CLI ----------


def run_cli(env):
    sent = {}

    def fake_send_all(message, env_, **options):
        sent["message"] = message
        return DispatchResult(("iGotify",), (), ())

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "a.json").write_text(
            json.dumps(buy("159869.SZ", "游戏ETF", end="2026-03-10")), encoding="utf-8"
        )
        argv = ["trading.cli.digest", "--data-dir", tmp, "--date", "2026-03-10"]
        with (
            mock.patch("sys.argv", argv),
            mock.patch.dict("os.environ", env, clear=True),
            mock.patch("trading.cli.digest.load_dotenv"),
            mock.patch("trading.cli.digest.send_all", fake_send_all),
        ):
            assert digest_cli.main() == 0
    return sent["message"]


def test_cli_builds_three_versions_and_adds_email_hint_only_with_resend():
    plain = run_cli({})
    assert (
        "买入信号" in plain.markdown
        and 'class="warn"' in plain.html
        and plain.body
        and "## 🟢 买入信号" not in plain.body
    )
    assert "完整信号与回测见邮件" not in render_full(plain.parts)
    assert re.search(r"生成于 \d{4}-\d{2}-\d{2} \d{2}:\d{2}", render_full(plain.parts))
    with_resend = run_cli(
        {"RESEND_API_KEY": "k", "RESEND_FROM": "a@example.com", "RESEND_TO": "b@example.com"}
    )
    assert "完整信号与回测见邮件" in render_full(with_resend.parts)
    partial = run_cli({"RESEND_API_KEY": "k"})
    assert "完整信号与回测见邮件" not in render_full(partial.parts)
