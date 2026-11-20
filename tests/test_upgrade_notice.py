"""Tests for the daily upgrade notice of agent-circus 1.x."""

import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_circus.cli import app
from agent_circus.upgrade_notice import (
    UPGRADE_NOTICE_MESSAGE,
    maybe_show_upgrade_notice,
    notice_state_file,
    should_show_notice,
)

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_notice_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the notice state at a temporary directory.

    The autouse ``isolate_user_data`` fixture sets
    ``AGENT_CIRCUS_NO_UPGRADE_NOTICE`` to keep the notice out of other
    tests' CLI output; these tests undo that to exercise the notice.

    :param tmp_path: Per-test temporary directory.
    :param monkeypatch: Pytest environment patch helper.
    :returns: None.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("AGENT_CIRCUS_NO_UPGRADE_NOTICE", raising=False)


def test_notice_shown_when_no_state_file_exists(tmp_path: Path) -> None:
    """The notice is shown when the state file does not exist yet."""
    assert should_show_notice(tmp_path / "missing" / ".upgrade-notice", time.time())


def test_notice_hidden_while_state_file_is_young(tmp_path: Path) -> None:
    """The notice is hidden when the state file is younger than the interval."""
    state_file = tmp_path / ".upgrade-notice"
    state_file.touch()
    assert not should_show_notice(state_file, time.time())


def test_notice_shown_again_after_interval(tmp_path: Path) -> None:
    """The notice is shown again once the interval has passed."""
    state_file = tmp_path / ".upgrade-notice"
    state_file.touch()
    now = time.time()
    assert not should_show_notice(state_file, now + 3600.0)
    assert should_show_notice(state_file, now + 86400.0)


def test_notice_hidden_when_environment_variable_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The notice is hidden when AGENT_CIRCUS_NO_UPGRADE_NOTICE is non-empty."""
    state_file = tmp_path / ".upgrade-notice"
    monkeypatch.setenv("AGENT_CIRCUS_NO_UPGRADE_NOTICE", "1")
    assert not should_show_notice(state_file, time.time())


def test_notice_hidden_when_environment_variable_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty AGENT_CIRCUS_NO_UPGRADE_NOTICE does not hide the notice."""
    state_file = tmp_path / ".upgrade-notice"
    monkeypatch.setenv("AGENT_CIRCUS_NO_UPGRADE_NOTICE", "")
    assert should_show_notice(state_file, time.time())


def test_notice_state_file_respects_xdg_state_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """XDG_STATE_HOME decides where the state file lives."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    assert (
        notice_state_file()
        == tmp_path / "xdg-state" / "agent-circus" / ".upgrade-notice"
    )


def test_notice_state_file_falls_back_to_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unset or relative XDG_STATE_HOME falls back to ~/.local/state."""
    monkeypatch.delenv("XDG_STATE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    expected = (
        tmp_path / "home" / ".local" / "state" / "agent-circus" / ".upgrade-notice"
    )
    assert notice_state_file() == expected

    monkeypatch.setenv("XDG_STATE_HOME", "relative/path")
    assert notice_state_file() == expected


def test_maybe_show_upgrade_notice_records_state_file(tmp_path: Path) -> None:
    """Showing the notice creates the state file."""
    maybe_show_upgrade_notice()
    state_file = notice_state_file()
    assert state_file.exists()
    assert not should_show_notice(state_file, time.time())


def test_notice_goes_to_stderr_not_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI writes the notice to stderr and keeps stdout clean."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert UPGRADE_NOTICE_MESSAGE in result.stderr
    assert UPGRADE_NOTICE_MESSAGE not in result.stdout
    assert (tmp_path / "xdg-state" / "agent-circus" / ".upgrade-notice").exists()


def test_notice_suppressed_in_cli_when_environment_variable_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI stays quiet when AGENT_CIRCUS_NO_UPGRADE_NOTICE is set."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("AGENT_CIRCUS_NO_UPGRADE_NOTICE", "1")
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert UPGRADE_NOTICE_MESSAGE not in result.stderr
    assert not (tmp_path / "xdg-state" / "agent-circus" / ".upgrade-notice").exists()
