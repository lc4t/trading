"""命令行入口改名：``trading-*`` 是正式名，``tt-*`` 是已弃用别名，两者必须指向同一个入口函数."""
import importlib
import pathlib
import tomllib

import pytest

PYPROJECT = pathlib.Path(__file__).parents[2] / "pyproject.toml"
SCRIPTS = tomllib.loads(PYPROJECT.read_text())["project"]["scripts"]

# 新名 -> 旧名（弃用别名）；trading-mcp 没有旧名
PAIRS = [
    ("trading-backtest", "tt-backtest"),
    ("trading-fetcher", "tt-fetcher"),
    ("trading-digest", "tt-digest"),
]


def resolve(target: str):
    module, _, attr = target.partition(":")
    return getattr(importlib.import_module(module), attr)


@pytest.mark.parametrize("new,old", PAIRS)
def test_new_and_deprecated_names_are_both_registered_with_the_same_target(new, old):
    assert new in SCRIPTS and old in SCRIPTS
    assert SCRIPTS[new] == SCRIPTS[old]
    assert resolve(SCRIPTS[new]) is resolve(SCRIPTS[old])


def test_trading_mcp_is_kept_and_has_no_legacy_alias():
    assert SCRIPTS["trading-mcp"] == "trading.cli.serve:main"
    assert not [k for k in SCRIPTS if k.startswith("tt-") and "mcp" in k]


def test_only_the_three_known_tt_aliases_remain():
    assert sorted(k for k in SCRIPTS if k.startswith("tt-")) == sorted(old for _, old in PAIRS)


@pytest.mark.parametrize("name", [n for pair in PAIRS for n in pair])
def test_every_entry_point_answers_help(name, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [name, "--help"])
    with pytest.raises(SystemExit) as exc:
        resolve(SCRIPTS[name])()
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_readme_marks_tt_names_as_deprecated_aliases():
    readme = (PYPROJECT.parent / "README.md").read_text()
    assert "已弃用的别名" in readme and "下个大版本移除" in readme
    for new, old in PAIRS:
        assert f"`{new}`" in readme and f"`{old}`" in readme


def test_no_stale_tt_names_outside_the_alias_declarations():
    root = PYPROJECT.parent
    skip = {"uv.lock", "test_cli_entrypoints.py", "test_config_and_cli.py", "pyproject.toml", "README.md"}
    hits = []
    for base in ("trading", "scripts", "tests", "deploy"):
        for path in (root / base).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".md", ".yml", ".sh"} and path.name not in skip:
                text = path.read_text(errors="ignore")
                hits += [str(path) for old in ("tt-backtest", "tt-fetcher", "tt-digest") if old in text]
    assert not hits, hits
