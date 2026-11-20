"""Tests for the debug note about skills in host ~/.agents/skills."""

import logging

import pytest

from agent_circus.cli import _log_unused_host_agent_skills

NOTE = "not available in agent-circus sandboxes"


def test_log_unused_host_agent_skills_notes_at_debug_when_skills_exist(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A non-empty host ~/.agents/skills gives a debug note, no warning."""
    skills_dir = tmp_path / ".agents" / "skills"
    (skills_dir / "my-skill").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    with caplog.at_level(logging.DEBUG, logger="agent_circus.cli"):
        _log_unused_host_agent_skills()
    notes = [r for r in caplog.records if NOTE in r.getMessage()]
    assert len(notes) == 1
    assert notes[0].levelno == logging.DEBUG
    assert str(skills_dir) in notes[0].getMessage()
    assert "agent-circus skills add" in notes[0].getMessage()
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_log_unused_host_agent_skills_silent_for_empty_dir(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """An empty host ~/.agents/skills produces no note."""
    (tmp_path / ".agents" / "skills").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    with caplog.at_level(logging.DEBUG, logger="agent_circus.cli"):
        _log_unused_host_agent_skills()
    assert NOTE not in caplog.text


def test_log_unused_host_agent_skills_silent_without_dir(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A missing host ~/.agents/skills produces no note."""
    monkeypatch.setenv("HOME", str(tmp_path))
    with caplog.at_level(logging.DEBUG, logger="agent_circus.cli"):
        _log_unused_host_agent_skills()
    assert NOTE not in caplog.text


def test_log_unused_host_agent_skills_ignores_read_errors(
    tmp_path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """An unreadable directory gives only the read-error debug line."""
    skills_dir = tmp_path / ".agents" / "skills"
    skills_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))

    def fail_iterdir():
        raise OSError("denied")

    monkeypatch.setattr(type(skills_dir), "iterdir", lambda self: fail_iterdir())
    with caplog.at_level(logging.DEBUG, logger="agent_circus.cli"):
        _log_unused_host_agent_skills()
    assert NOTE not in caplog.text
    assert "Could not read host skill directory" in caplog.text
