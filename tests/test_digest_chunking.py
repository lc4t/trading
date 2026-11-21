"""iGotify 按 UTF-8 字节分段：结构不被切断、不丢内容、字节数不超限、分段发送与失败判定。"""

import copy
import json
import re
from datetime import date
from pathlib import Path
from urllib import error

import pytest
from test_digest_markdown import buy_report, hold_report, rotation_report

from trading.io import digest
from trading.io.channels import Message, send_all
from trading.io.channels.chunking import (
    Block,
    DigestParts,
    Unit,
    byte_len,
    pack,
    parts_from_markdown,
    render_full,
)
from trading.io.channels.igotify import DEFAULT_MAX_BYTES, parse_max_bytes, plan_segments
from trading.io.channels.markup import markdown_to_html
from trading.io.digest_markdown import build_digest_parts

pytestmark = pytest.mark.unit

TODAY = date(2026, 3, 10)
BUDGET = 2900


def make_parts(reports, rotations):
    records = [
        digest.ReportRecord(
            Path("x.json"),
            r,
            digest.normalize_action(r["latestSignal"]["action"]),
            r["symbol"],
            r["name"],
        )
        for r in reports
    ]
    return build_digest_parts(records, TODAY, rotations, ["示例警告"])


def eleven_items():
    singles = []
    for i in range(8):
        r = copy.deepcopy(buy_report() if i % 4 == 0 else hold_report())
        r["symbol"], r["name"] = f"{159900 + i}.SZ", f"示例标的{i}"
        singles.append(r)
    rotations = []
    for i in range(3):
        r = copy.deepcopy(rotation_report())
        r["strategyId"], r["strategyName"] = (
            f"momentum-top{i}",
            f"动量轮动 Top-{i}（创业板/纳指/黄金）",
        )
        rotations.append(r)
    return singles, rotations


def check_structure(messages, budget):
    for body in messages:
        assert byte_len(body) <= budget
        lines = body.split("\n")
        for i, line in enumerate(lines):
            if line.startswith("|"):  # 表格行必须带着表头与分隔行
                top = i
                while top > 0 and lines[top - 1].startswith("|"):
                    top -= 1
                assert re.match(r"^\|(---\|)+$", lines[top + 1]), (top, lines[top : top + 2])
            if line == "---":  # 分隔线前后留空行
                assert i > 0 and lines[i - 1] == "" and lines[i + 1] == ""
        assert not body.startswith("---") and not body.rstrip().endswith("---")


def content_lines(markdown):
    return {ln for ln in markdown.split("\n") if ln.strip() and ln != "---"}


def test_byte_len_counts_utf8_bytes_not_chars():
    assert (
        byte_len("abc") == 3
        and byte_len("中文") == 6
        and byte_len("📊") == 4
        and byte_len("🟢 买入") == 4 + 1 + 6
    )


def test_eleven_items_split_into_messages_without_losing_content():
    singles, rotations = eleven_items()
    parts = make_parts(singles, rotations)
    messages = pack(parts, BUDGET)
    assert len(messages) > 1 and byte_len(render_full(parts)) > 3500
    check_structure(messages, BUDGET)
    joined = "\n".join(messages)
    missing = content_lines(render_full(parts)) - content_lines(joined)
    assert not missing, missing


def test_first_message_is_conclusion_plus_status_table_and_standalone():
    singles, rotations = eleven_items()
    messages = pack(make_parts(singles, rotations), BUDGET)
    first = messages[0]
    assert first.startswith("# 📊 交易信号日报")
    assert "## 今日结论" in first and "## 状态总表" in first and "## 标的详情" not in first
    assert first.count("\n| ") >= 11 + 1  # 11 行 + 表头
    assert all("## 今日结论" not in m for m in messages[1:])


def test_status_table_drops_least_important_columns_to_fit_first_message():
    singles, rotations = eleven_items()
    parts = make_parts(singles, rotations)
    full_first = byte_len(SEP_JOIN(parts))
    tight = full_first - 200  # 比完整总表所需略小
    messages = pack(parts, tight)
    first = messages[0]
    assert "## 状态总表" in first and "| 标的 | 动作 | 持仓 | 浮盈 | 止损位 |" in first
    assert "最近切换" not in first and byte_len(first) <= tight


def SEP_JOIN(parts):
    """第 1 条在完整总表下的字节数。"""
    return "\n\n".join(b.text for b in parts.summary)


def test_150_items_extreme_case_all_content_survives():
    singles = []
    for i in range(150):
        r = copy.deepcopy(hold_report(f"极端标的{i:03d}"))
        r["symbol"] = f"{200000 + i}.SZ"
        singles.append(r)
    parts = make_parts(singles, [rotation_report()])
    messages = pack(parts, BUDGET)
    assert len(messages) > 20
    check_structure(messages, BUDGET)
    missing = content_lines(render_full(parts)) - content_lines("\n".join(messages))
    assert not missing, list(missing)[:3]
    assert "极端标的000" in messages[0] or "**（续）概览**" in "\n".join(
        messages
    )  # 总表放不下时顺延并标续
    assert "极端标的149" in "\n".join(messages)


def test_oversized_single_unit_is_split_by_rows_with_continuation_and_repeated_head():
    rotation = copy.deepcopy(rotation_report())
    rotation["universeRanking"] = [
        {
            "symbol": f"{300000 + i}.SZ",
            "name": f"候选{i:03d}",
            "momentum": 0.01 * i,
            "currentPrice": 1.0,
            "targetWeight": 0.0,
            "selected": False,
            "rank": i + 1,
        }
        for i in range(300)
    ]
    parts = make_parts([hold_report()], [rotation])
    messages = pack(parts, BUDGET)
    check_structure(messages, BUDGET)
    joined = "\n".join(messages)
    assert joined.count("**（续）") >= 2 and "动量轮动" in joined
    rank_rows = re.findall(r"^\| \d+ \| 候选\d{3} ", joined, re.M)
    assert len(rank_rows) == 300  # 每行只出现一次，没丢没重
    assert joined.count("| # | 标的 | 动量 | 现价 | 权重 |") >= 3  # 表头在每个续段重复


def test_single_line_longer_than_budget_is_hard_split_without_corrupting_utf8():
    long_line = "- " + "中文📊" * 1500
    parts = DigestParts("L", "h", (), (Unit("长行", (Block((long_line,)),)),))
    messages = pack(parts, 1000)
    assert all(byte_len(m) <= 1000 for m in messages) and len(messages) > 5
    body = "".join(re.sub(r"^\*\*（续）长行\*\*\n\n", "", m) for m in messages)
    assert body.replace("\n", "") == long_line and "�" not in body


def test_generic_markdown_fallback_keeps_tables_intact():
    table = ["| a | b |", "|---|---|", *[f"| {i} | 值{i} |" for i in range(200)]]
    md = "# t\n\n" + "\n".join(table) + "\n\n- 尾"
    messages = pack(parts_from_markdown(md), 1200)
    check_structure(messages, 1200)
    assert len(re.findall(r"^\| \d+ \| 值\d+ \|$", "\n".join(messages), re.M)) == 200


def test_titles_are_numbered_and_first_carries_headline():
    singles, rotations = eleven_items()
    message = Message(
        "📊 [2026-03-10] 3个交易信号",
        "精简",
        "主题",
        markdown=render_full(make_parts(singles, rotations)),
        parts=make_parts(singles, rotations),
    )
    segments = plan_segments(message, DEFAULT_MAX_BYTES)
    total = len(segments)
    assert total > 1
    assert segments[0][0] == f"trading 日报 2026-03-10 买入 2｜换仓 3 (1/{total})"
    assert [t for t, _ in segments[1:]] == [
        f"trading 日报 2026-03-10 ({i}/{total})" for i in range(2, total + 1)
    ]
    for title, body in segments:  # 标题也计入上限
        assert byte_len(title) + byte_len(body) <= DEFAULT_MAX_BYTES


def test_short_message_stays_single_with_original_title():
    parts = make_parts([hold_report()], [])
    message = Message("原标题", "精简", "主题", markdown=render_full(parts), parts=parts)
    segments = plan_segments(message, 8000)
    assert segments == [("原标题", render_full(parts))]


def test_max_bytes_env_override_and_invalid_fallback(capsys):
    assert parse_max_bytes(None) == DEFAULT_MAX_BYTES == 3000
    assert parse_max_bytes("2500") == 2500
    assert (
        parse_max_bytes("abc") == DEFAULT_MAX_BYTES and parse_max_bytes("10") == DEFAULT_MAX_BYTES
    )
    assert "IGOTIFY_MAX_BYTES 无效" in capsys.readouterr().err


# ---- 发送：顺序、间隔、独立重试、失败判定 ----

IGOTIFY_ENV = {"IGOTIFY_URL": "https://igotify.example.com", "IGOTIFY_TOKEN": "tok-SECRET"}
PUSHGO_ENV = {"PUSHGO_CHANNEL_ID": "cid", "PUSHGO_PASSWORD": "pwd"}


class Resp:
    status = 200

    def __init__(self, body=b'{"success": true}'):
        self._b = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b

    def getcode(self):
        return 200


def http_error(code):
    return error.HTTPError("https://x.example.com/m", code, "e", {}, None)


class Server:
    """igotify 请求按序消费 plan（None 表示成功，数字表示该 HTTP 状态码），其它地址一律成功。"""

    def __init__(self, igotify_plan=()):
        self.plan = list(igotify_plan)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        if req.full_url.startswith("https://igotify"):
            step = self.plan.pop(0) if self.plan else None
            if step:
                raise http_error(step)
        return Resp()

    def igotify(self):
        return [
            json.loads(r.data) for r in self.requests if r.full_url.startswith("https://igotify")
        ]


def long_message():
    singles, rotations = eleven_items()
    parts = make_parts(singles, rotations)
    return Message(
        "📊 [2026-03-10] 3个交易信号",
        "精简正文",
        "主题",
        markdown=render_full(parts),
        html=markdown_to_html(render_full(parts), "主题"),
        parts=parts,
    )


def test_segments_sent_in_order_with_interval_and_markdown_extras():
    server, sleeps = Server(), []
    result = send_all(long_message(), IGOTIFY_ENV, urlopen=server, sleep_fn=sleeps.append)
    payloads = server.igotify()
    total = len(payloads)
    assert result.sent == ("iGotify",) and total > 1
    assert sleeps == [1.0] * (total - 1)  # 段间隔 1 秒，最后一段后不等
    assert [p["title"].rsplit("(", 1)[1] for p in payloads] == [
        f"{i}/{total})" for i in range(1, total + 1)
    ]
    for p in payloads:
        assert (
            p["extras"] == {"client::display": {"contentType": "text/markdown"}}
            and p["priority"] == 5
        )
        assert byte_len(p["title"]) + byte_len(p["message"]) <= 3000


def test_each_segment_retries_independently():
    server, sleeps = Server([None, 503, None]), []  # 第 2 段先 503 后成功
    result = send_all(long_message(), IGOTIFY_ENV, urlopen=server, sleep_fn=sleeps.append)
    assert result.sent == ("iGotify",)
    total = len(server.igotify())
    assert total >= 4  # 11 项 → 至少 4 段（含 1 次重试的重复请求）
    assert sleeps.count(1.0) >= total - 1  # 段间隔与重试退避都是 1 秒级


def test_partial_failure_names_the_segment_and_counts_as_channel_failure(capsys):
    server, sleeps = Server([None, 400]), []  # 第 2 段 4xx 不重试
    result = send_all(long_message(), IGOTIFY_ENV, urlopen=server, sleep_fn=sleeps.append)
    total = len(server.igotify())
    assert result.sent == () and result.failed[0][0] == "iGotify"
    assert (
        "部分失败：第 2 段失败" in result.failed[0][1] and f"共 {total} 段" in result.failed[0][1]
    )
    assert total > 2  # 失败后其余段仍继续发送
    err = capsys.readouterr().err
    assert "第 2/" in err and "tok-SECRET" not in err


def test_partial_igotify_failure_does_not_affect_other_channels_verdict():
    server = Server([400, 400, 400, 400, 400, 400])
    result = send_all(
        long_message(), {**IGOTIFY_ENV, **PUSHGO_ENV}, urlopen=server, sleep_fn=lambda _: None
    )
    assert result.sent == ("PushGo",) and result.ok  # 至少一个渠道完整成功即整体成功
    assert [n for n, _ in result.failed] == ["iGotify"]


def test_all_segments_failing_reports_all_failed():
    server = Server([500] * 50)
    result = send_all(long_message(), IGOTIFY_ENV, urlopen=server, sleep_fn=lambda _: None)
    assert "全部失败" in result.failed[0][1]


def test_env_max_bytes_changes_segment_size():
    server = Server()
    send_all(
        long_message(),
        {**IGOTIFY_ENV, "IGOTIFY_MAX_BYTES": "1500"},
        urlopen=server,
        sleep_fn=lambda _: None,
    )
    small = server.igotify()
    server2 = Server()
    send_all(long_message(), IGOTIFY_ENV, urlopen=server2, sleep_fn=lambda _: None)
    assert len(small) > len(server2.igotify())
    assert all(byte_len(p["title"]) + byte_len(p["message"]) <= 1500 for p in small)


def test_email_is_not_segmented():
    server = Server()
    env = {
        "RESEND_API_KEY": "re_SECRET",
        "RESEND_FROM": "A <a@example.com>",
        "RESEND_TO": "b@example.com",
    }
    message = long_message()
    send_all(message, env, urlopen=server, sleep_fn=lambda _: None)
    assert len(server.requests) == 1
    payload = json.loads(server.requests[0].data)
    assert payload["text"] == message.markdown and payload["html"] == message.html
    assert byte_len(payload["text"]) > 3500
