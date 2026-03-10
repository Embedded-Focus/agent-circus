"""Tests for configuration loading."""

from pathlib import Path

import pytest

from agent_circus.config import (
    find_project_root,
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
    assert config == {"shadow": [], "mcp_servers": [], "env": {}}


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


# ---------------------------------------------------------------------------
# find_project_root
# ---------------------------------------------------------------------------


def test_find_project_root_git_marker(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    assert find_project_root(tmp_path) == tmp_path


def test_find_project_root_pyproject_marker(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").touch()
    assert find_project_root(tmp_path) == tmp_path


def test_find_project_root_projectile_marker(tmp_path: Path) -> None:
    (tmp_path / ".projectile").touch()
    assert find_project_root(tmp_path) == tmp_path


def test_find_project_root_walks_up(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    subdir = tmp_path / "src" / "pkg"
    subdir.mkdir(parents=True)
    assert find_project_root(subdir) == tmp_path


def test_find_project_root_stops_at_nearest(tmp_path: Path) -> None:
    # Marker at two levels; inner (nearest to start) should win.
    (tmp_path / ".git").mkdir()
    inner = tmp_path / "sub"
    inner.mkdir()
    (inner / "pyproject.toml").touch()
    assert find_project_root(inner) == inner


def test_find_project_root_no_marker_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Patch Path.exists to always return False so no marker is ever detected,
    # forcing the walk to reach the filesystem root and fall back to start.
    monkeypatch.setattr(Path, "exists", lambda self: False)
    result = find_project_root(tmp_path)
    assert result == tmp_path
