"""Tests for read-only user skill directory mounts and their drift warning."""

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from msb_circus.cli import _warn_on_agent_skill_mount_drift
from msb_circus.config import resolve_agent_skill_mounts
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
    (home / ".agents" / "skills").mkdir(parents=True)
    (home / ".pi" / "agent" / "skills").mkdir(parents=True)
    result = resolve_agent_skill_mounts(environ={"HOME": str(home)})
    assert result == [
        (str(home / ".agents/skills"), "/root/.agents/skills:ro"),
        (str(home / ".pi/agent/skills"), "/root/.pi/agent/skills:ro"),
    ]


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
    (home / ".agents").mkdir()
    (home / ".agents" / "skills").write_text("not a directory")
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
def skills_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``$HOME`` at a fresh directory holding ``~/.agents/skills``.

    :param tmp_path: Per-test temporary directory.
    :param monkeypatch: Pytest environment patch helper.
    :returns: The fake home directory.
    """
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_warn_on_agent_skill_mount_drift_warns_when_mount_missing(
    skills_home, caplog
) -> None:
    """An existing host skill dir without a sandbox mount triggers a warning."""
    with caplog.at_level(logging.WARNING):
        _warn_on_agent_skill_mount_drift(
            cast(MsbInvoker, InspectInvoker()), "agents-demo"
        )
    assert "User skill directory mounts (/root/.agents/skills)" in caplog.text


@pytest.mark.parametrize("guest", ["/root/.agents/skills", "/root/.agents/skills:ro"])
def test_warn_on_agent_skill_mount_drift_passes_when_mounted(
    skills_home, caplog, guest
) -> None:
    """A mounted skill dir passes, with or without the ``:ro`` suffix."""
    binds = [
        {"type": "Bind", "host": str(skills_home / ".agents/skills"), "guest": guest}
    ]
    with caplog.at_level(logging.WARNING):
        _warn_on_agent_skill_mount_drift(
            cast(MsbInvoker, InspectInvoker(binds)), "agents-demo"
        )
    assert "User skill directory mounts" not in caplog.text


def test_warn_on_agent_skill_mount_drift_skips_inspect_without_host_dirs(
    tmp_path, monkeypatch, caplog
) -> None:
    """No host skill dirs: no inspect call and no warning."""
    monkeypatch.setenv("HOME", str(tmp_path))
    invoker = InspectInvoker()
    with caplog.at_level(logging.WARNING):
        _warn_on_agent_skill_mount_drift(cast(MsbInvoker, invoker), "agents-demo")
    assert invoker.calls == []
    assert caplog.text == ""


def test_warn_on_agent_skill_mount_drift_ignores_inspect_failure(
    skills_home, caplog
) -> None:
    """A failing inspect skips the check without a warning."""
    with caplog.at_level(logging.WARNING):
        _warn_on_agent_skill_mount_drift(
            cast(MsbInvoker, InspectInvoker(fail=True)), "agents-demo"
        )
    assert caplog.text == ""
