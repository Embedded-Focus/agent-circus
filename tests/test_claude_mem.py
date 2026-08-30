"""Tests for Claude-Mem integration."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_circus.compose import ComposeContext, _exec_compose
from agent_circus.config import (
    CLAUDE_MEM_CONTAINER_DATA_DIR,
    build_claude_mem_override,
    validate_config,
)
from agent_circus.context import build_compose_context
from agent_circus.exceptions import ConfigurationError
from agent_circus.state import get_claude_mem_dir, get_claude_mem_override_path


def test_build_claude_mem_override_mounts_workspace_memory(tmp_path: Path) -> None:
    result = json.loads(build_claude_mem_override(tmp_path, ["claude-code"]))
    service = result["services"]["claude-code"]

    assert service["environment"] == {
        "AGENT_CIRCUS_CLAUDE_MEM_ENABLED": "true",
        "CLAUDE_MEM_DATA_DIR": CLAUDE_MEM_CONTAINER_DATA_DIR,
    }
    assert service["volumes"] == [f"{tmp_path}:{CLAUDE_MEM_CONTAINER_DATA_DIR}:cached"]


def test_validate_claude_mem_accepts_v1_shape() -> None:
    validate_config(
        {
            "mcp_servers": [],
            "claude_mem": {
                "enabled": True,
                "scope": "workspace",
                "services": ["claude-code"],
            },
        }
    )


@pytest.mark.parametrize(
    ("claude_mem", "message"),
    [
        (True, "claude_mem must be a table"),
        ({"enabled": "yes"}, "claude_mem.enabled must be a boolean"),
        ({"enabled": True, "scope": "user"}, "claude_mem.scope"),
        ({"enabled": True, "services": ["codex"]}, "only supports"),
    ],
)
def test_validate_claude_mem_rejects_unsupported_shapes(
    claude_mem: object,
    message: str,
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        validate_config({"mcp_servers": [], "claude_mem": claude_mem})


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={"shadow": [], "mcp_servers": [], "claude_mem": {"enabled": True}},
)
@patch("agent_circus.context.resolve_config", return_value=None)
def test_context_includes_claude_mem_override_when_enabled(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    with build_compose_context(tmp_path) as ctx:
        override = json.loads(ctx.claude_mem_override or "{}")

    service = override["services"]["claude-code"]
    assert (
        service["environment"]["CLAUDE_MEM_DATA_DIR"] == CLAUDE_MEM_CONTAINER_DATA_DIR
    )
    assert service["volumes"] == [
        f"{get_claude_mem_dir(tmp_path)}:{CLAUDE_MEM_CONTAINER_DATA_DIR}:cached"
    ]


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={"shadow": [], "mcp_servers": [], "claude_mem": {"enabled": False}},
)
@patch("agent_circus.context.resolve_config", return_value=None)
def test_context_omits_claude_mem_override_when_disabled(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    with build_compose_context(tmp_path) as ctx:
        assert ctx.claude_mem_override is None


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={"shadow": [], "mcp_servers": [], "claude_mem": {"enabled": True}},
)
@patch("agent_circus.context.resolve_config", return_value=None)
def test_context_rejects_host_config_when_claude_mem_enabled(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    with (
        pytest.raises(ConfigurationError, match="--host-config"),
        build_compose_context(tmp_path, host_config_service="claude-code"),
    ):
        pass


@patch("agent_circus.compose.subprocess.run")
def test_exec_compose_writes_claude_mem_override(
    mock_run: MagicMock,
    tmp_path: Path,
) -> None:
    mock_run.return_value.returncode = 0
    ctx = ComposeContext(
        workspace=tmp_path,
        project_name="proj",
        compose_file=tmp_path / "compose.yaml",
        cwd=tmp_path,
        claude_mem_override='{"services":{"claude-code":{}}}',
    )

    _exec_compose(["ps"], ctx, capture_output=True)

    path = get_claude_mem_override_path(tmp_path)
    assert path.read_text() == '{"services":{"claude-code":{}}}'
    args = mock_run.call_args.args[0]
    assert str(path) in args


@patch("agent_circus.compose.subprocess.run")
def test_exec_compose_removes_stale_claude_mem_override(
    mock_run: MagicMock,
    tmp_path: Path,
) -> None:
    mock_run.return_value.returncode = 0
    path = get_claude_mem_override_path(tmp_path)
    path.write_text("{}")
    ctx = ComposeContext(
        workspace=tmp_path,
        project_name="proj",
        compose_file=tmp_path / "compose.yaml",
        cwd=tmp_path,
    )

    _exec_compose(["ps"], ctx, capture_output=True)

    assert not path.exists()
