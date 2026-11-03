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


def test_no_token_config_loads_empty_registry_for_bootstrap(tmp_path):
    config = load_config({**DB, "MCP_CONFIG_DIR": str(tmp_path)})
    assert len(config.tokens) == 0  # 不会无鉴权运行：服务启动时由 bootstrap 生成 token


def test_empty_token_list_counts_as_unconfigured(tmp_path):
    assert len(load_config({**DB, "MCP_TOKENS": "[]", "MCP_CONFIG_DIR": str(tmp_path)}).tokens) == 0


def test_single_mcp_token_defaults_to_read_scope(tmp_path):
    config = load_config({**DB, "MCP_TOKEN": TOKEN, "MCP_CONFIG_DIR": str(tmp_path)})
    principal = config.tokens.authenticate(TOKEN)
    assert principal.name == "env" and principal.scopes == frozenset({"read"})
    config = load_config({**DB, "MCP_TOKEN": TOKEN, "MCP_TOKEN_SCOPES": "refresh", "MCP_CONFIG_DIR": str(tmp_path)})
    assert config.tokens.authenticate(TOKEN).allows("refresh")


def test_short_mcp_token_rejected():
    with pytest.raises(ConfigError):
        load_config({**DB, "MCP_TOKEN": "short"})


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


# ---------------------------------------------------------------- trading-mcp 入口
def clear_env(monkeypatch):
    for key in ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME", "MCP_TOKENS", "MCP_TOKENS_FILE", "MCP_TOKEN"):
        monkeypatch.delenv(key, raising=False)


def test_serve_fails_fast_without_config(monkeypatch, capsys):
    clear_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        serve.main(["serve"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "配置错误" in err and "DB_HOST" in err


def test_serve_default_command_also_fails_fast(monkeypatch, capsys):
    clear_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        serve.main([])
    assert exc.value.code == 2


def test_serve_config_errors_never_echo_secrets(monkeypatch, capsys):
    clear_env(monkeypatch)
    for k, v in DB.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("MCP_TOKENS", "not json")
    with pytest.raises(SystemExit) as exc:
        serve.main(["serve"])
    assert exc.value.code == 2
    assert "pw-secret" not in capsys.readouterr().err


def test_gen_token_prints_hash_entry(capsys):
    with pytest.raises(SystemExit) as exc:
        serve.main(["gen-token", "--name", "agent-1", "--scopes", "read,refresh"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    token = next(line.strip() for line in out.splitlines() if line.strip().startswith("trading_mcp_"))
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


# ------------------------------------------------- 配置目录 token 文件 / 自动引导 / token 子命令
import os  # noqa: E402
import stat  # noqa: E402

from trading.market.auth import TokenStore, bootstrap_token  # noqa: E402


def test_bootstrap_generates_persists_hash_only_and_shows_once(tmp_path):
    config = load_config({**DB, "MCP_CONFIG_DIR": str(tmp_path)})
    store = TokenStore(tmp_path)
    boot = bootstrap_token(config.tokens, store)
    assert boot.persisted and len(boot.token) >= 40
    path = tmp_path / "tokens.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    content = path.read_text()
    assert boot.token not in content and hash_token(boot.token).hex() in content  # 文件里只有哈希
    assert config.tokens.authenticate(boot.token).scopes == frozenset({"read"})
    # 重启：同一份配置目录，不再生成/展示
    again = load_config({**DB, "MCP_CONFIG_DIR": str(tmp_path)})
    assert bootstrap_token(again.tokens, TokenStore(tmp_path)) is None
    assert again.tokens.authenticate(boot.token) is not None


def test_bootstrap_with_env_token_does_nothing(tmp_path):
    config = load_config({**DB, "MCP_TOKEN": TOKEN, "MCP_CONFIG_DIR": str(tmp_path)})
    assert bootstrap_token(config.tokens, TokenStore(tmp_path)) is None
    assert not (tmp_path / "tokens.json").exists()


def test_bootstrap_with_unwritable_dir_falls_back_to_memory_and_flags_it(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")  # 目录路径被一个普通文件占着 → 不可写
    config = load_config({**DB, "MCP_CONFIG_DIR": str(blocker / "cfg")})
    boot = bootstrap_token(config.tokens, TokenStore(blocker / "cfg"))
    assert boot.persisted is False
    assert config.tokens.authenticate(boot.token) is not None


def test_registry_hot_reloads_token_file_changes(tmp_path):
    config = load_config({**DB, "MCP_CONFIG_DIR": str(tmp_path)})
    store = TokenStore(tmp_path)
    assert config.tokens.authenticate("x" * 40) is None
    new_token = store.add("ops", ["refresh"])
    assert config.tokens.authenticate(new_token).allows("refresh")
    rotated = store.rotate("ops")
    assert config.tokens.authenticate(new_token) is None  # 旧 token 立即失效
    assert config.tokens.authenticate(rotated).name == "ops"


def test_token_store_rejects_duplicates_and_unknown_names(tmp_path):
    store = TokenStore(tmp_path)
    store.add("a", ["read"])
    with pytest.raises(ConfigError):
        store.add("a", ["read"])
    with pytest.raises(ConfigError):
        store.rotate("missing")
    with pytest.raises(ConfigError):
        store.add("b", ["root"])


def test_token_cli_new_rotate_list(monkeypatch, tmp_path, capsys):
    clear_env(monkeypatch)
    monkeypatch.setenv("MCP_CONFIG_DIR", str(tmp_path))

    def run(*argv):
        with pytest.raises(SystemExit) as exc:
            serve.main(list(argv))
        return exc.value.code, capsys.readouterr()

    code, out = run("token", "new", "--name", "ops", "--scopes", "read,refresh")
    assert code == 0 and "trading_mcp_" in out.out
    first = next(x.strip() for x in out.out.splitlines() if x.strip().startswith("trading_mcp_"))
    code, out = run("token", "list")
    assert code == 0 and "ops" in out.out and "refresh" in out.out and first not in out.out
    code, out = run("token", "rotate", "--name", "ops")
    second = next(x.strip() for x in out.out.splitlines() if x.strip().startswith("trading_mcp_"))
    assert code == 0 and second != first
    assert run("token", "new", "--name", "ops")[0] == 2  # 重名
    assert run("token", "new", "--name", "x", "--scopes", "root")[0] == 2


def test_bootstrap_banner_marks_non_persistent_tokens(capsys):
    from pathlib import Path

    from trading.market.auth import Bootstrap

    serve._announce_bootstrap(Bootstrap("trading_mcp_example", False, Path("/data/config/tokens.json")))
    err = capsys.readouterr().err
    assert "重启后会变" in err and "trading_mcp_example" in err
    serve._announce_bootstrap(Bootstrap("trading_mcp_example2", True, Path("/data/config/tokens.json")))
    assert "重启不会再显示" in capsys.readouterr().err


# ---------------------------------------------- token 前缀 / 来源关系 / gen-token 条目
def test_generated_tokens_use_new_prefix_and_old_prefix_still_authenticates(tmp_path):
    from trading.market.auth import TOKEN_PREFIX, generate_token

    assert TOKEN_PREFIX == "trading_mcp_" and generate_token().startswith("trading_mcp_")
    old = "ttm_" + "o" * 40  # 旧前缀的 token：校验只比对哈希，不受前缀影响
    entry = {"name": "legacy", "scopes": ["read"], "sha256": hash_token(old).hex()}
    config = load_config({**DB, "MCP_TOKENS": tokens(entry), "MCP_CONFIG_DIR": str(tmp_path)})
    assert config.tokens.authenticate(old).name == "legacy"
    assert config.tokens.authenticate("x" + old) is None


def test_env_and_config_dir_tokens_coexist_and_env_token_name_does_not_clash(tmp_path):
    store = TokenStore(tmp_path)
    file_token = store.add("default", ["refresh"])  # 配置目录里自动生成的那个也叫 default
    config = load_config({**DB, "MCP_TOKEN": TOKEN, "MCP_TOKENS": tokens(good_entry("a")), "MCP_CONFIG_DIR": str(tmp_path)})
    assert len(config.tokens) == 3  # MCP_TOKEN + MCP_TOKENS + 文件，取并集
    assert config.tokens.authenticate(file_token).allows("refresh")
    assert config.tokens.authenticate(TOKEN).name in {"env", "a"}  # 同一串明文出现两次时先出现的（环境变量）生效
    assert [src for _, _, src in config.tokens.describe()].count("file") == 1
    assert bootstrap_token(config.tokens, store) is None  # 已有 token，不再自动生成


def test_gen_token_entry_can_be_pasted_into_mcp_tokens(capsys, tmp_path):
    with pytest.raises(SystemExit):
        serve.main(["gen-token", "--name", "agent-x", "--scopes", "read,refresh"])
    out = capsys.readouterr().out
    token = next(x.strip() for x in out.splitlines() if x.strip().startswith("trading_mcp_"))
    entry = next(x.strip() for x in out.splitlines() if x.strip().startswith("{"))
    config = load_config({**DB, "MCP_TOKENS": f"[{entry}]", "MCP_CONFIG_DIR": str(tmp_path)})
    principal = config.tokens.authenticate(token)
    assert principal.name == "agent-x" and principal.allows("refresh")


def test_mcp_token_minimum_length_is_24():
    with pytest.raises(ConfigError):
        load_config({**DB, "MCP_TOKEN": "x" * 23})
    assert load_config({**DB, "MCP_TOKEN": "x" * 24}).tokens.authenticate("x" * 24) is not None


# ------------------------------------------------------------------ 命令名
def test_rebuild_is_a_trading_mcp_subcommand_and_old_names_are_gone(capsys):
    import pathlib
    import tomllib

    with pytest.raises(SystemExit) as exc:
        serve.main(["rebuild", "--help"])
    assert exc.value.code == 0 and "trading-mcp rebuild" in capsys.readouterr().out
    scripts = tomllib.loads((pathlib.Path(__file__).parents[2] / "pyproject.toml").read_text())["project"]["scripts"]
    assert "trading-mcp" in scripts and "tt-rebuild" not in scripts and "tt-serve" not in scripts
