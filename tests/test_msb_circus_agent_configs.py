"""Tests for agent harness config handling in msb-circus.

Covers the host config-dir bind mounts (claude-code, codex), the one-way
config file copies (claude-code, pi, opencode), the Claude Code folder
trust merge, and the corresponding mount-drift warning.
"""

import json
import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest
from typer.testing import CliRunner

import msb_circus.cli as cli_module
from msb_circus.cli import (
    _ensure_agent_config_copies,
    _ensure_claude_trust,
    _warn_on_agent_config_mount_drift,
    app,
)
from msb_circus.config import AdditionalDir, MsbConfig, resolve_agent_config_mounts
from msb_circus.exceptions import MsbCommandError
from msb_circus.exec_ import build_write_claude_trust_args
from msb_circus.msb import MsbInvoker
from msb_circus.sandbox import build_copy_args


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``$HOME`` at a fresh directory so host config lookups are clean.

    :param tmp_path: Per-test temporary directory.
    :param monkeypatch: Pytest environment patch helper.
    :returns: The fake home directory.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


class RecordingInvoker:
    """Invoker double: records argv, scripts ``copy`` failures and inspect."""

    def __init__(self, *, fail_copy: bool = False, inspect_output: str = "") -> None:
        self.calls: list[list[str]] = []
        self.fail_copy = fail_copy
        self.inspect_output = inspect_output

    def run(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == "copy" and self.fail_copy:
            raise MsbCommandError("msb copy failed", returncode=1)
        return SimpleNamespace(returncode=0, stdout=self.inspect_output, stderr="")


def test_build_copy_args_targets_guest_by_name():
    args = build_copy_args("agents-demo", "/host/file.json", "/root/file.json")
    assert args == ["copy", "-q", "/host/file.json", "agents-demo:/root/file.json"]


def test_resolve_agent_config_mounts_skips_missing_dir_with_warning(
    isolated_home, caplog
):
    with caplog.at_level(logging.WARNING):
        mounts = resolve_agent_config_mounts()
    assert mounts == []
    assert "skipping" in caplog.text
    assert "claude-code" in caplog.text


def test_resolve_agent_config_mounts_includes_existing_dirs_only(isolated_home, caplog):
    (isolated_home / ".codex").mkdir()
    (isolated_home / ".claude").mkdir()
    with caplog.at_level(logging.WARNING):
        mounts = resolve_agent_config_mounts()
    assert (str(isolated_home / ".claude"), "/root/.claude") in mounts
    assert (str(isolated_home / ".codex"), "/root/.codex") in mounts
    assert "skipping" not in caplog.text


def test_ensure_agent_config_copies_copies_existing_files(isolated_home):
    claude_json = isolated_home / ".claude.json"
    claude_json.write_text("{}")
    settings = isolated_home / ".pi" / "agent" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{}")
    opencode = isolated_home / ".config" / "opencode" / "opencode.json"
    opencode.parent.mkdir(parents=True)
    opencode.write_text("{}")

    invoker = RecordingInvoker()
    _ensure_agent_config_copies(cast(MsbInvoker, invoker), "agents-demo")

    destinations = [call[3] for call in invoker.calls if call[0] == "copy"]
    assert destinations == [
        "agents-demo:/root/.claude.json",
        "agents-demo:/root/.pi/agent/settings.json",
        "agents-demo:/root/.config/opencode/opencode.json",
    ]


def test_ensure_agent_config_copies_skips_missing_host_files(isolated_home):
    invoker = RecordingInvoker()
    _ensure_agent_config_copies(cast(MsbInvoker, invoker), "agents-demo")
    assert invoker.calls == []


def test_ensure_agent_config_copies_warns_and_survives_failure(isolated_home, caplog):
    settings = isolated_home / ".pi" / "agent" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{}")

    invoker = RecordingInvoker(fail_copy=True)
    with caplog.at_level(logging.WARNING):
        _ensure_agent_config_copies(cast(MsbInvoker, invoker), "agents-demo")

    assert invoker.calls  # the copy was attempted
    assert "Could not copy the pi config file" in caplog.text


def _inspect_output(binds: list[dict]) -> str:
    """Build a minimal ``msb inspect --format json`` payload.

    :param binds: Bind-mount entries (``type``/``host``/``guest``).
    :returns: JSON string accepted by ``parse_inspect_bind_mounts``.
    """
    return json.dumps({"config": {"mounts": binds}})


def test_warn_on_agent_config_mount_drift_warns_when_mount_missing(
    isolated_home, caplog
):
    (isolated_home / ".codex").mkdir()
    invoker = RecordingInvoker(inspect_output=_inspect_output([]))

    with caplog.at_level(logging.WARNING):
        _warn_on_agent_config_mount_drift(cast(MsbInvoker, invoker), "agents-demo")

    assert "Agent config directory mounts" in caplog.text


def test_warn_on_agent_config_mount_drift_passes_when_mounted(isolated_home, caplog):
    home = isolated_home
    (home / ".codex").mkdir()
    invoker = RecordingInvoker(
        inspect_output=_inspect_output(
            [{"type": "Bind", "host": str(home / ".codex"), "guest": "/root/.codex"}]
        )
    )

    with caplog.at_level(logging.WARNING):
        _warn_on_agent_config_mount_drift(cast(MsbInvoker, invoker), "agents-demo")

    assert "Agent config directory mounts" not in caplog.text


def test_warn_on_agent_config_mount_drift_silent_without_host_dirs(
    isolated_home, caplog
):
    """No host config dirs: nothing expected, no drift warning, no inspect."""
    invoker = RecordingInvoker()
    with caplog.at_level(logging.WARNING):
        _warn_on_agent_config_mount_drift(cast(MsbInvoker, invoker), "agents-demo")
    assert invoker.calls == []
    assert "Agent config directory mounts" not in caplog.text


class LifecycleFakeInvoker:
    """Invoker double for ``up``: records argv, stubs every subcommand."""

    behavior: ClassVar[dict] = {}

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        self.behavior.setdefault("calls", []).append(args)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")


@pytest.fixture
def fake_msb(monkeypatch: pytest.MonkeyPatch):
    """Patch the CLI invoker and version probe for ``up`` runs."""
    LifecycleFakeInvoker.behavior = {"calls": []}
    monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: Path("/fake"))
    monkeypatch.setattr(cli_module, "check_version", lambda binary: "0.7.12")
    monkeypatch.setattr(cli_module, "MsbInvoker", LifecycleFakeInvoker)
    monkeypatch.setattr(cli_module, "DOCKERD_PROBE_ATTEMPTS", 1)
    return LifecycleFakeInvoker.behavior


runner = CliRunner()


def test_up_boots_with_agent_config_dir_mounts(isolated_home, fake_msb):
    """Existing harness config dirs are added as rw bind mounts at boot."""
    (isolated_home / ".codex").mkdir()
    workspace = isolated_home / "project"
    workspace.mkdir()

    result = runner.invoke(app, ["up", "--workspace", str(workspace)])

    assert result.exit_code == 0
    run_args = next(c for c in fake_msb["calls"] if c[0] == "run")
    volumes = [run_args[i + 1] for i, arg in enumerate(run_args) if arg == "--volume"]
    specs = [tuple(v.split(":", 1)) for v in volumes]
    assert (str(workspace), "/workspace") in specs
    assert (str(isolated_home / ".codex"), "/root/.codex") in specs


def _run_claude_trust_script(
    home: Path, dirs: list[str]
) -> subprocess.CompletedProcess[str]:
    """Run the generated trust script with Node.js against a fake ``$HOME``."""
    args = build_write_claude_trust_args("agents-demo", dirs)
    assert args[:6] == ["exec", "agents-demo", "--", "node", "-e", args[5]]
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin"}
    return subprocess.run(
        ["node", "-e", args[5]], env=env, capture_output=True, text=True, check=False
    )


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")


@needs_node
def test_claude_trust_script_creates_missing_state_file(tmp_path):
    result = _run_claude_trust_script(tmp_path, ["/workspace", "/workspaces/lib"])

    assert result.returncode == 0, result.stderr
    state_file = tmp_path / ".claude.json"
    assert json.loads(state_file.read_text()) == {
        "projects": {
            "/workspace": {"hasTrustDialogAccepted": True},
            "/workspaces/lib": {"hasTrustDialogAccepted": True},
        }
    }
    assert state_file.stat().st_mode & 0o777 == 0o600


@needs_node
def test_claude_trust_script_preserves_existing_state(tmp_path):
    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "projects": {
                    "/host/repo": {},
                    "/workspace": {"allowedTools": ["Bash"]},
                },
            }
        )
    )

    result = _run_claude_trust_script(tmp_path, ["/workspace", "/tmp/a'b\"c $HOME"])

    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / ".claude.json").read_text()) == {
        "hasCompletedOnboarding": True,
        "projects": {
            "/host/repo": {},
            "/workspace": {"allowedTools": ["Bash"], "hasTrustDialogAccepted": True},
            "/tmp/a'b\"c $HOME": {"hasTrustDialogAccepted": True},
        },
    }


@needs_node
def test_claude_trust_script_leaves_invalid_state_untouched(tmp_path):
    (tmp_path / ".claude.json").write_text("not json")

    result = _run_claude_trust_script(tmp_path, ["/workspace"])

    assert result.returncode != 0
    assert (tmp_path / ".claude.json").read_text() == "not json"


def test_build_write_claude_trust_args_rejects_empty_dirs():
    with pytest.raises(ValueError, match="at least one directory"):
        build_write_claude_trust_args("agents-demo", [])


def _config_with_additional_dir() -> MsbConfig:
    return MsbConfig(additional_dirs=(AdditionalDir(path="/host/lib", name="lib"),))


def test_ensure_claude_trust_trusts_workdir_and_additional_dirs():
    invoker = RecordingInvoker()

    _ensure_claude_trust(
        cast(MsbInvoker, invoker), _config_with_additional_dir(), "agents-demo"
    )

    assert invoker.calls == [
        build_write_claude_trust_args("agents-demo", ["/workspace", "/workspaces/lib"])
    ]


def test_ensure_claude_trust_warns_on_failure(caplog):
    class FailingInvoker:
        def run(self, args, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="node: not found")

    with caplog.at_level(logging.WARNING):
        _ensure_claude_trust(
            cast(MsbInvoker, FailingInvoker()), MsbConfig(), "agents-demo"
        )

    assert "Could not pre-trust" in caplog.text
    assert "node: not found" in caplog.text
