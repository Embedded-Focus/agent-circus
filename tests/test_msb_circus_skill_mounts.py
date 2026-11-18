"""Tests for read-only user skill directory mounts and their drift warning."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from msb_circus.cli import _warn_on_agent_skill_mount_drift
from msb_circus.config import MsbConfig, resolve_agent_skill_mounts
from msb_circus.exceptions import MsbCommandError
from msb_circus.msb import MsbInvoker


def test_resolve_agent_skill_mounts_returns_empty_without_dirs(
    tmp_path, caplog
) -> None:
    """Missing skill directories yield no mounts and no warning log."""
    with caplog.at_level(logging.WARNING):
        result = resolve_agent_skill_mounts(environ={"HOME": str(tmp_path)})
    assert result == []
    assert caplog.text == ""


def test_resolve_agent_skill_mounts_mounts_existing_dirs_read_only(
    tmp_path,
) -> None:
    """Existing skill directories are mounted read-only in declaration order."""
    home = tmp_path
    (home / ".pi" / "agent" / "skills").mkdir(parents=True)
    (home / ".config" / "opencode" / "skills").mkdir(parents=True)
    result = resolve_agent_skill_mounts(environ={"HOME": str(home)})
    assert result == [
        (str(home / ".pi/agent/skills"), "/root/.pi/agent/skills:ro"),
        (str(home / ".config/opencode/skills"), "/root/.config/opencode/skills:ro"),
    ]


def test_resolve_agent_skill_mounts_ignores_host_agents_skills(
    tmp_path,
) -> None:
    """Host ``~/.agents/skills`` is not mounted; the system store owns it."""
    home = tmp_path
    (home / ".agents" / "skills").mkdir(parents=True)
    result = resolve_agent_skill_mounts(environ={"HOME": str(home)})
    assert result == []


def test_resolve_agent_skill_mounts_skips_missing_dir(tmp_path) -> None:
    """Only the existing skill directory is mounted."""
    home = tmp_path
    (home / ".pi" / "agent" / "skills").mkdir(parents=True)
    result = resolve_agent_skill_mounts(environ={"HOME": str(home)})
    assert result == [(str(home / ".pi/agent/skills"), "/root/.pi/agent/skills:ro")]


def test_resolve_agent_skill_mounts_ignores_file_in_place_of_dir(
    tmp_path,
) -> None:
    """A regular file at the skills path is not mounted."""
    home = tmp_path
    (home / ".pi" / "agent").mkdir(parents=True)
    (home / ".pi" / "agent" / "skills").write_text("not a directory")
    result = resolve_agent_skill_mounts(environ={"HOME": str(home)})
    assert result == []


class InspectInvoker:
    """Invoker double: records argv and answers every call with inspect JSON."""

    def __init__(self, binds: list[dict] | None = None, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.binds = binds or []
        self.fail = fail

    def run(self, args: list[str], check: bool = True) -> SimpleNamespace:
        self.calls.append(args)
        if self.fail:
            raise MsbCommandError("inspect failed", returncode=1, stderr="")
        stdout = json.dumps({"config": {"mounts": self.binds}})
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)


@pytest.fixture
def skills_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a system skill store and point ``XDG_DATA_HOME`` at its root.

    :param tmp_path: Per-test temporary directory.
    :param monkeypatch: Pytest environment patch helper.
    :returns: The store root directory.
    """
    store = tmp_path / "xdg-data" / "msb-circus" / "system-skills"
    (store / ".agents" / "skills").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return store


def test_warn_on_agent_skill_mount_drift_warns_when_store_mount_missing(
    skills_store, caplog
) -> None:
    """A system store without a sandbox mount triggers a warning."""
    with caplog.at_level(logging.WARNING):
        _warn_on_agent_skill_mount_drift(
            cast(MsbInvoker, InspectInvoker()), MsbConfig(), "agents-demo"
        )
    assert "User or system skill directory mounts (/root/.agents/skills)" in caplog.text


@pytest.mark.parametrize("guest", ["/root/.agents/skills", "/root/.agents/skills:ro"])
def test_warn_on_agent_skill_mount_drift_passes_when_store_mounted(
    skills_store, caplog, guest
) -> None:
    """A mounted system store passes, with or without the ``:ro`` suffix."""
    binds = [
        {"type": "Bind", "host": str(skills_store / ".agents/skills"), "guest": guest}
    ]
    with caplog.at_level(logging.WARNING):
        _warn_on_agent_skill_mount_drift(
            cast(MsbInvoker, InspectInvoker(binds)), MsbConfig(), "agents-demo"
        )
    assert "skill directory mounts" not in caplog.text


def test_warn_on_agent_skill_mount_drift_skips_inspect_without_mounts(
    tmp_path, monkeypatch, caplog
) -> None:
    """No host skill dirs and no system store: no inspect call, no warning."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("HOME", str(tmp_path))
    invoker = InspectInvoker()
    with caplog.at_level(logging.WARNING):
        _warn_on_agent_skill_mount_drift(
            cast(MsbInvoker, invoker), MsbConfig(), "agents-demo"
        )
    assert invoker.calls == []
    assert caplog.text == ""


def test_warn_on_agent_skill_mount_drift_ignores_inspect_failure(
    skills_store, caplog
) -> None:
    """A failing inspect skips the check without a warning."""
    with caplog.at_level(logging.WARNING):
        _warn_on_agent_skill_mount_drift(
            cast(MsbInvoker, InspectInvoker(fail=True)), MsbConfig(), "agents-demo"
        )
    assert caplog.text == ""


def test_resolve_agent_skill_mounts_mounts_opencode_skill_dirs(tmp_path) -> None:
    """Both opencode config skill directories are mounted read-only."""
    home = tmp_path
    (home / ".config" / "opencode" / "skills").mkdir(parents=True)
    (home / ".config" / "opencode" / "skill").mkdir(parents=True)
    result = resolve_agent_skill_mounts(environ={"HOME": str(home)})
    assert result == [
        (str(home / ".config/opencode/skills"), "/root/.config/opencode/skills:ro"),
        (str(home / ".config/opencode/skill"), "/root/.config/opencode/skill:ro"),
    ]


def test_system_skill_mounts_created_on_demand_and_expected_in_drift(
    tmp_path, monkeypatch
) -> None:
    """``_system_skill_mounts`` creates the store; missing store yields none."""
    from msb_circus.cli import _system_skill_mounts

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("HOME", str(tmp_path))
    config = MsbConfig()
    assert _system_skill_mounts(config, create=False) == []
    mounts = _system_skill_mounts(config, create=True)
    store = tmp_path / "xdg-data" / "msb-circus" / "system-skills"
    assert (store / ".agents" / "skills").is_dir()
    assert mounts == [(str(store / ".agents/skills"), "/root/.agents/skills:ro")]
    assert _system_skill_mounts(config, create=False) == mounts
