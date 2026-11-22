"""手机推送（iGotify）精简版日报：只讲「做什么 / 现状 / 盯什么」，细节只进邮件。"""

import copy
import json
import re
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from test_digest_chunking import TODAY, Server, eleven_items
from test_digest_markdown import assert_codes_named, build, buy_report, hold_report, rotation_report

from trading.cli import digest as digest_cli
from trading.io import digest
from trading.io.channels import Message, send_all
from trading.io.channels.chunking import byte_len, pack, render_full
from trading.io.digest_markdown import LINE_WIDTH, build_full_markdown, display_width
from trading.io.digest_push import build_push_parts

pytestmark = pytest.mark.unit

BUDGET = 2900
BANNED = (
    "回测",
    "绩效",
    "年度收益",
    "策略参数",
    "夏普",
    "最大回撤",
    "胜率",
    "年化",
    "动量排名",
    "上笔",
    "最近换仓",
)


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


def push(reports, rotations, warnings=()):
    return build_push_parts(records(reports), TODAY, rotations, list(warnings))


def messages_of(reports, rotations, warnings=()):
    return pack(push(reports, rotations, warnings), BUDGET)


def quiet_items():
    """无动作日：所有单标的持有、轮动也持有。"""
    singles = []
    for i in range(8):
        r = copy.deepcopy(hold_report())
        r["symbol"], r["name"] = f"{159900 + i}.SZ", f"示例标的{i}"
        singles.append(r)
    rotations = []
    for i in range(3):
        r = copy.deepcopy(rotation_report())
        r["strategyId"], r["strategyName"] = (
            f"momentum-top{i}",
            f"动量轮动 Top-{i}（创业板/纳指/黄金）",
        )
        r["nextSignal"].update(action="HOLD", reason="动量最强仍为 518880.SS", rotateTo="518880.SS")
        rotations.append(r)
    return singles, rotations


def test_action_day_is_three_messages_with_a_short_first_one():
    msgs = messages_of(*eleven_items(), warnings=["示例警告"])
    assert len(msgs) == 3
    assert msgs[0].split("\n")[0] == "🔄 **今日需操作 5 项**"
    assert byte_len(msgs[0]) <= 600
    assert msgs[1].startswith("## 状态总览") and msgs[2].startswith("## 需关注")
    assert len(re.findall(r"^- ", msgs[0], re.M)) <= 6  # 结论只有几行要点


def test_quiet_day_is_two_messages_and_omits_details():
    msgs = messages_of(*quiet_items())
    assert len(msgs) == 2 and "需关注" not in "\n".join(msgs)
    assert msgs[0].split("\n")[0] == "✅ **今日无信号变化**" and byte_len(msgs[0]) <= 600
    assert "🟡 持有 11｜⚪ 空仓 0" in msgs[0]
    assert msgs[1].startswith("## 状态总览")


def test_push_has_no_backtest_performance_or_parameters_but_email_keeps_them():
    singles, rotations = eleven_items()
    text = "\n".join(messages_of(singles, rotations, ["示例警告"]))
    for word in BANNED:
        assert word not in text, word
    full = build(reports=[hold_report()])
    assert "## 回测与绩效" in full and "年度收益" in full and "策略参数" in full


def test_details_only_for_action_entries_and_at_most_three_lines():
    singles, rotations = eleven_items()
    detail = messages_of(singles, rotations)[2]
    for name in ("示例标的1", "示例标的2", "示例标的3", "示例标的5", "示例标的6", "示例标的7"):
        assert name not in detail  # 持有的标的不写详情
    groups = [g for g in detail.split("\n\n")[1:-1] if g]  # 去掉小标题与末尾的边界声明
    assert len(groups) == 5
    for group in groups:
        assert 2 <= len(group.split("\n")) <= 4  # 标题行 + 最多 3 行
    assert "- 依据：MA6(1.5) > MA15(1.4)" in detail and "- 触发：2026-03-10 收盘价 1.234" in detail
    assert (
        "- 目标：国泰纳斯达克100ETF（513100.SS）\n- 需换仓" in detail
    )  # 一行放不下（45>44）就拆成两行
    assert "（距现价" not in detail  # 买入的标的没有持仓，没有止损行


def test_sell_signal_detail_basis_is_one_short_sentence_and_includes_stop():
    reason = "吊灯止损触发，收盘跌破止损价，并且连续两日低于二十日均线，建议减仓观察"
    report = copy.deepcopy(hold_report("示例卖出ETF"))
    report["symbol"] = "588000.SS"
    report["latestSignal"]["action"] = "卖出"
    report["recentTrades"].append(
        {
            "date": "2026-03-10",
            "action": "SELL",
            "price": 1.0,
            "quantity": 100,
            "profitLossPercentage": -2.0,
            "reason": reason,
        }
    )
    detail = messages_of([report], [])[-1]
    assert f"- 依据：{reason[:29]}…" in detail and reason not in detail  # ≤30 字，超出删字
    assert "- 止损：1.111（距现价 9.97%）" in detail
    assert len([ln for ln in detail.split("\n") if ln.startswith("- ")]) == 3  # 依据 / 触发 / 止损


def test_every_line_fits_one_phone_line_and_uses_single_heading_level():
    for items in (eleven_items(), quiet_items()):
        for body in messages_of(*items, warnings=["示例警告"]):
            for line in body.split("\n"):
                assert display_width(line) <= LINE_WIDTH + 2, line  # 行首的「- 」不算
            assert not re.search(r"^#{1}(?!#) ", body, re.M) and not re.search(
                r"^#{3,} ", body, re.M
            )


def test_boundary_is_one_line_at_the_end_of_the_last_message():
    for items in (eleven_items(), quiet_items()):
        msgs = messages_of(*items)
        joined = "\n".join(msgs)
        assert joined.count("非投资建议") == 1
        assert msgs[-1].rstrip().endswith("仅为策略信号提示，非投资建议，不自动下单。")
        assert "非投资建议" not in "\n".join(msgs[:-1])


def test_bold_only_on_names_and_action_words():
    for body in messages_of(*eleven_items()):
        for line in body.split("\n"):
            if line.startswith("- ") and "**" in line:
                assert re.match(r"^- (?:🟢|🔄|🟡|🔴|⚪) \*\*", line)  # 只有结论里的动作行带粗体
            if line.startswith(("🟢 ", "🔄 ")) and "**" in line and "今日需操作" not in line:
                assert line.count("**") == 4  # 名称 + 动作词
            if line.startswith("🟡 "):
                assert line.count("**") == 2  # 持有：只有名称加粗
        assert not re.search(r"^- (持仓|浮盈|止损|切换|依据|触发|目标)[^\n]*\*\*", body, re.M)


def test_no_tables_and_every_code_has_a_name():
    for items in (eleven_items(), quiet_items()):
        for body in messages_of(*items, warnings=["跳过损坏 JSON: /data/159941.SZ.json"]):
            assert "|---" not in body and not any(ln.startswith("|") for ln in body.split("\n"))
            assert_codes_named(body)


def test_many_actions_are_capped_in_first_message():
    singles = []
    for i in range(11):
        r = copy.deepcopy(buy_report())
        r["symbol"], r["name"] = f"{500000 + i}.SS", f"买入标的{i}"
        singles.append(r)
    msgs = messages_of(singles, [])
    assert msgs[0].split("\n")[0] == "🔄 **今日需操作 11 项**" and byte_len(msgs[0]) <= 600
    assert len(re.findall(r"^- ", msgs[0], re.M)) <= 5 and "另有 7 项" in msgs[0]
    assert all(f"买入标的{i}" in "\n".join(msgs) for i in range(11))  # 其余在总览/详情里都有


def test_stale_entry_is_flagged_in_conclusion_and_details_even_without_action():
    singles, rotations = quiet_items()
    singles[0]["dateRange"]["end"] = "2026-03-06"
    msgs = messages_of(singles, rotations)
    assert "⚠️ 数据落后：示例标的0（159900.SZ）" in msgs[0]
    assert len(msgs) == 3 and msgs[2].startswith("## 需关注")
    assert "⚠️ **示例标的0（159900.SZ）**\n- 数据：截止 2026-03-06，落后" in msgs[2]
    assert "示例标的1" not in msgs[2]


def test_status_field_order_is_fixed_and_hold_is_three_lines():
    status = messages_of(*eleven_items())[1]
    group = next(g for g in status.split("\n\n") if "示例标的1（159901.SZ）" in g)
    assert group.split("\n") == [
        "🟡 **示例标的1（159901.SZ）** 持有",
        "- 持仓 1000股｜浮盈 +12.18%",
        "- 止损 1.111（距现价 9.97%）｜切换 2026-02-01",
    ]


def test_no_data_day_says_so_and_still_has_boundary():
    msgs = messages_of([], [])
    assert (
        len(msgs) == 1 and msgs[0].startswith("⚠️ **今日未取得数据**") and "非投资建议" in msgs[0]
    )


def make_message(singles, rotations, warnings=()):
    parts = push(singles, rotations, list(warnings))
    full = build_full_markdown(records(singles), TODAY, rotations, list(warnings))
    return Message(
        "📊 [2026-03-10] 示例", "精简", "主题", markdown=full, html="<html>完整</html>", parts=parts
    )


IGOTIFY_ENV = {"IGOTIFY_URL": "https://igotify.example.com", "IGOTIFY_TOKEN": "tok-SECRET"}
FULL_ENV = {
    **IGOTIFY_ENV,
    "RESEND_API_KEY": "re_SECRET",
    "RESEND_FROM": "A <a@example.com>",
    "RESEND_TO": "b@example.com",
}
EXTRAS = {"client::display": {"contentType": "text/markdown"}}


def real_shape_items(action: bool):
    """真实日报的数据量级：几个单标的 + 一个轮动策略。"""
    singles = []
    for i in range(4):
        r = copy.deepcopy(buy_report() if (action and i == 0) else hold_report())
        r["symbol"], r["name"] = f"{159900 + i}.SZ", f"示例标的{i}"
        singles.append(r)
    rotation = copy.deepcopy(rotation_report())
    rotation["nextSignal"].update(
        action="HOLD", reason="动量最强仍为 518880.SS", rotateTo="518880.SS"
    )
    return singles, [rotation]


@pytest.mark.parametrize("action", [True, False])
def test_real_shape_daily_report_is_exactly_one_message(action, capsys):
    server = Server()
    result = send_all(
        make_message(*real_shape_items(action)),
        FULL_ENV,
        urlopen=server,
        sleep_fn=lambda _: (_ for _ in ()).throw(AssertionError("单条消息不应有分段等待")),
    )
    payloads = server.igotify()
    assert set(result.sent) == {"Resend", "iGotify"}
    assert len(payloads) == 1  # 一份日报 = 一条消息
    payload = payloads[0]
    assert (
        payload["extras"] == EXTRAS
        and byte_len(payload["title"]) + byte_len(payload["message"]) <= 3000
    )
    assert payload["title"] == (
        "trading 日报 2026-03-10 买入 1" if action else "trading 日报 2026-03-10 无信号变化"
    )
    body = payload["message"]
    assert "(1/" not in payload["title"] and "---" not in body.split("\n")
    order = [
        body.index(marker)
        for marker in ("今日需操作" if action else "今日无信号变化", "## 状态总览")
    ]
    if action:
        order.append(body.index("## 需关注"))
    else:
        assert "## 需关注" not in body
    assert order == sorted(order)
    assert body.count("非投资建议") == 1 and body.rstrip().endswith("不自动下单。")
    assert "\n\n## 状态总览\n\n" in body  # 小节之间空行 + 二级标题
    assert "|---" not in body and assert_codes_named(body) >= 0
    assert "iGotify 日报 1 条已提交到 Gotify" in capsys.readouterr().err


def test_eleven_item_action_day_still_fits_in_one_message_by_default():
    server = Server()
    send_all(
        make_message(*eleven_items(), ["示例警告"]),
        IGOTIFY_ENV,
        urlopen=server,
        sleep_fn=lambda _: None,
    )
    payloads = server.igotify()
    assert len(payloads) == 1 and payloads[0]["title"] == "trading 日报 2026-03-10 买入 2｜换仓 3"
    assert byte_len(payloads[0]["title"]) + byte_len(payloads[0]["message"]) <= 3000


def test_over_the_limit_falls_back_to_numbered_segments_without_breaking_structure(capsys):
    message = make_message(*eleven_items(), ["示例警告"])
    server, sleeps = Server(), []
    result = send_all(
        message, {**FULL_ENV, "IGOTIFY_MAX_BYTES": "1500"}, urlopen=server, sleep_fn=sleeps.append
    )
    payloads = server.igotify()
    assert len(payloads) == 3 and "iGotify" in result.sent
    assert [p["title"] for p in payloads] == [
        "trading 日报 2026-03-10 买入 2｜换仓 3 (1/3)",
        "trading 日报 2026-03-10 (2/3)",
        "trading 日报 2026-03-10 (3/3)",
    ]
    assert sleeps.count(8.0) == 2  # 分段间隔只在回退路径出现
    for p in payloads:
        assert p["extras"] == EXTRAS and byte_len(p["title"]) + byte_len(p["message"]) <= 1500
        assert "|---" not in p["message"] and assert_codes_named(p["message"]) >= 0
    assert (
        payloads[0]["message"].split("\n")[0] == "🔄 **今日需操作 5 项**"
        and "## 状态总览" in payloads[1]["message"]
    )
    assert "超过 1500 字节，分 3 段顺序提交到 Gotify" in capsys.readouterr().err
    email = json.loads(next(r for r in server.requests if "resend" in r.full_url).data)
    assert (
        "## 回测与绩效" in email["text"] and email["html"] == "<html>完整</html>"
    )  # 邮件始终是完整的一封


def test_message_exactly_at_the_limit_is_still_one_message():
    message = make_message(*real_shape_items(True))
    title = "trading 日报 2026-03-10 买入 1"
    size = byte_len(title) + byte_len(render_full(message.parts).rstrip("\n"))
    for limit, segmented in ((size, False), (size - 1, True)):
        server = Server()
        send_all(
            message,
            {**IGOTIFY_ENV, "IGOTIFY_MAX_BYTES": str(limit)},
            urlopen=server,
            sleep_fn=lambda _: None,
        )
        assert (len(server.igotify()) > 1) is segmented  # 恰好等于上限仍是一条，多 1 字节才回退分段


def test_cli_builds_push_for_igotify_and_full_for_email():
    sent = {}

    def fake_send_all(message, env, **options):
        sent["message"] = message
        from trading.io.channels import DispatchResult

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
    assert "## 回测与绩效" in message.markdown and "<h2" in message.html
    assert (
        "回测" not in "\n".join(pack(message.parts, BUDGET))
        and message.body
        and "回测" not in message.body
    )
