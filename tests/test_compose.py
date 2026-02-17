"""Tests for compose helper functions."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_circus.compose import compose_is_service_running
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
