"""Tests for exec command auto-up behavior."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from agent_circus.cli import app

runner = CliRunner()


@patch("agent_circus.commands.exec_.compose_exec")
@patch("agent_circus.commands.exec_.compose_up")
@patch("agent_circus.commands.exec_.compose_is_service_running", return_value=False)
def test_exec_starts_service_when_not_running(
    mock_is_running: MagicMock,
    mock_up: MagicMock,
    mock_exec: MagicMock,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "exec",
            "-T",
            "--workspace",
            str(tmp_path),
            "claude-code",
            "--",
            "echo",
            "hello",
        ],
    )

    assert result.exit_code == 0
    mock_is_running.assert_called_once_with(tmp_path, "claude-code")
    mock_up.assert_called_once_with(tmp_path, ["claude-code"])
    mock_exec.assert_called_once_with(
        tmp_path, "claude-code", ["echo", "hello"], no_tty=True
    )
    assert "not running" in result.output
    assert "Starting" in result.output


@patch("agent_circus.commands.exec_.compose_exec")
@patch("agent_circus.commands.exec_.compose_up")
@patch("agent_circus.commands.exec_.compose_is_service_running", return_value=True)
def test_exec_skips_up_when_already_running(
    mock_is_running: MagicMock,
    mock_up: MagicMock,
    mock_exec: MagicMock,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "exec",
            "-T",
            "--workspace",
            str(tmp_path),
            "claude-code",
            "--",
            "echo",
            "hello",
        ],
    )

    assert result.exit_code == 0
    mock_is_running.assert_called_once_with(tmp_path, "claude-code")
    mock_up.assert_not_called()
    mock_exec.assert_called_once_with(
        tmp_path, "claude-code", ["echo", "hello"], no_tty=True
    )


@patch("agent_circus.commands.exec_.compose_exec")
@patch("agent_circus.commands.exec_.compose_up")
@patch("agent_circus.commands.exec_.compose_is_service_running", return_value=False)
def test_exec_auto_up_starts_only_requested_service(
    mock_is_running: MagicMock,
    mock_up: MagicMock,
    mock_exec: MagicMock,
    tmp_path: Path,
) -> None:
    result = runner.invoke(
        app, ["exec", "-T", "--workspace", str(tmp_path), "codex", "--", "echo", "hi"]
    )

    assert result.exit_code == 0
    mock_up.assert_called_once_with(tmp_path, ["codex"])
