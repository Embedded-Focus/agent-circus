"""Tests for compose context assembly."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_circus.context import build_compose_context


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch("agent_circus.context.build_agent_configs_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={"shadow": [".env"], "mcp_servers": []},
)
@patch("agent_circus.context.resolve_config", return_value=None)
def test_context_includes_shadow_override(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_agent_configs: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    with build_compose_context(tmp_path) as ctx:
        assert ctx.shadow_override is not None
        assert ".env" in ctx.shadow_override


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch("agent_circus.context.build_agent_configs_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={"shadow": [], "mcp_servers": []},
)
@patch("agent_circus.context.resolve_config", return_value=None)
def test_context_no_shadow_when_empty(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_agent_configs: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    with build_compose_context(tmp_path) as ctx:
        assert ctx.shadow_override is None


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch("agent_circus.context.build_agent_configs_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={"shadow": [], "mcp_servers": []},
)
@patch("agent_circus.context.resolve_config")
def test_context_deploy_mode(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_agent_configs: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    compose_file = config_dir / "compose.yaml"
    compose_file.touch()
    mock_resolve.return_value = config_dir

    with build_compose_context(tmp_path) as ctx:
        assert ctx.compose_file == compose_file
        assert ctx.cwd == config_dir
        assert ctx.env is None
