"""Tests for the msb-circus hook commands: hooks, hook run/logs."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

import msb_circus.cli as cli_module
from msb_circus.cli import app
from msb_circus.config import sandbox_name_for_workspace
from msb_circus.hooks import hook_key_hash

runner = CliRunner()


class FakeInvoker:
    """Records invocations; scripted per-subcommand behavior for hooks."""

    behavior: ClassVar[dict] = {}

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        self.behavior.setdefault("calls", []).append(args)
        if args[0] == "list":
            name = self.behavior.get("sandbox_name")
            running = self.behavior.get("running", True)
            payload = [{"name": name, "status": "Running"}] if running else []
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        if args[0] == "exec" and "boot=%s" in args[-1]:
            return SimpleNamespace(
                returncode=0,
                stdout=self.behavior.get("status_stdout", ""),
                stderr="",
            )
        if args[0] == "exec" and any("flock 9" in str(a) for a in args):
            return SimpleNamespace(
                returncode=self.behavior.get("run_rc", 0), stdout="", stderr=""
            )
        if args[0] == "exec":
            return SimpleNamespace(
                returncode=self.behavior.get("exec_rc", 0), stdout="", stderr=""
            )
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")


@pytest.fixture
def fake_msb(tmp_path, monkeypatch):
    """Patch the CLI invoker; provide a workspace config with both hooks."""
    FakeInvoker.behavior = {}
    FakeInvoker.behavior["sandbox_name"] = sandbox_name_for_workspace(tmp_path)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(
        b'[msb.hooks]\nprovision = "echo p"\nstartup = "echo s"\n'
    )
    monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: Path("/fake"))
    monkeypatch.setattr(cli_module, "check_version", lambda binary: "0.7.12")
    monkeypatch.setattr(cli_module, "MsbInvoker", FakeInvoker)
    return FakeInvoker.behavior


def test_hooks_lists_states(tmp_path, fake_msb):
    provision_key = hook_key_hash("echo p")
    fake_msb["status_stdout"] = (
        f"boot=b1\nprovision key={provision_key} exit=0 finished=1000\n"
        "startup key=b0 exit=3 finished=1001\n"
    )
    result = runner.invoke(app, ["hooks", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "HOOK" in result.output
    assert "STATE" in result.output
    lines = {line.split()[0]: line for line in result.output.splitlines()[1:] if line}
    assert "up to date" in lines["provision"]
    assert "due" in lines["startup"]
    assert "3" in lines["startup"]


def test_hooks_shows_never_run_and_not_configured(tmp_path, fake_msb):
    (tmp_path / ".agent-circus" / "config.toml").write_bytes(
        b'[msb.hooks]\nprovision = "echo p"\n'
    )
    fake_msb["status_stdout"] = "boot=b1\n"
    result = runner.invoke(app, ["hooks", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    lines = {line.split()[0]: line for line in result.output.splitlines()[1:] if line}
    assert "never run" in lines["provision"]
    assert "not configured" in lines["startup"]


def test_hooks_without_config_prints_hint(tmp_path, fake_msb):
    (tmp_path / ".agent-circus" / "config.toml").write_bytes(b"[msb.agent]\n")
    result = runner.invoke(app, ["hooks", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "No hooks configured" in result.output
    assert not fake_msb.get("calls")


def test_hooks_fails_when_sandbox_is_not_running(tmp_path, fake_msb):
    fake_msb["running"] = False
    result = runner.invoke(app, ["hooks", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "msb-circus up" in result.output


def test_hook_run_runs_hook_in_ensure_mode(tmp_path, fake_msb):
    result = runner.invoke(
        app, ["hook", "run", "provision", "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 0
    runs = [
        c
        for c in fake_msb["calls"]
        if c[0] == "exec" and any("flock 9" in str(a) for a in c)
    ]
    assert len(runs) == 1
    assert runs[0][-2:] == ["provision", "ensure"]


def test_hook_run_force_uses_force_mode(tmp_path, fake_msb):
    result = runner.invoke(
        app, ["hook", "run", "provision", "--force", "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 0
    runs = [
        c
        for c in fake_msb["calls"]
        if c[0] == "exec" and any("flock 9" in str(a) for a in c)
    ]
    assert runs[0][-2:] == ["provision", "force"]


def test_hook_run_exits_1_when_optional_hook_fails(tmp_path, fake_msb):
    fake_msb["run_rc"] = 2
    result = runner.invoke(
        app, ["hook", "run", "startup", "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 1


def test_hook_run_unknown_hook_fails(tmp_path, fake_msb):
    result = runner.invoke(app, ["hook", "run", "nope", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "unknown hook 'nope'" in result.output


def test_hook_run_unconfigured_hook_fails(tmp_path, fake_msb):
    (tmp_path / ".agent-circus" / "config.toml").write_bytes(
        b'[msb.hooks]\nprovision = "echo p"\n'
    )
    result = runner.invoke(
        app, ["hook", "run", "startup", "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "not configured" in result.output


def test_hook_logs_runs_tail_on_log(tmp_path, fake_msb):
    result = runner.invoke(
        app, ["hook", "logs", "startup", "-n", "5", "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 0
    execs = [c for c in fake_msb["calls"] if c[0] == "exec"]
    assert any("tail" in c and "/var/log/msb-circus/startup.log" in c for c in execs)
