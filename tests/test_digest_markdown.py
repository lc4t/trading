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


def test_full_markdown_contains_all_decision_fields():
    md = build()
    for expected in (
        "# 📊 交易信号日报 · 2026-03-10",
        "## 🎯 信号总览",
        "## 📌 详情",
        "## ⚠️ 边界声明",
        "示例持有ETF",
        "159941.SZ",
        "持有",
        "浮动盈亏",  # 动作/持仓
        "止损位：吊灯止损 1.111",
        "距现价 9.97%",
        "触发条件：MA18",  # 止损与条件
        "交易记录：2026-02-01 BUY",
        "原因：MA 金叉",
        "原因：吊灯止损触发",  # 最近交易与原因
        "数据日期：2026-03-10",
        "年化收益率 12.35%",
        "最大回撤 -24.47%",
        "总盈亏 ¥1,234.50".replace(",", ""),
        "夏普比率 1.23",
        "胜率 55.50%",
        "平均持仓 21天",
        "年度收益",
        "2025 +8.12%",
        "轮动目标：518880.SS（华安易富黄金ETF） → 513100.SS（国泰纳斯达克100ETF）",
        "**动量排名**",
        "1. ✅ 国泰纳斯达克100ETF（513100.SS） 动量 +0.0812",
        "目标权重 0.95",
        "动量 暂无",
        "最近换仓：159915.SZ（创业板ETF）",
        "收益 +10.00%",
        "下次预期动作：换仓",
        "总收益 +45.67%",
        "最大回撤 -21.30%",
        "top\\_n=1",
        "不构成投资建议",
    ):
        assert expected in md, expected
    assert "不应出现在消息里的说明文字" not in md  # 指标说明是静态文档，不是意见


def test_markdown_uses_only_supported_syntax_no_tables_or_html():
    md = build()
    assert "|" not in md.replace("｜", "")  # 无表格管道符
    assert "<" not in md and "![" not in md and "](" not in md  # 无 HTML/图片/链接


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


def test_igotify_long_message_is_sent_whole_and_logs_hint(capsys):
    seen, fake = capture()
    long_md = "# t\n" + "- 行\n" * 5000
    send_all(
        Message("t", "b", "s", markdown=long_md),
        {k: v for k, v in ENV.items() if k.startswith("IGOTIFY")},
        urlopen=fake,
        sleep_fn=lambda _: None,
    )
    assert json.loads(seen[0].data)["message"] == long_md
    assert "未截断" in capsys.readouterr().err


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
