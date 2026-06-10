"""Tests for runtime state management."""

from pathlib import Path

import pytest

from agent_circus.state import (
    get_agent_config_store_dir,
    get_shadow_override_path,
    get_state_dir,
)


def test_get_state_dir_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    # Use a fake home so we don't touch the real filesystem.
    monkeypatch.setenv("HOME", str(tmp_path))
    state_dir = get_state_dir(tmp_path / "my-project")
    assert state_dir == tmp_path / ".local" / "state" / "agent-circus" / "my-project"
    assert state_dir.is_dir()


def test_get_state_dir_xdg_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    state_dir = get_state_dir(tmp_path / "my-project")
    assert state_dir == tmp_path / "xdg-state" / "agent-circus" / "my-project"
    assert state_dir.is_dir()


def test_get_agent_config_store_dir_is_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    store_dir = get_agent_config_store_dir(tmp_path / "project", "codex")

    assert store_dir == (
        tmp_path / "state" / "agent-circus" / "project" / "agent-config" / "codex"
    )
    assert store_dir.stat().st_mode & 0o777 == 0o700


def test_get_shadow_override_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    path = get_shadow_override_path(tmp_path / "my-project")
    assert path.name == "compose.shadow.json"
    assert path.parent == tmp_path / "xdg-state" / "agent-circus" / "my-project"
