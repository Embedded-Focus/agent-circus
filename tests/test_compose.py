"""Tests for compose helper functions."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_circus.compose import _build_shadow_override, compose_is_service_running
from agent_circus.exceptions import ComposeError


@patch("agent_circus.compose._run_compose")
def test_is_service_running_returns_true_on_json_output(
    mock_run: MagicMock,
    tmp_path: Path,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='{"Name":"claude-code-1","State":"running"}\n'
    )
    assert compose_is_service_running(tmp_path, "claude-code") is True


@patch("agent_circus.compose._run_compose")
def test_is_service_running_returns_false_on_empty_output(
    mock_run: MagicMock,
    tmp_path: Path,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=""
    )
    assert compose_is_service_running(tmp_path, "claude-code") is False


@patch("agent_circus.compose._run_compose")
def test_is_service_running_returns_false_on_empty_json_array(
    mock_run: MagicMock,
    tmp_path: Path,
) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="[]"
    )
    assert compose_is_service_running(tmp_path, "claude-code") is False


@patch("agent_circus.compose._run_compose", side_effect=ComposeError("fail"))
def test_is_service_running_returns_false_on_compose_error(
    mock_run: MagicMock,
    tmp_path: Path,
) -> None:
    assert compose_is_service_running(tmp_path, "claude-code") is False


def test_build_shadow_override_produces_valid_json() -> None:
    result = _build_shadow_override([".env", ".env.local"])
    data = json.loads(result)

    assert "services" in data
    for service in ("claude-code", "codex", "mistral-vibe"):
        volumes = data["services"][service]["volumes"]
        assert "/dev/null:/workspace/.env:ro" in volumes
        assert "/dev/null:/workspace/.env.local:ro" in volumes


def test_build_shadow_override_empty_list() -> None:
    result = _build_shadow_override([])
    data = json.loads(result)

    for service in ("claude-code", "codex", "mistral-vibe"):
        assert data["services"][service]["volumes"] == []


@patch("agent_circus.compose._exec_compose")
@patch(
    "agent_circus.compose.load_config",
    return_value={"shadow": [".env"]},
)
@patch("agent_circus.compose.resolve_config", return_value=None)
def test_run_compose_passes_shadow_to_exec(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_exec: MagicMock,
    tmp_path: Path,
) -> None:
    from agent_circus.compose import _run_compose

    _run_compose(["ps"], tmp_path, capture_output=True)

    mock_load.assert_called_once_with(tmp_path)
    _, kwargs = mock_exec.call_args
    assert kwargs["shadow"] == [".env"]


@patch("agent_circus.compose._exec_compose")
@patch(
    "agent_circus.compose.load_config",
    return_value={"shadow": []},
)
@patch("agent_circus.compose.resolve_config", return_value=None)
def test_run_compose_passes_empty_shadow(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_exec: MagicMock,
    tmp_path: Path,
) -> None:
    from agent_circus.compose import _run_compose

    _run_compose(["ps"], tmp_path, capture_output=True)

    _, kwargs = mock_exec.call_args
    assert kwargs["shadow"] == []
