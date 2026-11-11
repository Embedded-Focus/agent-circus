"""Tests for read-only user skill directory mounts in msb_circus.config."""

import logging

from msb_circus.config import resolve_agent_skill_mounts


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
