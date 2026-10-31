import json

import pytest

from trading.cli import serve
from trading.market.auth import RateLimiter, TokenRegistry, hash_token
from trading.market.config import load_config
from trading.market.errors import ConfigError

TOKEN = "t" * 32
DB = {"DB_HOST": "h", "DB_USER": "u", "DB_PASSWORD": "pw-secret", "DB_NAME": "n"}


def tokens(*entries):
    return json.dumps(list(entries))


def good_entry(name="a", scopes=("read",)):
    return {"name": name, "scopes": list(scopes), "sha256": hash_token(TOKEN).hex()}


def test_missing_mysql_config_fails_naming_variables_only():
    with pytest.raises(ConfigError) as exc:
        load_config({"MCP_TOKENS": tokens(good_entry())})
    message = str(exc.value)
    assert "DB_HOST" in message and "DB_PASSWORD" in message and "DB_NAME" in message


def test_missing_token_config_refuses_to_run_unauthenticated():
    with pytest.raises(ConfigError) as exc:
        load_config(dict(DB))
    assert "MCP_TOKENS" in str(exc.value)


def test_empty_token_list_is_rejected():
    with pytest.raises(ConfigError):
        load_config({**DB, "MCP_TOKENS": "[]"})


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "a", "scopes": ["read"]},  # 没有 sha256/token
        {"name": "a", "scopes": ["read"], "token": "short"},  # 明文太短
        {"name": "a", "scopes": ["root"], "sha256": "0" * 64},  # 未知 scope
        {"name": "a", "scopes": [], "sha256": "0" * 64},
        {"name": "bad name!", "scopes": ["read"], "sha256": "0" * 64},
        {"name": "a", "scopes": ["read"], "sha256": "xyz"},
    ],
)
def test_invalid_token_entries_are_rejected(entry):
    with pytest.raises(ConfigError):
        load_config({**DB, "MCP_TOKENS": tokens(entry)})


def test_duplicate_names_rejected():
    with pytest.raises(ConfigError):
        load_config({**DB, "MCP_TOKENS": tokens(good_entry("x"), good_entry("x"))})


def test_tokens_file_and_env_are_merged(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text(tokens({"name": "from-file", "scopes": ["refresh"], "sha256": hash_token("f" * 32).hex()}))
    config = load_config({**DB, "MCP_TOKENS": tokens(good_entry()), "MCP_TOKENS_FILE": str(path)})
    assert len(config.tokens) == 2
    assert config.tokens.authenticate("f" * 32).allows("read")  # refresh 隐含 read
    assert config.tokens.authenticate("f" * 32).name == "from-file"


def test_unreadable_tokens_file_fails(tmp_path):
    with pytest.raises(ConfigError):
        load_config({**DB, "MCP_TOKENS_FILE": str(tmp_path / "missing.json")})


def test_registry_hashes_plaintext_and_matches():
    registry = TokenRegistry.from_json(tokens({"name": "p", "scopes": ["read"], "token": TOKEN}))
    assert registry.authenticate(TOKEN).name == "p"
    assert registry.authenticate(TOKEN + "x") is None
    assert registry.authenticate("") is None
    assert TOKEN not in repr(registry.__dict__)  # 内存里只有哈希


def test_scope_implication():
    registry = TokenRegistry.from_json(tokens(good_entry(scopes=("refresh",))))
    principal = registry.authenticate(TOKEN)
    assert principal.allows("read") and principal.allows("refresh") and not principal.allows("backtest")


def test_config_repr_hides_secrets():
    config = load_config({**DB, "MCP_TOKENS": tokens(good_entry())})
    assert "pw-secret" not in repr(config)
    assert config.database.password == "pw-secret"


def test_bad_port_rejected():
    with pytest.raises(ConfigError):
        load_config({**DB, "MCP_TOKENS": tokens(good_entry()), "MCP_PORT": "99999"})


def test_rate_limiter_sliding_window():
    now = [0.0]
    limiter = RateLimiter(2, 60, clock=lambda: now[0])
    assert limiter.acquire("k") is None and limiter.acquire("k") is None
    assert limiter.acquire("k") == pytest.approx(60)
    assert limiter.acquire("other") is None
    now[0] = 61
    assert limiter.acquire("k") is None


# ---------------------------------------------------------------- tt-serve 入口
def clear_env(monkeypatch):
    for key in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME", "MCP_TOKENS", "MCP_TOKENS_FILE"):
        monkeypatch.delenv(key, raising=False)


def test_serve_fails_fast_without_config(monkeypatch, capsys):
    clear_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        serve.main(["serve"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "配置错误" in err and "DB_HOST" in err and "MCP_TOKENS" in err


def test_serve_default_command_also_fails_fast(monkeypatch, capsys):
    clear_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        serve.main([])
    assert exc.value.code == 2


def test_serve_fails_when_tokens_missing_even_if_db_configured(monkeypatch, capsys):
    clear_env(monkeypatch)
    for k, v in DB.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit) as exc:
        serve.main(["serve"])
    assert exc.value.code == 2
    assert "pw-secret" not in capsys.readouterr().err


def test_gen_token_prints_hash_entry(capsys):
    with pytest.raises(SystemExit) as exc:
        serve.main(["gen-token", "--name", "agent-1", "--scopes", "read,refresh"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    token = next(line.strip() for line in out.splitlines() if line.strip().startswith("ttm_"))
    entry = json.loads(next(line.strip() for line in out.splitlines() if line.strip().startswith("{")))
    assert entry["sha256"] == hash_token(token).hex() and entry["scopes"] == ["read", "refresh"]
    assert token not in json.dumps(entry)


def test_gen_token_rejects_unknown_scope(capsys):
    with pytest.raises(SystemExit) as exc:
        serve.main(["gen-token", "--name", "x", "--scopes", "root"])
    assert exc.value.code == 2


def test_healthcheck_is_noop_for_non_server_containers(monkeypatch):
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    with pytest.raises(SystemExit) as exc:
        serve.main(["healthcheck"])
    assert exc.value.code == 0
