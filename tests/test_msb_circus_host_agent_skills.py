"""Tests for the warning about skills in host ~/.agents/skills."""

import logging

import pytest

from msb_circus.cli import _warn_on_unused_host_agent_skills


def test_warn_on_unused_host_agent_skills_warns_when_skills_exist(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A non-empty host ~/.agents/skills triggers the warning."""
    skills_dir = tmp_path / ".agents" / "skills"
    (skills_dir / "my-skill").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    with caplog.at_level(logging.WARNING):
        _warn_on_unused_host_agent_skills()
    assert "not available in msb-circus sandboxes" in caplog.text
    assert str(skills_dir) in caplog.text
    assert "msb-circus skills add" in caplog.text


def test_warn_on_unused_host_agent_skills_silent_for_empty_dir(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """An empty host ~/.agents/skills produces no warning."""
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    with caplog.at_level(logging.WARNING):
        _warn_on_unused_host_agent_skills()
    assert caplog.text == ""


def test_warn_on_unused_host_agent_skills_silent_without_dir(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A missing host ~/.agents/skills produces no warning."""
    monkeypatch.setenv("HOME", str(tmp_path))
    with caplog.at_level(logging.WARNING):
        _warn_on_unused_host_agent_skills()
    assert caplog.text == ""


def test_warn_on_unused_host_agent_skills_ignores_read_errors(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """An unreadable directory is skipped silently (debug level)."""
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))

    def fail_iterdir():
        raise OSError("denied")

    monkeypatch.setattr(type(skills_dir), "iterdir", lambda self: fail_iterdir())
    with caplog.at_level(logging.WARNING):
        _warn_on_unused_host_agent_skills()
    assert caplog.text == ""
