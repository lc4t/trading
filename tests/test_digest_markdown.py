"""完整版日报 Markdown / HTML 邮件 / iGotify extras 的测试（假数据，不联网）。"""

import json
import re
import tempfile
from datetime import date
from pathlib import Path

import pytest

from trading.io import digest
from trading.io.channels import DispatchResult, Message, send_all
from trading.io.channels.markup import markdown_to_html, md_escape
from trading.io.digest_markdown import build_full_markdown
from trading.io.json_export import signal_extras

pytestmark = pytest.mark.unit

TODAY = date(2026, 3, 10)
EVIL = "<img src=x onerror=alert(1)> [点我](https://evil.example/?t=1) ![](https://evil.example/p.png) **粗** _斜_"


def metric(name, value):
    return {"name": name, "value": value, "description": "不应出现在消息里的说明文字"}


def hold_report(name="示例持有ETF"):
    return {
        "symbol": "159941.SZ",
        "name": name,
        "reportDate": "2026-03-10",
        "dateRange": {"start": "2022-01-01", "end": "2026-03-10"},
        "latestSignal": {
            "action": "持有",
            "timestamp": "2026-03-10",
            "prices": {"open": 1.1, "close": 1.234, "high": 1.3, "low": 1.05},
            "stopLoss": {"type": "吊灯止损", "price": 1.111, "distancePct": 9.97},
            "conditions": ["MA18(1.20) > MA25(1.15)"],
        },
        "positionInfo": {
            "entry_date": "2026-02-01 00:00:00",
            "entry_price": 1.1,
            "position_size": 1000,
            "current_value": 1234.0,
            "cost": 1100.0,
            "unrealized_pnl": 134.0,
            "unrealized_pnl_pct": 12.18,
        },
        "recentTrades": [
            {
                "date": "2026-01-05",
                "action": "SELL",
                "price": 1.0,
                "quantity": 500,
                "profitLossPercentage": 3.5,
                "reason": "吊灯止损触发",
            },
            {
                "date": "2026-02-01",
                "action": "BUY",
                "price": 1.1,
                "quantity": 1000,
                "profitLossPercentage": None,
                "reason": "MA 金叉",
            },
        ],
        "annualReturns": [{"year": 2025, "value": 8.123}],
        "returnMetrics": [metric("年化收益率", 12.3456), metric("总盈亏", 1234.5)],
        "riskMetrics": [metric("最大回撤", 24.47), metric("波动率", 18.2)],
        "riskAdjustedMetrics": [metric("夏普比率", 1.234)],
        "tradingMetrics": [metric("总交易次数", 12), metric("胜率", 55.5)],
        "positionMetrics": [metric("平均持仓", "21天")],
    }


def rotation_report():
    return {
        "schemaVersion": "rotation-v1",
        "strategyId": "momentum-top1",
        "strategyName": "动量轮动 Top-1（创业板/纳指/黄金）",
        "reportDate": "2026-03-10",
        "config": {"top_n": 1, "lookback": 23},
        "dateRange": {"start": "2022-01-01", "end": "2026-03-10"},
        "summary": {
            "totalReturnPct": 45.67,
            "annualReturn": 9.8,
            "maxDrawdown": 21.3,
            "sharpe": 0.91,
            "volatility": 15.2,
        },
        "currentHolding": {
            "symbol": "518880.SS",
            "name": "518880.SS",
            "since": "2026-02-20",
            "entryPrice": 5.0,
            "currentPrice": 5.25,
            "unrealizedPnlPct": 5.0,
        },
        "nextSignal": {
            "action": "ROTATE",
            "reason": "下次预期换股: 518880.SS → 513100.SS",
            "rotateFrom": "518880.SS",
            "rotateTo": "513100.SS",
            "evaluatedAt": "2026-03-10 00:00:00",
        },
        "universeRanking": [
            {
                "symbol": "513100.SS",
                "name": "513100.SS",
                "momentum": 0.0812,
                "currentPrice": 1.9,
                "targetWeight": 0.95,
                "selected": True,
                "rank": 1,
            },
            {
                "symbol": "518880.SS",
                "name": "518880.SS",
                "momentum": 0.0301,
                "currentPrice": 5.25,
                "targetWeight": 0.0,
                "selected": False,
                "rank": 2,
            },
            {
                "symbol": "159915.SZ",
                "name": "159915.SZ",
                "momentum": None,
                "currentPrice": 3.1,
                "targetWeight": 0.0,
                "selected": False,
                "rank": 3,
            },
        ],
        "rotations": [
            {
                "symbol": "159915.SZ",
                "entryDate": "2026-01-02",
                "exitDate": "2026-02-20",
                "entryPrice": 3.0,
                "exitPrice": 3.3,
                "holdingDays": 49,
                "periodReturnPct": 10.0,
            },
        ],
        "annualReturns": [{"year": 2025, "value": 11.0}],
        "metrics": {
            "returnMetrics": [metric("年化收益率", 9.8)],
            "riskMetrics": [metric("当前回撤", 3.2)],
        },
    }


def build(reports=None, rotations=None, warnings=()):
    records = [
        digest.ReportRecord(
            Path("x.json"),
            r,
            digest.normalize_action(r["latestSignal"]["action"]),
            r["symbol"],
            r["name"],
        )
        for r in (reports if reports is not None else [hold_report()])
    ]
    return build_full_markdown(
        records, TODAY, rotations if rotations is not None else [rotation_report()], warnings
    )


def buy_report():
    r = hold_report("示例买入ETF")
    r["symbol"] = "588000.SS"
    r["latestSignal"].update(action="买入", conditions=["MA6(1.5) > MA15(1.4)"])
    r["latestSignal"].pop("stopLoss")
    r["positionInfo"] = None
    r["recentTrades"] = []
    return r


def section(md, title):
    """取 `## title` 小节的正文。"""
    start = md.index(f"## {title}")
    rest = md[start + len(title) + 3 :]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


def test_conclusion_comes_first_and_names_every_trade_signal():
    md = build(reports=[hold_report(), buy_report()])
    order = [
        md.index(f"## {t}") for t in ("今日结论", "状态总表", "标的详情", "回测与绩效", "边界声明")
    ]
    assert order == sorted(order)
    conclusion = section(md, "今日结论")
    assert "示例买入ETF" in conclusion and "买入：MA6(1.5) > MA15(1.4)" in conclusion
    assert "动量轮动 Top-1" in conclusion and "换仓：下次预期换股" in conclusion
    assert "示例持有ETF" not in conclusion  # 无动作的不进结论
    assert len([ln for ln in conclusion.splitlines() if ln.startswith("- ")]) <= 3


def test_conclusion_says_no_change_when_no_signal():
    rotation = rotation_report()
    rotation["nextSignal"].update(
        action="HOLD", reason="动量最强仍为 518880.SS", rotateTo="518880.SS"
    )
    md = build(reports=[hold_report()], rotations=[rotation])
    assert "今日无信号变化：持有 2 ｜ 空仓 0" in section(md, "今日结论")


def test_conclusion_flags_stale_data_and_missing_data():
    stale = buy_report()
    stale["dateRange"]["end"] = "2026-03-06"
    md = build(reports=[hold_report(), stale], warnings=["跳过损坏 JSON"])
    conclusion = section(md, "今日结论")
    assert (
        "数据截止日不一致，以 2026-03-10 为准" in conclusion
        and "示例买入ETF（2026-03-06）" in conclusion
    )
    assert "1 条数据提示" in conclusion and "## 数据提示" in md and "跳过损坏 JSON" in md

    empty = build(reports=[], rotations=[])
    assert "未取得任何当日数据" in section(empty, "今日结论")
    assert "## 状态总表" not in empty and "## 边界声明" in empty


def test_status_table_is_one_row_per_item_with_fixed_columns():
    md = build(reports=[hold_report(), buy_report()])
    table = [ln for ln in section(md, "状态总表").splitlines() if ln.startswith("|")]
    assert table[0] == "| 标的 | 动作 | 持仓 | 浮盈 | 止损位 | 最近切换 |"
    assert len(table) == 2 + 3
    hold_row = next(r for r in table if "示例持有ETF" in r)
    assert "🟡 持有" in hold_row and "1000股" in hold_row and "+12.18%" in hold_row
    assert "1.111" in hold_row and "2026-02-01 买入" in hold_row
    rotation_row = next(r for r in table if "动量轮动" in r)
    assert (
        "🔄 换仓" in rotation_row and "+5.00%" in rotation_row and "2026-02-20 入场" in rotation_row
    )


def test_details_have_identical_field_order_per_kind():
    md = build(reports=[hold_report(), buy_report()])
    details = section(md, "标的详情")
    symbol_blocks = [b for b in details.split("---") if "**最近交易**" in b]
    assert len(symbol_blocks) == 2
    fields = [re.findall(r"- \*\*(.+?)\*\*：", b) for b in symbol_blocks]
    assert fields[0] == fields[1] == ["动作", "现价", "持仓", "止损", "依据", "最近交易", "上一笔"]
    rotation_fields = re.findall(
        r"- \*\*(.+?)\*\*：", next(b for b in details.split("---") if "最近换仓" in b)
    )
    assert rotation_fields == ["动作", "持仓", "目标", "止损", "依据", "最近换仓", "上一次"]


def test_details_keep_decision_content_and_ranking_table():
    md = build()
    for expected in (
        "止损**：吊灯止损 1.111（距现价 9.97%）",
        "依据**：MA18(1.20) > MA25(1.15)",
        "最近交易**：2026-02-01 买入 @1.100 ×1000（MA 金叉）",
        "吊灯止损触发",
        "目标**：华安易富黄金ETF → 国泰纳斯达克100ETF",
        "最近换仓**：2026-01-02 → 2026-02-20 创业板ETF",
        "| 1 | ✅ 国泰纳斯达克100ETF | +0.0812 | 1.900 | 0.95 |",
        "| 3 | 创业板ETF | — | 3.100 | 0.00 |",
        "| # | 标的 | 动量 | 现价 | 权重 |",
    ):
        assert expected in md, expected


def test_performance_section_is_compact():
    md = build()
    perf = section(md, "回测与绩效")
    assert "| 标的 | 年化 | 最大回撤 | 当前回撤 | 夏普 | 胜率 |" in perf
    assert "| 示例持有ETF | +12.35% | -24.47% | — | 1.23 | 55.50% |" in perf
    assert "+9.80%" in perf and "-21.30%" in perf and "-3.20%" in perf
    assert "**年度收益**" in perf and "2025 +8.12%" in perf and "top\\_n=1" in perf
    assert "不应出现在消息里的说明文字" not in md and "Beta" not in md


def test_layout_rules_for_ios_renderer():
    md = build(reports=[hold_report(), buy_report()])
    lines = md.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "---":  # 前后必须空行，否则被当成 Setext 二级标题
            assert lines[i - 1] == "" and lines[i + 1] == "", i
    headings = {len(m.group(1)) for m in re.finditer(r"^(#+) ", md, re.M)}
    assert headings <= {1, 2}  # 标题不超过两级
    assert not re.search(r"(?<!\\)[<\[]", md) and "![" not in md  # 无 HTML/链接/图片
    for block in re.findall(r"(?:^\|.*\n)+", md, re.M):  # 每张表各行列数一致
        counts = {len(re.findall(r"(?<!\\)\|", row)) for row in block.strip().splitlines()}
        assert len(counts) == 1 and next(iter(counts)) <= 7  # 最多 6 列


def test_long_content_is_never_truncated():
    many = [hold_report(f"标的{i:03d}") | {"symbol": f"{i:06d}.SZ"} for i in range(150)]
    md = build(reports=many)
    assert len(md.encode()) > 40_000
    assert "标的000" in md and "标的149" in md and md.rstrip().endswith("。")


def test_user_data_cannot_inject_markdown_or_html():
    report = hold_report(EVIL)
    report["recentTrades"][1]["reason"] = EVIL
    md = build(reports=[report])
    assert not re.search(r"(?<!\\)[<\[]", md)  # 所有 < 与 [ 都已转义，不可能成为 HTML/链接/图片
    assert md_escape(EVIL) in md
    html_doc = markdown_to_html(md)
    assert "<img" not in html_doc and "<a " not in html_doc and "onerror=alert(1)>" not in html_doc
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_doc
    assert "[点我]" in html_doc  # 链接语法按字面显示，不会变成 <a>


def test_markdown_to_html_structure_and_escaping():
    html_doc = markdown_to_html(
        "# 标题 <b>\n\n- 项 **粗** & <script>alert(1)</script>\n- 二\n\n1. 甲\n2. 乙\n\n---\n\n段落\\*字面\\*",
        title='主题"<x>',
    )
    assert "<h1" in html_doc and "<ul" in html_doc and "<ol" in html_doc and "<hr" in html_doc
    assert "<strong>粗</strong>" in html_doc and "&amp;" in html_doc
    assert "<script>" not in html_doc and "&lt;script&gt;" in html_doc
    assert "<title>主题&quot;&lt;x&gt;</title>" in html_doc
    assert "*字面*" in html_doc and "<strong>字面" not in html_doc
    # 手机宽度 + 深色模式
    assert 'name="viewport"' in html_doc and "max-width:640px" in html_doc
    assert "prefers-color-scheme:dark" in html_doc and 'name="color-scheme"' in html_doc
    assert "style=" in html_doc  # 样式内联


def test_markdown_table_converts_with_escaping_and_dark_mode_hooks():
    md = "| 标的 | 值 |\n|---|---|\n| <b>x</b> \\| y | **1** |\n| 少列 |\n\n段落"
    html_doc = markdown_to_html(md)
    assert "<table" in html_doc and html_doc.count("<tr>") == 3  # 表头 + 2 行
    assert html_doc.count("<th ") == 2 and html_doc.count("<td ") == 4  # 少列补空
    assert "&lt;b&gt;x&lt;/b&gt; | y" in html_doc and "<b>" not in html_doc
    assert "overflow-x:auto" in html_doc and "border:1px solid" in html_doc
    assert "th.c{background:#1c2128" in html_doc and "<p " in html_doc
    assert "|---|" not in html_doc


def test_full_digest_html_has_tables_and_no_raw_markup():
    md = build()
    html_doc = markdown_to_html(md, "主题")
    assert html_doc.count("<table") == 3  # 状态总表 + 动量排名 + 绩效
    assert "<h2" in html_doc and "<hr" in html_doc and "<strong>" in html_doc
    assert "|---|" not in html_doc and "**" not in html_doc


def test_unclosed_bold_does_not_break_tags():
    html_doc = markdown_to_html("- **未闭合")
    assert html_doc.count("<strong>") == html_doc.count("</strong>")


def test_signal_extras_exports_stop_loss_and_conditions():
    extras = signal_extras(
        {
            "stop_loss": {"type": "吊灯止损", "price": 1.11111, "distance_pct": 9.9712},
            "conditions": ["卖出条件触发", ""],
        }
    )
    assert extras == {
        "stopLoss": {"type": "吊灯止损", "price": 1.111, "distancePct": 9.97},
        "conditions": ["卖出条件触发"],
    }
    assert signal_extras({"stop_loss": None, "conditions": []}) == {}


# ---- 渠道：iGotify extras / 邮件 html+text / PushGo 精简正文不变 ----


class Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return b'{"success": true}'

    def getcode(self):
        return 200


def capture():
    seen = []

    def fake(req, timeout=None):
        seen.append(req)
        return Resp()

    return seen, fake


ENV = {
    "IGOTIFY_URL": "https://igotify.example.com",
    "IGOTIFY_TOKEN": "tok-SECRET",
    "RESEND_API_KEY": "re_SECRET",
    "RESEND_FROM": "A <a@example.com>",
    "RESEND_TO": "b@example.com",
    "PUSHGO_CHANNEL_ID": "cid",
    "PUSHGO_PASSWORD": "pwd",
}


def make_message():
    md = build()
    return Message("标题", "精简正文", "主题", markdown=md, html=markdown_to_html(md, "主题")), md


def test_igotify_sends_full_markdown_with_extras_and_keeps_title_priority():
    seen, fake = capture()
    message, md = make_message()
    send_all(
        message,
        {k: v for k, v in ENV.items() if k.startswith("IGOTIFY")},
        urlopen=fake,
        sleep_fn=lambda _: None,
    )
    body = json.loads(seen[0].data)
    assert body["message"] == md and body["title"] == "标题" and body["priority"] == 5
    assert body["extras"] == {"client::display": {"contentType": "text/markdown"}}
    assert "精简正文" not in body["message"]


def test_igotify_without_markdown_stays_plain_without_extras():
    seen, fake = capture()
    send_all(
        Message("t", "plain", "s"),
        {k: v for k, v in ENV.items() if k.startswith("IGOTIFY")},
        urlopen=fake,
        sleep_fn=lambda _: None,
    )
    body = json.loads(seen[0].data)
    assert body["message"] == "plain" and "extras" not in body


def test_resend_sends_html_and_text_versions():
    seen, fake = capture()
    message, md = make_message()
    send_all(
        message,
        {k: v for k, v in ENV.items() if k.startswith("RESEND")},
        urlopen=fake,
        sleep_fn=lambda _: None,
    )
    payload = json.loads(seen[0].data)
    assert payload["text"] == md and payload["html"] == message.html
    assert payload["html"].startswith("<!doctype html>") and "<h1" in payload["html"]


def test_pushgo_body_is_unchanged_concise_version():
    seen, fake = capture()
    message, _ = make_message()
    send_all(
        message,
        {k: v for k, v in ENV.items() if k.startswith("PUSHGO")},
        urlopen=fake,
        sleep_fn=lambda _: None,
    )
    payload = json.loads(seen[0].data)
    assert payload["body"] == "精简正文" and payload["title"] == "标题"


def test_cli_dry_run_unchanged_and_main_builds_full_markdown(monkeypatch, capsys):
    from unittest import mock

    from trading.cli import digest as digest_cli

    sent = {}

    def fake_send_all(message, env, **options):
        sent["message"] = message
        return DispatchResult(("iGotify",), (), ())

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "a.json").write_text(json.dumps(hold_report()), encoding="utf-8")
        argv = ["trading.cli.digest", "--data-dir", tmp, "--date", "2026-03-10"]
        with (
            mock.patch("sys.argv", argv),
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch("trading.cli.digest.load_dotenv"),
            mock.patch("trading.cli.digest.send_all", fake_send_all),
        ):
            assert digest_cli.main() == 0
    message = sent["message"]
    assert "止损位" in message.markdown and "<h1" in message.html
    assert "止损位" not in message.body  # PushGo 精简正文保持原样
