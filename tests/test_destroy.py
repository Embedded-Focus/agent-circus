"""Tests for the destroy command."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from agent_circus.cli import app

runner = CliRunner()


@patch("agent_circus.commands.destroy.compose_down")
@patch("agent_circus.commands.destroy.build_compose_context")
@patch("agent_circus.commands.destroy.config_exists", return_value=False)
@patch("agent_circus.commands.destroy.get_workspace_path")
def test_destroy_force_skips_confirmation(
    mock_workspace: MagicMock,
    mock_config_exists: MagicMock,
    mock_ctx: MagicMock,
    mock_down: MagicMock,
    tmp_path: Path,
) -> None:
    mock_workspace.return_value = tmp_path

    result = runner.invoke(app, ["destroy", "--force"])

    assert result.exit_code == 0
    assert "Removing containers..." in result.output
    assert "Containers removed successfully." in result.output
    assert "Are you sure" not in result.output


@patch("agent_circus.commands.destroy.compose_down")
@patch("agent_circus.commands.destroy.build_compose_context")
@patch("agent_circus.commands.destroy.config_exists", return_value=False)
@patch("agent_circus.commands.destroy.get_workspace_path")
def test_destroy_aborts_when_user_declines(
    mock_workspace: MagicMock,
    mock_config_exists: MagicMock,
    mock_ctx: MagicMock,
    mock_down: MagicMock,
    tmp_path: Path,
) -> None:
    mock_workspace.return_value = tmp_path

    result = runner.invoke(app, ["destroy"], input="n\n")

    assert result.exit_code == 0
    assert "Aborted." in result.output
    mock_ctx.assert_not_called()


@patch("agent_circus.commands.destroy.destroy_deployed_files", return_value=[])
@patch("agent_circus.commands.destroy.compose_down")
@patch("agent_circus.commands.destroy.build_compose_context")
@patch("agent_circus.commands.destroy.config_exists", return_value=True)
@patch("agent_circus.commands.destroy.get_workspace_path")
def test_destroy_removes_deployed_files_in_deploy_mode(
    mock_workspace: MagicMock,
    mock_config_exists: MagicMock,
    mock_ctx: MagicMock,
    mock_down: MagicMock,
    mock_destroy_files: MagicMock,
    tmp_path: Path,
) -> None:
    mock_workspace.return_value = tmp_path

    result = runner.invoke(app, ["destroy", "--force"])

    assert result.exit_code == 0
    mock_destroy_files.assert_called_once_with(tmp_path)
    assert "Removing deployed files..." in result.output
    assert "Deployed files removed successfully." in result.output


@patch("agent_circus.commands.destroy.compose_down")
@patch("agent_circus.commands.destroy.build_compose_context")
@patch("agent_circus.commands.destroy.config_exists", return_value=False)
@patch("agent_circus.commands.destroy.get_workspace_path")
def test_destroy_skips_deployed_files_in_instant_mode(
    mock_workspace: MagicMock,
    mock_config_exists: MagicMock,
    mock_ctx: MagicMock,
    mock_down: MagicMock,
    tmp_path: Path,
) -> None:
    mock_workspace.return_value = tmp_path

    result = runner.invoke(app, ["destroy", "--force"])

    assert result.exit_code == 0
    assert "No deployed files to remove (running in instant mode)." in result.output
