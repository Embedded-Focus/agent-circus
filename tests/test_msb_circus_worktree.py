"""Tests for the git worktree mirror: config, mounts, parsing, and drift."""

import json
import logging
import subprocess
from pathlib import Path

import pytest

from msb_circus.cli import _warn_on_mount_drift
from msb_circus.config import effective_mounts, parse_config, resolve_agent_workdir
from msb_circus.exceptions import ConfigurationError, MsbCommandError
from msb_circus.msb import MsbInvoker
from msb_circus.sandbox import parse_inspect_bind_mounts


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


def test_parse_config_defaults_worktree_mirror_to_true():
    config = parse_config({})
    assert config.git_worktree_mirror is True
    assert config.git_worktree_mirror_explicit is False


def test_parse_config_reads_worktree_mirror():
    assert parse_config({"git": {"worktree_mirror": True}}).git_worktree_mirror
    assert not parse_config({"git": {"worktree_mirror": False}}).git_worktree_mirror


def test_parse_config_rejects_non_bool_worktree_mirror():
    with pytest.raises(
        ConfigurationError, match=r"git\.worktree_mirror must be a boolean"
    ):
        parse_config({"git": {"worktree_mirror": "yes"}})


def test_parse_config_logs_unsupported_git_subkeys_at_debug(caplog):
    with caplog.at_level(logging.DEBUG):
        parse_config({"git": {"worktree_mirror": True, "foo": 1}})
    messages = [record.getMessage() for record in caplog.records]
    assert any("git.foo" in message for message in messages)
    assert all(record.levelno == logging.DEBUG for record in caplog.records)


def test_parse_config_does_not_report_config_path_as_unsupported(caplog):
    with caplog.at_level(logging.DEBUG):
        parse_config({"git": {"worktree_mirror": True, "config_path": "~/.gitconfig"}})
    messages = [record.getMessage() for record in caplog.records]
    assert not any("git.config_path" in message for message in messages)


def test_effective_mounts_adds_worktree_mirror(tmp_path):
    config = parse_config({"git": {"worktree_mirror": True}})
    mounts = effective_mounts(config, tmp_path)
    # The workspace is mounted at /workspace and a second time at its
    # exact host path, so the same host path appears in two pairs.
    assert mounts == [(str(tmp_path), "/workspace"), (str(tmp_path), str(tmp_path))]


def test_effective_mounts_without_mirror_is_unchanged(tmp_path):
    config = parse_config({"git": {"worktree_mirror": False}})
    mounts = effective_mounts(config, tmp_path)
    assert mounts == [(str(tmp_path), "/workspace")]


def test_effective_mounts_rejects_mirror_target_collision(tmp_path):
    config = parse_config(
        {
            "msb": {"mounts": {"/srv/data": str(tmp_path)}},
            "git": {"worktree_mirror": True},
        }
    )
    with pytest.raises(ConfigurationError, match="already used as a mount target"):
        effective_mounts(config, tmp_path)


def test_parse_inspect_bind_mounts_extracts_binds():
    output = json.dumps(
        {
            "config": {
                "volumes": [
                    {"type": "Bind", "host": "/home/u/proj", "guest": "/home/u/proj"},
                    {"type": "Named", "name": "cache", "guest": "/cache"},
                    {"type": "Bind", "host": 1, "guest": "/broken"},
                ]
            }
        }
    )
    assert parse_inspect_bind_mounts(output) == [("/home/u/proj", "/home/u/proj")]


def test_parse_inspect_bind_mounts_falls_back_to_active_config():
    document = json.loads(
        json.dumps(
            {"config": {"volumes": [{"type": "Bind", "host": "/h", "guest": "/g"}]}}
        )
    )
    output = json.dumps({"active_config": document["config"]})
    assert parse_inspect_bind_mounts(output) == [("/h", "/g")]


def test_parse_inspect_bind_mounts_rejects_invalid_json():
    with pytest.raises(ConfigurationError, match="Could not parse"):
        parse_inspect_bind_mounts("not json")


def test_warn_on_mount_drift_warns_when_mirror_mount_missing(tmp_path, caplog):
    config = parse_config({"git": {"worktree_mirror": True}})
    invoker = StubInvoker(json.dumps({"config": {"unrelated": 1}}))
    with caplog.at_level(logging.WARNING):
        _warn_on_mount_drift(invoker, config, "agents-x", tmp_path)
    assert any("worktree_mirror" in record.getMessage() for record in caplog.records)
    assert any("agents-x" in record.getMessage() for record in caplog.records)


def test_warn_on_mount_drift_silent_when_mirror_mount_present(tmp_path, caplog):
    config = parse_config({"git": {"worktree_mirror": True}})
    invoker = StubInvoker(
        json.dumps(
            {
                "config": {
                    "volumes": [
                        {
                            "type": "Bind",
                            "host": str(tmp_path),
                            "guest": str(tmp_path),
                        }
                    ]
                }
            }
        )
    )
    with caplog.at_level(logging.WARNING):
        _warn_on_mount_drift(invoker, config, "agents-x", tmp_path)
    assert caplog.records == []


def test_warn_on_mount_drift_noop_when_disabled(tmp_path, caplog):
    config = parse_config({"git": {"worktree_mirror": False}})
    invoker = StubInvoker(json.dumps({"config": {}}))
    with caplog.at_level(logging.WARNING):
        _warn_on_mount_drift(invoker, config, "agents-x", tmp_path)
    assert caplog.records == []


def test_warn_on_mount_drift_skips_on_inspect_failure(tmp_path, caplog):
    config = parse_config({"git": {"worktree_mirror": True}})
    with caplog.at_level(logging.WARNING):
        _warn_on_mount_drift(FailingInvoker(), config, "agents-x", tmp_path)
    assert caplog.records == []


def test_resolve_agent_workdir_defaults_to_workspace_mount(tmp_path):
    config = parse_config({"git": {"worktree_mirror": False}})
    assert resolve_agent_workdir(config, tmp_path) == "/workspace"


def test_resolve_agent_workdir_uses_host_path_with_mirror(tmp_path):
    config = parse_config({"git": {"worktree_mirror": True}})
    assert resolve_agent_workdir(config, tmp_path) == str(tmp_path)


def test_resolve_agent_workdir_prefers_explicit_workdir(tmp_path):
    config = parse_config(
        {"git": {"worktree_mirror": True}, "msb": {"agent": {"workdir": "/w"}}}
    )
    assert resolve_agent_workdir(config, tmp_path) == "/w"


def test_parse_config_rejects_empty_agent_workdir():
    with pytest.raises(ConfigurationError, match="workdir"):
        parse_config({"msb": {"agent": {"workdir": ""}}})
