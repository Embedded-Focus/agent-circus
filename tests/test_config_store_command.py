"""Tests for writable agent configuration store management."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from agent_circus.cli import app
from agent_circus.state import get_agent_config_store_dir

runner = CliRunner()


@patch("agent_circus.commands.config_store.load_config", return_value={})
@patch(
    "agent_circus.commands.config_store.compose_is_service_running", return_value=False
)
@patch("agent_circus.commands.config_store.build_compose_context")
def test_config_reset_removes_store(
    mock_context: MagicMock,
    mock_running: MagicMock,
    mock_load: MagicMock,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = get_agent_config_store_dir(workspace, "codex")
    (store / "config.toml").write_text("model = 'gpt-5'\n")

    result = runner.invoke(
        app,
        ["config", "reset", "codex", "--workspace", str(workspace), "--force"],
    )

    assert result.exit_code == 0
    assert not store.exists()
    assert "Reset codex config store" in result.output


@patch(
    "agent_circus.commands.config_store.compose_is_service_running", return_value=True
)
@patch("agent_circus.commands.config_store.build_compose_context")
def test_config_reset_rejects_running_service(
    mock_context: MagicMock,
    mock_running: MagicMock,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = runner.invoke(
        app,
        ["config", "reset", "codex", "--workspace", str(workspace), "--force"],
    )

    assert result.exit_code == 1
    assert "running services: codex" in result.output


def test_config_reset_requires_agent_or_all(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = runner.invoke(
        app, ["config", "reset", "--workspace", str(workspace), "--force"]
    )

    assert result.exit_code == 1
    assert "exactly one agent or --all" in result.output
