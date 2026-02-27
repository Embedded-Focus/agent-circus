"""Tests for exec command auto-up behavior."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from agent_circus.cli import app
from agent_circus.compose import ComposeContext

runner = CliRunner()


def _make_ctx(workspace: Path) -> ComposeContext:
    """Create a minimal ComposeContext for testing."""
    return ComposeContext(
        workspace=workspace,
        project_name="test-project",
        compose_file=workspace / "compose.yaml",
        cwd=workspace,
    )


@patch("agent_circus.commands.exec_.compose_exec")
@patch("agent_circus.commands.exec_.compose_up")
@patch("agent_circus.commands.exec_.compose_is_service_running", return_value=False)
@patch("agent_circus.commands.exec_.build_compose_context")
def test_exec_starts_service_when_not_running(
    mock_build_ctx: MagicMock,
    mock_is_running: MagicMock,
    mock_up: MagicMock,
    mock_exec: MagicMock,
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path)
    mock_build_ctx.return_value.__enter__ = MagicMock(return_value=ctx)
    mock_build_ctx.return_value.__exit__ = MagicMock(return_value=False)

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
    mock_is_running.assert_called_once_with(ctx, "claude-code")
    mock_up.assert_called_once_with(ctx, ["claude-code"])
    mock_exec.assert_called_once_with(
        ctx, "claude-code", ["echo", "hello"], no_tty=True
    )
    assert "not running" in result.output
    assert "Starting" in result.output


@patch("agent_circus.commands.exec_.compose_exec")
@patch("agent_circus.commands.exec_.compose_up")
@patch("agent_circus.commands.exec_.compose_is_service_running", return_value=True)
@patch("agent_circus.commands.exec_.build_compose_context")
def test_exec_skips_up_when_already_running(
    mock_build_ctx: MagicMock,
    mock_is_running: MagicMock,
    mock_up: MagicMock,
    mock_exec: MagicMock,
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path)
    mock_build_ctx.return_value.__enter__ = MagicMock(return_value=ctx)
    mock_build_ctx.return_value.__exit__ = MagicMock(return_value=False)

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
    mock_is_running.assert_called_once_with(ctx, "claude-code")
    mock_up.assert_not_called()
    mock_exec.assert_called_once_with(
        ctx, "claude-code", ["echo", "hello"], no_tty=True
    )


@patch("agent_circus.commands.exec_.compose_exec")
@patch("agent_circus.commands.exec_.compose_up")
@patch("agent_circus.commands.exec_.compose_is_service_running", return_value=False)
@patch("agent_circus.commands.exec_.build_compose_context")
def test_exec_auto_up_starts_only_requested_service(
    mock_build_ctx: MagicMock,
    mock_is_running: MagicMock,
    mock_up: MagicMock,
    mock_exec: MagicMock,
    tmp_path: Path,
) -> None:
    ctx = _make_ctx(tmp_path)
    mock_build_ctx.return_value.__enter__ = MagicMock(return_value=ctx)
    mock_build_ctx.return_value.__exit__ = MagicMock(return_value=False)

    result = runner.invoke(
        app, ["exec", "-T", "--workspace", str(tmp_path), "codex", "--", "echo", "hi"]
    )

    assert result.exit_code == 0
    mock_up.assert_called_once_with(ctx, ["codex"])
