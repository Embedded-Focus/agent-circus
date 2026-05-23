"""Tests for low-level compose functions."""

import dataclasses
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_circus.compose import (
    ComposeContext,
    _exec_compose,
    compose_down,
    compose_is_service_running,
    compose_up,
)
from agent_circus.config import build_shadow_override
from agent_circus.exceptions import ComposeError
from agent_circus.state import get_port_forwards_override_path


def _make_ctx(tmp_path: Path) -> ComposeContext:
    """Create a minimal ComposeContext for testing."""
    return ComposeContext(
        workspace=tmp_path,
        project_name="test-project",
        compose_file=tmp_path / "compose.yaml",
        cwd=tmp_path,
    )


@patch("agent_circus.compose._exec_compose")
def test_is_service_running_returns_true_on_json_output(
    mock_exec: MagicMock,
    tmp_path: Path,
) -> None:
    mock_exec.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout='{"Name":"claude-code-1","State":"running"}\n'
    )
    assert compose_is_service_running(_make_ctx(tmp_path), "claude-code") is True


@patch("agent_circus.compose._exec_compose")
def test_is_service_running_returns_false_on_empty_output(
    mock_exec: MagicMock,
    tmp_path: Path,
) -> None:
    mock_exec.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=""
    )
    assert compose_is_service_running(_make_ctx(tmp_path), "claude-code") is False


@patch("agent_circus.compose._exec_compose")
def test_is_service_running_returns_false_on_empty_json_array(
    mock_exec: MagicMock,
    tmp_path: Path,
) -> None:
    mock_exec.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="[]"
    )
    assert compose_is_service_running(_make_ctx(tmp_path), "claude-code") is False


@patch("agent_circus.compose._exec_compose", side_effect=ComposeError("fail"))
def test_is_service_running_returns_false_on_compose_error(
    mock_exec: MagicMock,
    tmp_path: Path,
) -> None:
    assert compose_is_service_running(_make_ctx(tmp_path), "claude-code") is False


@patch("agent_circus.compose._exec_compose")
def test_compose_down_passes_services_as_trailing_args(
    mock_exec: MagicMock,
    tmp_path: Path,
) -> None:
    mock_exec.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=""
    )
    compose_down(_make_ctx(tmp_path), services=["claude-code", "codex"])

    called_args = mock_exec.call_args[0][0]
    assert called_args[0] == "down"
    assert "claude-code" in called_args
    assert "codex" in called_args
    assert called_args.index("claude-code") > called_args.index("down")


@patch("agent_circus.compose._exec_compose")
def test_compose_down_no_services_omits_trailing_args(
    mock_exec: MagicMock,
    tmp_path: Path,
) -> None:
    mock_exec.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=""
    )
    compose_down(_make_ctx(tmp_path), services=None)

    called_args = mock_exec.call_args[0][0]
    assert called_args == ["down"]


@patch("agent_circus.compose._exec_compose")
def test_compose_up_runs_data_store_seeder_before_compose(
    mock_exec: MagicMock,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def seed() -> None:
        calls.append("seed")

    def run_compose(
        *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append("compose")
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="")

    mock_exec.side_effect = run_compose
    ctx = dataclasses.replace(_make_ctx(tmp_path), data_store_seeder=seed)

    compose_up(ctx, ["codex"])

    assert calls == ["seed", "compose"]


@patch("agent_circus.compose.subprocess.run")
def test_exec_compose_writes_port_forwards_override(
    mock_run: MagicMock,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    override = '{"services":{"codex":{"ports":["127.0.0.1:3333:3333/tcp"]}}}'
    ctx = ComposeContext(
        workspace=tmp_path / "workspace",
        project_name="test-project",
        compose_file=tmp_path / "compose.yaml",
        cwd=tmp_path,
        port_forwards_override=override,
    )
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=""
    )

    _exec_compose(["ps"], ctx, capture_output=True)

    override_path = get_port_forwards_override_path(ctx.workspace)
    called_cmd = mock_run.call_args[0][0]
    assert override_path.read_text() == override
    assert "-f" in called_cmd
    assert str(override_path) in called_cmd


def test_build_shadow_override_produces_valid_json() -> None:
    result = build_shadow_override([".env", ".env.local"])
    data = json.loads(result)

    assert "services" in data
    for service in ("claude-code", "codex", "mistral-vibe", "opencode"):
        volumes = data["services"][service]["volumes"]
        assert "/dev/null:/workspace/.env:ro" in volumes
        assert "/dev/null:/workspace/.env.local:ro" in volumes


def test_build_shadow_override_empty_list() -> None:
    result = build_shadow_override([])
    data = json.loads(result)

    for service in ("claude-code", "codex", "mistral-vibe", "opencode"):
        assert data["services"][service]["volumes"] == []
