"""Tests for the default-on git worktree mirror and its safeguards."""

import json
import logging
import subprocess
from pathlib import Path

import pytest

from msb_circus.cli import _warn_on_mount_drift
from msb_circus.config import (
    apply_worktree_mirror_default,
    effective_mounts,
    load_config,
    parse_config,
    resolve_agent_workdir,
)
from msb_circus.exceptions import ConfigurationError, MsbCommandError
from msb_circus.msb import MsbInvoker


class StubInvoker(MsbInvoker):
    """Returns a canned inspect response."""

    def __init__(self, stdout: str) -> None:
        super().__init__(binary=Path("/fake/msb"))
        self.stdout = stdout

    def run(
        self,
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, 0, self.stdout, "")


class FailingInvoker(MsbInvoker):
    """Raises like a failing msb invocation."""

    def __init__(self) -> None:
        super().__init__(binary=Path("/fake/msb"))

    def run(
        self,
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        raise MsbCommandError("msb inspect failed", returncode=1)


def test_parse_config_enables_mirror_by_default():
    config = parse_config({})
    assert config.git_worktree_mirror is True
    assert config.git_worktree_mirror_explicit is False


def test_parse_config_git_table_without_key_enables_mirror():
    config = parse_config({"git": {"config_path": "/x"}})
    assert config.git_worktree_mirror is True
    assert config.git_worktree_mirror_explicit is False


def test_parse_config_explicit_false_disables_mirror():
    config = parse_config({"git": {"worktree_mirror": False}})
    assert config.git_worktree_mirror is False
    assert config.git_worktree_mirror_explicit is True


def test_parse_config_explicit_true_is_explicit():
    config = parse_config({"git": {"worktree_mirror": True}})
    assert config.git_worktree_mirror is True
    assert config.git_worktree_mirror_explicit is True


def test_apply_default_turns_mirror_off_on_collision(caplog):
    config = parse_config({})
    with caplog.at_level(logging.INFO):
        result = apply_worktree_mirror_default(config, Path("/workspace"))
    assert result.git_worktree_mirror is False
    assert any(
        "Not mounting the git worktree mirror" in record.getMessage()
        for record in caplog.records
    )


def test_apply_default_turns_mirror_off_for_additional_dir_target(caplog):
    config = parse_config(
        {
            "additional_dirs": [{"path": "/srv/shared", "name": "x"}],
        }
    )
    with caplog.at_level(logging.INFO):
        result = apply_worktree_mirror_default(config, Path("/workspaces/x"))
    assert result.git_worktree_mirror is False
    assert any(
        "Not mounting the git worktree mirror" in record.getMessage()
        for record in caplog.records
    )


def test_apply_default_keeps_explicit_true_and_effective_mounts_raises(caplog):
    config = parse_config({"git": {"worktree_mirror": True}})
    with caplog.at_level(logging.INFO):
        result = apply_worktree_mirror_default(config, Path("/workspace"))
    assert result is config
    assert caplog.records == []
    with pytest.raises(ConfigurationError, match="already used as a mount target"):
        effective_mounts(config, Path("/workspace"))


def test_apply_default_keeps_mirror_without_collision():
    config = parse_config({})
    result = apply_worktree_mirror_default(config, Path("/home/u/repo"))
    assert result.git_worktree_mirror is True


def test_resolve_agent_workdir_uses_host_path_by_default():
    config = parse_config({})
    assert resolve_agent_workdir(config, Path("/home/u/repo")) == "/home/u/repo"


def test_load_config_applies_mirror_default(tmp_path):
    environ = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    (tmp_path / "xdg").mkdir()
    config = load_config(tmp_path, environ)
    assert config.git_worktree_mirror is True


def test_mount_drift_warning_mentions_new_default(tmp_path, caplog):
    config = parse_config({})
    invoker = StubInvoker(json.dumps({"config": {"unrelated": 1}}))
    with caplog.at_level(logging.WARNING):
        _warn_on_mount_drift(invoker, config, "agents-x", tmp_path)
    messages = [record.getMessage() for record in caplog.records]
    assert any("defaults to true" in message for message in messages)
    assert any("agents-x" in message for message in messages)


def test_mount_drift_warning_keeps_text_for_explicit_true(tmp_path, caplog):
    config = parse_config({"git": {"worktree_mirror": True}})
    invoker = StubInvoker(json.dumps({"config": {"unrelated": 1}}))
    with caplog.at_level(logging.WARNING):
        _warn_on_mount_drift(invoker, config, "agents-x", tmp_path)
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "worktree_mirror is enabled in the configuration" in message
        for message in messages
    )
    assert not any("defaults to true" in message for message in messages)
