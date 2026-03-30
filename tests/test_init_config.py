"""Tests for config-writing flags on the init command."""

import tomllib

import pytest
from typer.testing import CliRunner

from agent_circus.cli import app

runner = CliRunner()


@pytest.fixture()
def workspace(tmp_path):
    """A temporary directory that looks like a project root (has a .git dir)."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _read_toml(path):
    with open(path, "rb") as f:
        return tomllib.load(f)


def test_init_ssh_creates_config_dir_and_toml(workspace) -> None:
    result = runner.invoke(app, ["init", "--workspace", str(workspace), "--ssh"])
    assert result.exit_code == 0, result.output
    config_file = workspace / ".agent-circus" / "config.toml"
    assert config_file.is_file()
    data = _read_toml(config_file)
    assert data.get("ssh") == {}


def test_init_ssh_config_path_written(workspace, tmp_path) -> None:
    cfg = tmp_path / "ssh_config"
    cfg.write_text("Host github.com\n  User git\n")
    result = runner.invoke(
        app, ["init", "--workspace", str(workspace), "--ssh-config", str(cfg)]
    )
    assert result.exit_code == 0, result.output
    data = _read_toml(workspace / ".agent-circus" / "config.toml")
    assert data["ssh"]["config_path"] == str(cfg)


def test_init_ssh_known_hosts_written(workspace, tmp_path) -> None:
    kh = tmp_path / "known_hosts"
    kh.write_text("github.com ssh-ed25519 AAAA...\n")
    result = runner.invoke(
        app, ["init", "--workspace", str(workspace), "--ssh-known-hosts", str(kh)]
    )
    assert result.exit_code == 0, result.output
    data = _read_toml(workspace / ".agent-circus" / "config.toml")
    assert data["ssh"]["known_hosts_path"] == str(kh)


def test_init_shadow_single(workspace) -> None:
    result = runner.invoke(
        app, ["init", "--workspace", str(workspace), "--shadow", ".env"]
    )
    assert result.exit_code == 0, result.output
    data = _read_toml(workspace / ".agent-circus" / "config.toml")
    assert data["shadow"] == [".env"]


def test_init_shadow_multiple(workspace) -> None:
    result = runner.invoke(
        app,
        [
            "init",
            "--workspace",
            str(workspace),
            "--shadow",
            ".env",
            "--shadow",
            ".env.local",
        ],
    )
    assert result.exit_code == 0, result.output
    data = _read_toml(workspace / ".agent-circus" / "config.toml")
    assert ".env" in data["shadow"]
    assert ".env.local" in data["shadow"]


def test_init_merges_existing_shadow(workspace) -> None:
    config_dir = workspace / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('shadow = [".env"]\n')
    result = runner.invoke(
        app, ["init", "--workspace", str(workspace), "--shadow", ".env.local"]
    )
    assert result.exit_code == 0, result.output
    data = _read_toml(workspace / ".agent-circus" / "config.toml")
    assert ".env" in data["shadow"]
    assert ".env.local" in data["shadow"]


def test_init_ssh_merges_with_existing_config(workspace) -> None:
    config_dir = workspace / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text('shadow = [".env"]\n')
    result = runner.invoke(app, ["init", "--workspace", str(workspace), "--ssh"])
    assert result.exit_code == 0, result.output
    data = _read_toml(workspace / ".agent-circus" / "config.toml")
    assert data["shadow"] == [".env"]
    assert "ssh" in data


def test_init_no_flags_no_config_written(workspace) -> None:
    # Bare init without config flags must not create config.toml
    # (it will print guidance about missing config, exit 1 — that's fine)
    runner.invoke(app, ["init", "--workspace", str(workspace)])
    assert not (workspace / ".agent-circus" / "config.toml").exists()
