"""Tests for configuration loading."""

from pathlib import Path

import pytest

from agent_circus.config import (
    get_project_config_path,
    get_user_config_path,
    load_config,
)
from agent_circus.exceptions import ConfigurationError


def test_get_user_config_path_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    path = get_user_config_path()
    assert path == Path.home() / ".config" / "agent-circus" / "config.toml"


def test_get_user_config_path_xdg_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg-test")
    path = get_user_config_path()
    assert path == Path("/tmp/xdg-test/agent-circus/config.toml")


def test_get_project_config_path(tmp_path: Path) -> None:
    path = get_project_config_path(tmp_path)
    assert path == tmp_path / ".agent-circus" / "config.toml"


def test_load_config_no_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point XDG to a directory with no config file.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = load_config(tmp_path)
    assert config == {"shadow": [], "mcp_servers": []}


def test_load_config_user_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    xdg_dir = tmp_path / "xdg" / "agent-circus"
    xdg_dir.mkdir(parents=True)
    (xdg_dir / "config.toml").write_text('shadow = [".env"]\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    config = load_config(tmp_path)
    assert config["shadow"] == [".env"]


def test_load_config_project_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    project_dir = tmp_path / ".agent-circus"
    project_dir.mkdir()
    (project_dir / "config.toml").write_text('shadow = [".env.local"]\n')

    config = load_config(tmp_path)
    assert config["shadow"] == [".env.local"]


def test_load_config_project_overrides_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg_dir = tmp_path / "xdg" / "agent-circus"
    xdg_dir.mkdir(parents=True)
    (xdg_dir / "config.toml").write_text('shadow = [".env"]\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    project_dir = tmp_path / ".agent-circus"
    project_dir.mkdir()
    (project_dir / "config.toml").write_text('shadow = [".env.local"]\n')

    config = load_config(tmp_path)
    assert config["shadow"] == [".env.local"]


def test_load_config_invalid_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    project_dir = tmp_path / ".agent-circus"
    project_dir.mkdir()
    (project_dir / "config.toml").write_text("this is not valid toml [[[")

    with pytest.raises(ConfigurationError, match="Invalid TOML"):
        load_config(tmp_path)
