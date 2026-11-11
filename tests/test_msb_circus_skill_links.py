"""Tests for the broken skill link check in msb_circus.skills."""

import logging
import os
from pathlib import Path

import pytest

from msb_circus.cli import _warn_on_broken_skill_links
from msb_circus.config import DEFAULT_MOUNTS, MsbConfig
from msb_circus.skills import BrokenSkillLink, LinkProblem, find_broken_skill_links


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path]:
    """Create a workspace and a home with one real skill in each.

    :param tmp_path: Per-test temporary directory.
    :returns: ``(workspace, home)`` directories.
    """
    workspace = tmp_path / "ws"
    home = tmp_path / "home"
    (workspace / ".agents" / "skills" / "proj").mkdir(parents=True)
    (workspace / ".claude" / "skills").mkdir(parents=True)
    (home / ".agents" / "skills" / "user").mkdir(parents=True)
    (home / ".claude" / "skills").mkdir(parents=True)
    return workspace, home


def _mounts(workspace: Path, home: Path) -> list[tuple[str, str]]:
    """Return the default mounts: workspace, ~/.claude and user skills.

    :param workspace: Workspace directory.
    :param home: Fake home directory.
    :returns: ``(host path, guest spec)`` pairs.
    """
    return [
        (str(workspace), "/workspace"),
        (str(home / ".claude"), "/root/.claude"),
        (str(home / ".agents/skills"), "/root/.agents/skills:ro"),
    ]


def test_find_broken_skill_links_accepts_relative_project_link(layout) -> None:
    """A relative link inside the workspace works in the sandbox."""
    workspace, home = layout
    (workspace / ".claude/skills/proj").symlink_to("../../.agents/skills/proj")
    result = find_broken_skill_links(
        [workspace / ".claude/skills"], _mounts(workspace, home)
    )
    assert result == []


def test_find_broken_skill_links_reports_missing_target(layout) -> None:
    """A link whose target is gone on the host is reported as missing."""
    workspace, home = layout
    link = workspace / ".claude/skills/gone"
    link.symlink_to("../../.agents/skills/gone")
    result = find_broken_skill_links(
        [workspace / ".claude/skills"], _mounts(workspace, home)
    )
    assert result == [
        BrokenSkillLink(link, "../../.agents/skills/gone", LinkProblem.MISSING)
    ]


def test_find_broken_skill_links_reports_absolute_host_link(layout) -> None:
    """An absolute link to a host home path does not resolve in the guest."""
    workspace, home = layout
    target = str(home / ".agents/skills/user")
    link = workspace / ".claude/skills/user"
    link.symlink_to(target)
    result = find_broken_skill_links(
        [workspace / ".claude/skills"], _mounts(workspace, home)
    )
    assert result == [BrokenSkillLink(link, target, LinkProblem.SANDBOX)]


def test_find_broken_skill_links_accepts_absolute_link_with_mirror(layout) -> None:
    """The worktree mirror mount makes absolute workspace links work."""
    workspace, home = layout
    (workspace / ".claude/skills/proj").symlink_to(workspace / ".agents/skills/proj")
    mounts = [*_mounts(workspace, home), (str(workspace), str(workspace))]
    result = find_broken_skill_links([workspace / ".claude/skills"], mounts)
    assert result == []


def test_find_broken_skill_links_accepts_relative_user_link(layout) -> None:
    """``~/.claude/skills`` links into ``~/.agents/skills`` work when mounted."""
    workspace, home = layout
    (home / ".claude/skills/user").symlink_to("../../.agents/skills/user")
    result = find_broken_skill_links(
        [home / ".claude/skills"], _mounts(workspace, home)
    )
    assert result == []


def test_find_broken_skill_links_reports_user_link_without_mount(layout) -> None:
    """Without the ``~/.agents/skills`` mount, the user link breaks."""
    workspace, home = layout
    link = home / ".claude/skills/user"
    link.symlink_to("../../.agents/skills/user")
    mounts = _mounts(workspace, home)[:2]
    result = find_broken_skill_links([home / ".claude/skills"], mounts)
    assert result == [
        BrokenSkillLink(link, "../../.agents/skills/user", LinkProblem.SANDBOX)
    ]


def test_find_broken_skill_links_ignores_dirs_and_missing_dirs(layout) -> None:
    """Real skill directories and missing skill dirs are not reported."""
    workspace, home = layout
    result = find_broken_skill_links(
        [workspace / ".agents/skills", workspace / ".pi/skills"],
        _mounts(workspace, home),
    )
    assert result == []


def test_warn_on_broken_skill_links_logs_absolute_link(
    layout, monkeypatch, caplog
) -> None:
    """The CLI check warns about an absolute link into the host home."""
    workspace, home = layout
    monkeypatch.setenv("HOME", str(home))
    (workspace / ".claude/skills/user").symlink_to(home / ".agents/skills/user")
    with caplog.at_level(logging.WARNING):
        _warn_on_broken_skill_links(MsbConfig(mounts=dict(DEFAULT_MOUNTS)), workspace)
    assert "These skill links do not work in the sandbox" in caplog.text
    assert "target not visible in the sandbox" in caplog.text
    assert "Host config directory" not in caplog.text


def test_warn_on_broken_skill_links_silent_for_relative_links(
    layout, monkeypatch, caplog
) -> None:
    """Relative project and user links produce no warning."""
    workspace, home = layout
    monkeypatch.setenv("HOME", str(home))
    (workspace / ".claude/skills/proj").symlink_to("../../.agents/skills/proj")
    os.symlink("../../.agents/skills/user", home / ".claude/skills/user")
    with caplog.at_level(logging.WARNING):
        _warn_on_broken_skill_links(MsbConfig(mounts=dict(DEFAULT_MOUNTS)), workspace)
    assert caplog.text == ""
