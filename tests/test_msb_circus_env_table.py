"""Tests for the top-level ``[env]`` table in msb-circus."""

import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

import msb_circus.cli as cli_module
from msb_circus.cli import app
from msb_circus.config import parse_config, warn_unsupported_keys
from msb_circus.exceptions import ConfigurationError

runner = CliRunner()


class FakeInvoker:
    """Records invocations; scripted list/inspect output."""

    behavior: ClassVar[dict] = {}

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        self.behavior.setdefault("calls", []).append(args)
        if args[:2] == ["image", "inspect"]:
            return SimpleNamespace(
                returncode=self.behavior.get("inspect_rc", 0),
                stdout="",
                stderr="",
            )
        if args[0] == "list":
            return SimpleNamespace(
                returncode=0,
                stdout=self.behavior.get("list_output", "[]"),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")


@pytest.fixture
def fake_msb(monkeypatch):
    """Patch the CLI invoker (no host subprocess, no ACP pump)."""
    FakeInvoker.behavior = {}
    FakeInvoker.behavior["inspect_rc"] = 0
    FakeInvoker.behavior["list_output"] = "[]"
    recorded: dict = {}

    def fake_interactive_run(argv, **kwargs):
        recorded["interactive_argv"] = argv
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: Path("/fake"))
    monkeypatch.setattr(cli_module, "check_version", lambda binary: "0.7.12")
    monkeypatch.setattr(cli_module, "probe_version", lambda binary: "0.7.12")
    monkeypatch.setattr(cli_module, "MsbInvoker", FakeInvoker)
    monkeypatch.setattr(cli_module.subprocess, "run", fake_interactive_run)
    monkeypatch.setattr(cli_module, "DOCKERD_PROBE_ATTEMPTS", 1)
    return recorded


def test_parse_config_reads_top_level_env():
    config = parse_config({"env": {"A": "1"}})
    assert config.env == {"A": "1"}


def test_parse_config_msb_env_wins_over_top_level_env():
    config = parse_config(
        {"env": {"A": "top", "B": "top"}, "msb": {"env": {"A": "msb"}}}
    )
    assert config.env == {"A": "msb", "B": "top"}


def test_parse_config_env_skips_dollar_values_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="msb_circus.config"):
        config = parse_config({"env": {"PATH": "/x:$PATH", "B": "2"}})
    assert config.env == {"B": "2"}
    assert "PATH" in caplog.text


@pytest.mark.parametrize("table", ["env", "msb.env"])
def test_parse_config_env_rejects_invalid_name(table):
    raw: dict = {"1BAD": "x"}
    config = {"env": {}, "msb": {}}
    if table == "env":
        config["env"] = raw
    else:
        config["msb"] = {"env": raw}
    with pytest.raises(
        ConfigurationError,
        match=re.escape(table) + " key '1BAD' is not a valid",
    ):
        parse_config(config)


def test_parse_config_env_rejects_msb_prefix():
    with pytest.raises(ConfigurationError, match=r"MSB_FOO.*msb reserves"):
        parse_config({"env": {"MSB_FOO": "x"}})


def test_parse_config_env_rejects_non_string_value():
    with pytest.raises(ConfigurationError, match=re.escape("env.A must be a string")):
        parse_config({"env": {"A": 1}})


def test_env_is_not_reported_as_unsupported(caplog):
    with caplog.at_level(logging.DEBUG, logger="msb_circus.config"):
        warn_unsupported_keys({"env": {}})
    assert "env" not in caplog.text


def test_merged_env_puts_env_table_below_passthrough(monkeypatch):
    config = parse_config({"env": {"A": "env", "B": "env"}, "env_passthrough": ["B"]})
    monkeypatch.setenv("B", "host")
    merged = cli_module._merged_env(config, env_passthrough=(), env={})
    assert merged["A"] == "env"
    assert merged["B"] == "host"


def test_exec_passes_env_table_values(tmp_path, fake_msb):
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(b'[env]\nGCX_KEYCHAIN = "off"\n')
    result = runner.invoke(app, ["exec", "--workspace", str(tmp_path), "--", "pi"])
    assert result.exit_code == 0
    argv = fake_msb["interactive_argv"]
    envs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert "GCX_KEYCHAIN=off" in envs


def test_exec_cli_env_overrides_env_table(tmp_path, fake_msb):
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(b'[env]\nGCX_KEYCHAIN = "off"\n')
    result = runner.invoke(
        app,
        [
            "exec",
            "--workspace",
            str(tmp_path),
            "-e",
            "GCX_KEYCHAIN=on",
            "--",
            "pi",
        ],
    )
    assert result.exit_code == 0
    argv = fake_msb["interactive_argv"]
    envs = [argv[i + 1] for i, a in enumerate(argv) if a == "--env"]
    assert "GCX_KEYCHAIN=on" in envs
    assert "GCX_KEYCHAIN=off" not in envs
