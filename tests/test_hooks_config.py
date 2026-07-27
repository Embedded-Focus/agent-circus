"""Tests for config.toml inline hook script support."""

import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_circus.config import write_hook_script
from agent_circus.context import (
    _ENV_INJECTION_ANCHOR,
    _write_config_hooks,
    build_compose_context,
)

# ---------------------------------------------------------------------------
# write_hook_script
# ---------------------------------------------------------------------------


def test_write_hook_script_prepends_shebang_when_missing(tmp_path: Path) -> None:
    dest = tmp_path / "hook.sh"
    write_hook_script("apt-get install -y tree\n", dest)
    content = dest.read_text()
    assert content.startswith("#!/usr/bin/env bash\n")
    assert "apt-get install -y tree" in content


def test_write_hook_script_does_not_double_add_shebang(tmp_path: Path) -> None:
    dest = tmp_path / "hook.sh"
    write_hook_script("#!/bin/bash\napt-get install -y tree\n", dest)
    content = dest.read_text()
    assert content.count("#!") == 1
    assert content.startswith("#!/bin/bash\n")


def test_write_hook_script_sets_executable_bit(tmp_path: Path) -> None:
    dest = tmp_path / "hook.sh"
    write_hook_script("echo hello\n", dest)
    mode = dest.stat().st_mode
    assert mode & stat.S_IXUSR, "owner execute bit should be set"


def test_write_hook_script_creates_parent_dirs(tmp_path: Path) -> None:
    dest = tmp_path / "a" / "b" / "hook.sh"
    write_hook_script("echo hi\n", dest)
    assert dest.is_file()


# ---------------------------------------------------------------------------
# _write_config_hooks
# ---------------------------------------------------------------------------


def test_write_config_hooks_writes_base_root(tmp_path: Path) -> None:
    hooks_dst = tmp_path / "hooks"
    hooks_dst.mkdir()
    _write_config_hooks({"base_root": "apt-get install -y ripgrep\n"}, tmp_path)
    content = (hooks_dst / "base-root.sh").read_text()
    assert "apt-get install -y ripgrep" in content


def test_write_config_hooks_writes_base_user(tmp_path: Path) -> None:
    hooks_dst = tmp_path / "hooks"
    hooks_dst.mkdir()
    _write_config_hooks({"base_user": "npm install -g typescript\n"}, tmp_path)
    content = (hooks_dst / "base-user.sh").read_text()
    assert "npm install -g typescript" in content


def test_write_config_hooks_skips_absent_keys(tmp_path: Path) -> None:
    hooks_dst = tmp_path / "hooks"
    hooks_dst.mkdir()
    # Only base_root provided — base-user.sh should not be created.
    _write_config_hooks({"base_root": "echo root\n"}, tmp_path)
    assert not (hooks_dst / "base-user.sh").exists()


def test_write_config_hooks_ignores_startup_key(tmp_path: Path) -> None:
    # startup is handled upstream in build_compose_context(); _write_config_hooks
    # should not create any file for it.
    hooks_dst = tmp_path / "hooks"
    hooks_dst.mkdir()
    _write_config_hooks({"startup": "source .env\n"}, tmp_path)
    assert not (hooks_dst / "startup.sh").exists()


# ---------------------------------------------------------------------------
# Integration: config hooks override project hooks in instant mode
# ---------------------------------------------------------------------------


def _make_template_dir(base: Path) -> Path:
    template_dir = base / "template"
    template_dir.mkdir()
    hooks_dir = template_dir / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "base-root.sh").write_text("")
    (hooks_dir / "base-user.sh").write_text("")
    (template_dir / "Dockerfile").write_text(
        f"FROM scratch{_ENV_INJECTION_ANCHOR} []\n"
    )
    return template_dir


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={
        "shadow": [],
        "mcp_servers": [],
        "hooks": {"base_root": "echo from-config\n"},
    },
)
@patch("agent_circus.context.resolve_config", return_value=None)
@patch("agent_circus.context.template_dir_context")
def test_config_hooks_override_project_hooks_in_instant_mode(
    mock_tdc: MagicMock,
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    template_dir = _make_template_dir(tmp_path)
    mock_tdc.return_value.__enter__ = MagicMock(return_value=template_dir)
    mock_tdc.return_value.__exit__ = MagicMock(return_value=False)

    # Project hook has different content — config.toml should win.
    hooks_src = workspace / ".agent-circus" / "hooks"
    hooks_src.mkdir(parents=True)
    (hooks_src / "base-root.sh").write_text("echo from-project\n")

    with build_compose_context(workspace) as ctx:
        content = (ctx.cwd / "hooks" / "base-root.sh").read_text()
    assert "from-config" in content
    assert "from-project" not in content


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={
        "shadow": [],
        "mcp_servers": [],
        "hooks": {"base_root": "echo deploy-hooks-ignored\n"},
    },
)
@patch("agent_circus.context.resolve_config")
def test_deploy_mode_warns_on_config_build_hooks(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "compose.yaml").touch()
    mock_resolve.return_value = config_dir

    import logging

    with (
        caplog.at_level(logging.WARNING, logger="agent_circus.context"),
        build_compose_context(tmp_path),
    ):
        pass
    assert any("deploy mode" in msg for msg in caplog.messages)


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={
        "shadow": [],
        "mcp_servers": [],
        "hooks": {"startup": "uv sync\n"},
    },
)
@patch("agent_circus.context.resolve_config", return_value=None)
@patch("agent_circus.context.template_dir_context")
def test_startup_hook_sets_override_in_instant_mode(
    mock_tdc: MagicMock,
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    template_dir = _make_template_dir(tmp_path)
    mock_tdc.return_value.__enter__ = MagicMock(return_value=template_dir)
    mock_tdc.return_value.__exit__ = MagicMock(return_value=False)

    with build_compose_context(workspace) as ctx:
        assert ctx.startup_hook_override is not None
        assert "/workspace/.agent-circus/hooks/startup.sh" in ctx.startup_hook_override
        assert ":ro" in ctx.startup_hook_override


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={
        "shadow": [],
        "mcp_servers": [],
        "hooks": {"startup": "uv sync\n"},
    },
)
@patch("agent_circus.context.resolve_config", return_value=None)
@patch("agent_circus.context.template_dir_context")
def test_startup_hook_written_to_state_dir(
    mock_tdc: MagicMock,
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    template_dir = _make_template_dir(tmp_path)
    mock_tdc.return_value.__enter__ = MagicMock(return_value=template_dir)
    mock_tdc.return_value.__exit__ = MagicMock(return_value=False)

    with build_compose_context(workspace):
        pass

    state_dir = tmp_path / "state" / "agent-circus"
    startup_files = list(state_dir.rglob("startup.sh"))
    assert len(startup_files) == 1
    assert "uv sync" in startup_files[0].read_text()
