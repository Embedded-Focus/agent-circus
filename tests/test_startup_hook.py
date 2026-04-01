"""Tests for startup hook override helpers."""

import json
from pathlib import Path

import pytest

from agent_circus.config import AVAILABLE_SERVICES, build_startup_hook_override
from agent_circus.state import get_startup_hook_override_path, get_startup_hook_path


def test_build_startup_hook_override_contains_all_services(tmp_path: Path) -> None:
    script = tmp_path / "startup.sh"
    script.touch()
    result = json.loads(build_startup_hook_override(script))
    assert set(result["services"].keys()) == set(AVAILABLE_SERVICES)


def test_build_startup_hook_override_mounts_readonly(tmp_path: Path) -> None:
    script = tmp_path / "startup.sh"
    script.touch()
    override = build_startup_hook_override(script)
    assert ":ro" in override


def test_build_startup_hook_override_mounts_at_expected_container_path(
    tmp_path: Path,
) -> None:
    script = tmp_path / "startup.sh"
    script.touch()
    override = build_startup_hook_override(script)
    assert "/workspace/.agent-circus/hooks/startup.sh" in override


def test_get_startup_hook_path_creates_hooks_subdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = get_startup_hook_path(tmp_path)
    assert path.parent.is_dir()
    assert path.name == "startup.sh"


def test_get_startup_hook_override_path_in_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = get_startup_hook_override_path(tmp_path)
    assert path.name == "compose.startup-hook.json"
    assert "agent-circus" in str(path)
