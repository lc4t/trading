"""trading.io.channels（PushGo / Resend / iGotify 多渠道分发）测试：全部 mock HTTP，不联网。"""

import io
import json
import tempfile
from datetime import date
from pathlib import Path
from unittest import mock
from urllib import error

import pytest

from trading.cli import digest as digest_cli
from trading.io import digest
from trading.io.channels import Message, igotify, send_all
from trading.io.channels.common import ChannelError, post_json, sanitize

pytestmark = pytest.mark.unit

TOKEN = "gotify-app-token-SECRET123"
RESEND_KEY = "re_SECRETKEY456"
FROM = "Signals <sender@example.com>"
TO = "alice@example.com, bob@example.org"
MSG = Message(title="📊 [2026-03-10] 1个交易信号", body="# 日报\n- 买入 示例", subject="主题")

FULL_ENV = {
    "PUSHGO_CHANNEL_ID": "pg-channel",
    "PUSHGO_PASSWORD": "pg-password-SECRET",
    "RESEND_API_KEY": RESEND_KEY,
    "RESEND_FROM": FROM,
    "RESEND_TO": TO,
    "IGOTIFY_URL": "https://igotify.example.com/",
    "IGOTIFY_TOKEN": TOKEN,
}


class FakeResponse:
    def __init__(self, status=200, body=b"{}"):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body

    def getcode(self):
        return self.status


def http_error(code, url="https://x.example.com/p?key=SECRETQ"):
    return error.HTTPError(url, code, "err", {}, io.BytesIO(b"{}"))


class Recorder:
    """按 URL 前缀编排响应：值是一个按调用顺序消费的列表（异常实例会被抛出）。"""

    def __init__(self, plan):
        self.plan = {k: list(v) for k, v in plan.items()}
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        for prefix, queue in self.plan.items():
            if req.full_url.startswith(prefix):
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(item, BaseException):
                    raise item
                return item
        raise AssertionError(f"unexpected url {req.full_url}")

    def to(self, prefix):
        return [r for r, _ in self.requests if r.full_url.startswith(prefix)]


OK_PUSHGO = FakeResponse(body=b'{"success": true, "data": {"message_id": "m1"}}')
PUSHGO = "https://gateway.pushgo.dev"
RESEND = "https://api.resend.com"
IGOTIFY = "https://igotify.example.com"


def run(env, plan, **options):
    rec = Recorder(plan)
    sleeps = []
    result = send_all(MSG, env, urlopen=rec, sleep_fn=sleeps.append, **options)
    return result, rec, sleeps


def test_unconfigured_channels_are_skipped_with_log(capsys):
    result, rec, _ = run({}, {})
    assert result.skipped == ("PushGo", "Resend", "iGotify")
    assert not result.configured and not result.ok
    assert rec.requests == []
    err = capsys.readouterr().err
    assert "渠道 Resend 未配置" in err and "RESEND_API_KEY" in err


def test_partial_config_is_skipped_not_failed():
    env = {"RESEND_API_KEY": RESEND_KEY, "RESEND_FROM": FROM}  # 缺 RESEND_TO
    result, rec, _ = run(env, {})
    assert "Resend" in result.skipped and not result.configured


def test_all_channels_success_payloads():
    result, rec, _ = run(
        FULL_ENV,
        {
            PUSHGO: [OK_PUSHGO],
            RESEND: [FakeResponse(body=b'{"id": "e1"}')],
            IGOTIFY: [FakeResponse()],
        },
    )
    assert result.sent == ("PushGo", "Resend", "iGotify") and result.ok

    (resend_req,) = rec.to(RESEND)
    assert resend_req.full_url == "https://api.resend.com/emails"
    assert resend_req.get_header("Authorization") == f"Bearer {RESEND_KEY}"
    payload = json.loads(resend_req.data)
    assert payload["to"] == ["alice@example.com", "bob@example.org"]
    assert payload["from"] == FROM and payload["subject"] == "主题" and payload["text"] == MSG.body
    assert RESEND_KEY not in resend_req.data.decode()

    (gotify_req,) = rec.to(IGOTIFY)
    assert gotify_req.full_url == "https://igotify.example.com/message"  # 斜杠已规范化
    assert gotify_req.get_header("X-gotify-key") == TOKEN
    assert "?" not in gotify_req.full_url and TOKEN not in gotify_req.full_url
    body = json.loads(gotify_req.data)
    assert body == {"title": MSG.title, "message": MSG.body, "priority": 5}


def test_igotify_priority_from_env():
    env = {**FULL_ENV, "IGOTIFY_PRIORITY": "8"}
    _, rec, _ = run(
        {k: v for k, v in env.items() if k.startswith("IGOTIFY")}, {IGOTIFY: [FakeResponse()]}
    )
    assert json.loads(rec.to(IGOTIFY)[0].data)["priority"] == 8


def test_5xx_retries_then_succeeds_with_exponential_backoff():
    env = {k: v for k, v in FULL_ENV.items() if k.startswith("RESEND")}
    result, rec, sleeps = run(env, {RESEND: [http_error(503), http_error(429), FakeResponse()]})
    assert result.sent == ("Resend",)
    assert len(rec.to(RESEND)) == 3 and sleeps == [1.0, 2.0]
    assert all(timeout == 10.0 for _, timeout in rec.requests)


def test_4xx_is_not_retried():
    env = {k: v for k, v in FULL_ENV.items() if k.startswith("IGOTIFY")}
    result, rec, sleeps = run(env, {IGOTIFY: [http_error(401)]})
    assert result.failed and result.failed[0][0] == "iGotify"
    assert len(rec.to(IGOTIFY)) == 1 and sleeps == []


def test_network_error_retries_then_gives_up():
    env = {k: v for k, v in FULL_ENV.items() if k.startswith("IGOTIFY")}
    result, rec, sleeps = run(env, {IGOTIFY: [error.URLError("boom"), TimeoutError("t")]})
    assert not result.ok and len(rec.to(IGOTIFY)) == 3 and len(sleeps) == 2


def test_one_channel_failing_does_not_block_others():
    result, _, _ = run(
        FULL_ENV,
        {PUSHGO: [error.URLError("down")], RESEND: [FakeResponse()], IGOTIFY: [http_error(500)]},
        max_retries=1,
        retry_delay=0,
    )
    assert result.sent == ("Resend",) and result.ok
    assert {name for name, _ in result.failed} == {"PushGo", "iGotify"}


def test_all_channels_failing_is_not_ok():
    result, _, _ = run(
        FULL_ENV,
        {PUSHGO: [http_error(400)], RESEND: [http_error(422)], IGOTIFY: [http_error(403)]},
        max_retries=1,
        retry_delay=0,
    )
    assert result.configured and not result.ok and len(result.failed) == 3


def test_logs_and_reasons_do_not_leak_secrets(capsys):
    env = dict(FULL_ENV)
    bad = http_error(401, url=f"https://igotify.example.com/message?token={TOKEN}")
    result, _, _ = run(
        env,
        {
            PUSHGO: [error.URLError("cannot reach pg-password-SECRET pg-channel")],
            RESEND: [error.URLError(f"bad to alice@example.com key {RESEND_KEY}")],
            IGOTIFY: [bad],
        },
        max_retries=1,
        retry_delay=0,
    )
    text = capsys.readouterr().err + repr(result)
    for secret in (
        TOKEN,
        RESEND_KEY,
        "pg-password-SECRET",
        "pg-channel",
        "alice@example.com",
        "bob@example.org",
        "sender@example.com",
        "token=",
    ):
        assert secret not in text, secret


def test_igotify_rejects_plain_http_unless_allowed():
    env = {"IGOTIFY_URL": "http://localhost:8080", "IGOTIFY_TOKEN": TOKEN}
    result, rec, _ = run(env, {})
    assert result.failed and "https" in result.failed[0][1] and rec.requests == []

    allowed = {**env, "IGOTIFY_ALLOW_HTTP": "1"}
    result, rec, _ = run(allowed, {"http://localhost:8080": [FakeResponse()]})
    assert result.sent == ("iGotify",)
    assert rec.requests[0][0].full_url == "http://localhost:8080/message"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://a.example.com", "https://a.example.com"),
        ("https://a.example.com///", "https://a.example.com"),
        ("  https://a.example.com/sub/ ", "https://a.example.com/sub"),
        ("https://a.example.com/message", "https://a.example.com"),
    ],
)
def test_igotify_normalize_url(raw, expected):
    assert igotify.normalize_url(raw, allow_http=False) == expected


def test_igotify_rejects_query_or_garbage():
    for raw in ("https://a.example.com/?token=x", "ftp://a.example.com", "not a url"):
        with pytest.raises(ChannelError):
            igotify.normalize_url(raw, allow_http=False)


def test_redirects_are_not_followed():
    from trading.io.channels.common import _NoRedirect

    assert (
        _NoRedirect().redirect_request(mock.Mock(), None, 302, "Found", {}, "https://evil") is None
    )


def test_sanitize_masks_values_urls_and_emails():
    out = sanitize("x https://h.example.com/p?k=1&t=2 y a@b.co z SECRETV", ["SECRETV"])
    assert "k=1" not in out and "a@b.co" not in out and "SECRETV" not in out
    assert "https://h.example.com/p?<redacted>" in out


def test_post_json_returns_status_and_text():
    rec = Recorder({"https://h.example.com": [FakeResponse(body=b"hello")]})
    status, text = post_json(
        channel="T", url="https://h.example.com/x", headers={}, payload={"a": 1}, urlopen=rec
    )
    assert (status, text) == (200, "hello")


def test_build_email_subject_includes_date_and_names():
    def report(symbol, name, action):
        return {
            "symbol": symbol,
            "name": name,
            "reportDate": "2026-03-10",
            "latestSignal": {
                "action": action,
                "timestamp": "2026-03-10 09:30:00",
                "prices": {"close": 1.0},
            },
            "positionInfo": None,
            "recentTrades": [],
        }

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "a.json").write_text(
            json.dumps(report("1.SZ", "买入标的", "买入")), encoding="utf-8"
        )
        (Path(tmp) / "b.json").write_text(
            json.dumps(report("2.SZ", "空仓标的", "观望")), encoding="utf-8"
        )
        records, _, _ = digest.load_reports(Path(tmp), date(2026, 3, 10))
    title, _ = digest.build_digest(records, date(2026, 3, 10))
    subject = digest.build_email_subject(title, records, date(2026, 3, 10))
    assert "2026-03-10" in subject and "买入标的" in subject and "空仓标的" not in subject


EMPTY_NEW_CHANNELS = {
    "RESEND_API_KEY": "",
    "RESEND_FROM": "",
    "RESEND_TO": "",
    "IGOTIFY_URL": "",
    "IGOTIFY_TOKEN": "  ",  # 纯空白也按未配置处理
}
PUSHGO_ENV = {k: v for k, v in FULL_ENV.items() if k.startswith("PUSHGO")}


def test_empty_string_secrets_are_skipped_and_pushgo_unaffected():
    """GitHub Secrets 未配置时 workflow 会传入空串：新渠道必须安静跳过，PushGo 行为不变。"""
    result, rec, _ = run({**PUSHGO_ENV, **EMPTY_NEW_CHANNELS}, {PUSHGO: [OK_PUSHGO]})
    assert result.sent == ("PushGo",) and result.skipped == ("Resend", "iGotify")
    assert result.failed == () and len(rec.requests) == 1


class TestEmptySecretsExitCodes:
    def _run(self, env, plan):
        return TestMainExitCodes()._run({**env, **EMPTY_NEW_CHANNELS}, plan)

    def test_pushgo_ok_exit_0(self):
        assert self._run(PUSHGO_ENV, {PUSHGO: [OK_PUSHGO]}) == 0

    def test_pushgo_fails_exit_1_same_as_before(self):
        assert self._run(PUSHGO_ENV, {PUSHGO: [http_error(500)]}) == 1

    def test_everything_unconfigured_exit_1_same_as_before(self):
        assert self._run({}, {}) == 1


class TestMainExitCodes:
    def _run(self, env, plan):
        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "trading.cli.digest",
                "--data-dir",
                tmp,
                "--date",
                "2026-03-10",
                "--max-retries",
                "1",
                "--retry-delay",
                "0",
            ]
            rec = Recorder(plan)
            with (
                mock.patch("sys.argv", argv),
                mock.patch.dict("os.environ", env, clear=True),
                mock.patch("trading.cli.digest.load_dotenv"),
                mock.patch("trading.io.channels.common.default_urlopen", rec),
                mock.patch("trading.io.channels.pushgo.request.urlopen", rec),
                mock.patch("trading.io.channels.common.time.sleep"),
            ):
                return digest_cli.main()

    def test_no_channel_configured_returns_1(self):
        assert self._run({}, {}) == 1

    def test_resend_only_success_returns_0(self):
        env = {k: v for k, v in FULL_ENV.items() if k.startswith("RESEND")}
        assert self._run(env, {RESEND: [FakeResponse()]}) == 0

    def test_all_failed_returns_1(self):
        env = {k: v for k, v in FULL_ENV.items() if k.startswith("RESEND")}
        assert self._run(env, {RESEND: [http_error(401)]}) == 1

    def test_pushgo_down_but_resend_ok_returns_0(self):
        env = {k: v for k, v in FULL_ENV.items() if k.startswith(("PUSHGO", "RESEND"))}
        assert self._run(env, {PUSHGO: [error.URLError("x")], RESEND: [FakeResponse()]}) == 0
