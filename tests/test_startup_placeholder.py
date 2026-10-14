"""Tests for cleanup of stale startup hook placeholder files.

Older versions bind-mounted ``config.toml`` startup hooks over
``.agent-circus/hooks/startup.sh``, which let the container runtime leave
zero-byte placeholders in the project.  Current versions mount under ``/run``
and remove any leftover via :func:`agent_circus.compose.remove_startup_placeholder`.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from agent_circus.cli import app
from agent_circus.compose import (
    ComposeContext,
    compose_exec,
    compose_up,
    remove_startup_placeholder,
)

runner = CliRunner()


def _make_ctx(tmp_path: Path) -> ComposeContext:
    """Build a minimal compose context rooted at *tmp_path*."""
    return ComposeContext(
        workspace=tmp_path,
        project_name="test-project",
        compose_file=tmp_path / "compose.yaml",
        cwd=tmp_path,
    )


def _make_placeholder(tmp_path: Path, content: str = "") -> Path:
    """Create ``.agent-circus/hooks/startup.sh`` under *tmp_path* and return it."""
    hooks = tmp_path / ".agent-circus" / "hooks"
    hooks.mkdir(parents=True)
    placeholder = hooks / "startup.sh"
    placeholder.write_text(content)
    return placeholder


def test_remove_startup_placeholder_removes_empty_file(
    tmp_path: Path,
) -> None:
    placeholder = _make_placeholder(tmp_path)
    hooks = placeholder.parent

    remove_startup_placeholder(_make_ctx(tmp_path))

    assert not placeholder.exists()
    assert not hooks.exists()


def test_remove_startup_placeholder_keeps_nonempty_script(
    tmp_path: Path,
) -> None:
    placeholder = _make_placeholder(tmp_path, "echo real\n")

    remove_startup_placeholder(_make_ctx(tmp_path))

    assert placeholder.read_text() == "echo real\n"
    assert placeholder.parent.is_dir()


def test_remove_startup_placeholder_keeps_nonempty_hooks_dir(
    tmp_path: Path,
) -> None:
    placeholder = _make_placeholder(tmp_path)
    sibling = placeholder.parent / "base-user.sh"
    sibling.write_text("npm install -g tool\n")

    remove_startup_placeholder(_make_ctx(tmp_path))

    assert not placeholder.exists()
    assert sibling.read_text() == "npm install -g tool\n"
    assert placeholder.parent.is_dir()


def test_remove_startup_placeholder_removes_empty_dir_without_file(
    tmp_path: Path,
) -> None:
    hooks = tmp_path / ".agent-circus" / "hooks"
    hooks.mkdir(parents=True)

    remove_startup_placeholder(_make_ctx(tmp_path))

    assert not hooks.exists()


def test_remove_startup_placeholder_is_noop_when_absent(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    with patch("agent_circus.compose._exec_compose") as mock_exec:
        remove_startup_placeholder(ctx)
    mock_exec.assert_not_called()


def test_remove_startup_placeholder_uses_container_when_host_removal_fails(
    tmp_path: Path, monkeypatch: MagicMock
) -> None:
    _make_placeholder(tmp_path)
    monkeypatch.setattr(
        Path,
        "unlink",
        MagicMock(side_effect=PermissionError(13, "Permission denied")),
    )
    with (
        patch(
            "agent_circus.compose._exec_compose",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="abc123def\n"
            ),
        ) as mock_exec,
        patch(
            "agent_circus.compose.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=""),
        ) as mock_run,
    ):
        remove_startup_placeholder(_make_ctx(tmp_path))

    # The images lookup goes through compose; the removal goes through the engine.
    assert mock_exec.call_args[0][0][:2] == ["images", "-q"]
    run_cmd = mock_run.call_args[0][0]
    assert "run" in run_cmd
    assert "--user" in run_cmd and run_cmd[run_cmd.index("--user") + 1] == "root"
    assert (
        "--entrypoint" in run_cmd and run_cmd[run_cmd.index("--entrypoint") + 1] == "sh"
    )
    assert "-v" in run_cmd
    assert run_cmd[run_cmd.index("-v") + 1].endswith("/.agent-circus:/project-config")
    script = run_cmd[run_cmd.index("-c") + 1]
    assert "rm -f" in script and "rmdir" in script and "hooks" in script
    # The startup.sh path is passed as a shell argument, not interpolated.
    assert "hooks/startup.sh" in run_cmd


def test_remove_startup_placeholder_skips_container_when_run_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    placeholder = _make_placeholder(tmp_path)
    monkeypatch.setattr(Path, "unlink", MagicMock(side_effect=OSError))
    with (
        patch(
            "agent_circus.compose._exec_compose",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="img\n"
            ),
        ),
        patch(
            "agent_circus.compose.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="nope"
            ),
        ),
    ):
        # A failed container-side removal must never crash the caller.
        remove_startup_placeholder(_make_ctx(tmp_path))

    assert placeholder.exists()


@patch("agent_circus.commands.remove.remove_startup_placeholder")
@patch("agent_circus.commands.remove.compose_down")
@patch("agent_circus.commands.remove.build_compose_context")
@patch("agent_circus.commands.remove.get_workspace_path")
def test_remove_command_cleans_startup_placeholder(
    mock_workspace: MagicMock,
    mock_ctx: MagicMock,
    mock_down: MagicMock,
    mock_cleanup: MagicMock,
    tmp_path: Path,
) -> None:
    mock_workspace.return_value = tmp_path

    result = runner.invoke(app, ["remove", "--force"])

    assert result.exit_code == 0
    mock_cleanup.assert_called_once()


def test_compose_up_removes_stale_placeholder(tmp_path: Path) -> None:
    placeholder = _make_placeholder(tmp_path)

    with patch(
        "agent_circus.compose._exec_compose",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=""),
    ):
        compose_up(_make_ctx(tmp_path), ["opencode"])

    assert not placeholder.exists()


def test_compose_exec_removes_stale_placeholder(tmp_path: Path) -> None:
    placeholder = _make_placeholder(tmp_path)

    with patch(
        "agent_circus.compose._exec_compose",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=""),
    ):
        compose_exec(_make_ctx(tmp_path), "opencode", ["bash"])

    assert not placeholder.exists()


def test_remove_startup_placeholder_skips_container_without_local_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    placeholder = _make_placeholder(tmp_path)
    monkeypatch.setattr(
        Path,
        "unlink",
        MagicMock(side_effect=PermissionError(13, "Permission denied")),
    )
    with (
        patch(
            "agent_circus.compose._exec_compose",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="\n"
            ),
        ),
        patch(
            "agent_circus.compose.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=""),
        ) as mock_run,
    ):
        remove_startup_placeholder(_make_ctx(tmp_path))

    mock_run.assert_not_called()
    assert placeholder.exists()
