"""Tests for runtime state management."""

from pathlib import Path

import pytest

from agent_circus.state import (
    acquire,
    get_refs,
    get_shadow_override_path,
    get_state_dir,
    release,
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


def test_get_shadow_override_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    path = get_shadow_override_path(tmp_path / "my-project")
    assert path.name == "compose.shadow.json"
    assert path.parent == tmp_path / "xdg-state" / "agent-circus" / "my-project"


def test_acquire_increments_ref_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    refs = acquire(tmp_path / "ws", "claude-code")
    assert refs == 1


def test_release_decrements_ref_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    workspace = tmp_path / "ws"
    acquire(workspace, "claude-code")
    acquire(workspace, "claude-code")
    refs = release(workspace, "claude-code")
    assert refs == 1


def test_release_floors_at_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    refs = release(tmp_path / "ws", "claude-code")
    assert refs == 0


def test_acquire_release_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    workspace = tmp_path / "ws"
    acquire(workspace, "claude-code")
    refs = release(workspace, "claude-code")
    assert refs == 0


def test_concurrent_acquire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    workspace = tmp_path / "ws"
    acquire(workspace, "claude-code")
    refs = acquire(workspace, "claude-code")
    assert refs == 2


def test_get_refs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    workspace = tmp_path / "ws"
    assert get_refs(workspace, "claude-code") == 0
    acquire(workspace, "claude-code")
    assert get_refs(workspace, "claude-code") == 1


def test_independent_services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    workspace = tmp_path / "ws"
    acquire(workspace, "claude-code")
    acquire(workspace, "codex")
    assert get_refs(workspace, "claude-code") == 1
    assert get_refs(workspace, "codex") == 1
    release(workspace, "claude-code")
    assert get_refs(workspace, "claude-code") == 0
    assert get_refs(workspace, "codex") == 1
