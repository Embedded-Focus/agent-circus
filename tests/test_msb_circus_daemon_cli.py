"""Tests for the msb-circus daemon commands: daemons, daemon start/stop/restart/logs."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

import msb_circus.cli as cli_module
from msb_circus.cli import _format_columns, app
from msb_circus.config import sandbox_name_for_workspace

runner = CliRunner()


class FakeInvoker:
    """Records invocations; scripted per-subcommand behavior for daemons."""

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
        if args[0] == "exec" and "s6-svstat" in args[-1]:
            return SimpleNamespace(
                returncode=0,
                stdout=self.behavior.get("status_stdout", ""),
                stderr="",
            )
        if args[0] == "exec":
            return SimpleNamespace(
                returncode=self.behavior.get("exec_rc", 0), stdout="", stderr=""
            )
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")


@pytest.fixture
def fake_msb(tmp_path, monkeypatch):
    """Patch the CLI invoker; provide a workspace config with two daemons."""
    FakeInvoker.behavior = {}
    FakeInvoker.behavior["sandbox_name"] = sandbox_name_for_workspace(tmp_path)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(
        b"[[msb.daemons]]\n"
        b'name = "web"\n'
        b'command = ["python", "-m", "http.server", "4096"]\n'
        b"port = 4096\n\n"
        b"[[msb.daemons]]\n"
        b'name = "db"\n'
        b'command = ["sleep", "infinity"]\n'
        b"autostart = false\n\n"
        b"[[port_forwards]]\n"
        b"container_port = 4096\n"
        b"host_port = 14096\n"
    )
    monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: Path("/fake"))
    monkeypatch.setattr(cli_module, "check_version", lambda binary: "0.7.12")
    monkeypatch.setattr(cli_module, "MsbInvoker", FakeInvoker)
    monkeypatch.setattr(cli_module, "DAEMON_READY_TIMEOUT", 0)
    return FakeInvoker.behavior


def test_format_columns_pads_all_but_last_column():
    assert _format_columns([["A", "BB"], ["CCC", "D"]]) == "A    BB\nCCC  D"


def test_daemons_lists_configured_and_registered_daemons(tmp_path, fake_msb):
    fake_msb["status_stdout"] = "web 0 true true 2230 45\n"
    result = runner.invoke(app, ["daemons", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "NAME" in result.output
    assert "STATE" in result.output
    lines = {line.split()[0]: line for line in result.output.splitlines()[1:] if line}
    assert "running" in lines["web"]
    assert "2230" in lines["web"]
    assert "45s" in lines["web"]
    assert "4096" in lines["web"]
    assert "http://127.0.0.1:14096" in lines["web"]
    assert "not registered" in lines["db"]


def test_daemons_shows_unconfigured_service_as_not_in_config(tmp_path, fake_msb):
    fake_msb["status_stdout"] = "web 0 true true 2230 45\nold 0 false false -1 5\n"
    result = runner.invoke(app, ["daemons", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "stopped (not in config)" in result.output


def test_daemons_fails_when_sandbox_is_not_running(tmp_path, fake_msb):
    fake_msb["running"] = False
    result = runner.invoke(app, ["daemons", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "msb-circus up" in result.output


def test_daemon_start_runs_start_script_and_prints_url(tmp_path, fake_msb):
    result = runner.invoke(
        app, ["daemon", "start", "web", "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert "✓ Started daemon web" in result.output
    execs = [c for c in fake_msb["calls"] if c[0] == "exec"]
    assert any("s6-svc -u" in c[-1] for c in execs)


def test_daemon_stop_fails_for_unknown_daemon(tmp_path, fake_msb):
    result = runner.invoke(
        app, ["daemon", "stop", "nope", "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "no daemon named 'nope'" in result.output
    assert "configured: web, db" in result.output


def test_daemon_stop_fails_when_daemon_is_not_registered(tmp_path, fake_msb):
    fake_msb["exec_rc"] = 4
    result = runner.invoke(app, ["daemon", "stop", "web", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "not registered" in result.output


def test_daemon_logs_runs_tail_command(tmp_path, fake_msb):
    result = runner.invoke(
        app,
        ["daemon", "logs", "web", "-n", "20", "-f", "--workspace", str(tmp_path)],
    )
    assert result.exit_code == 0
    execs = [c for c in fake_msb["calls"] if c[0] == "exec"]
    assert execs[-1][-5:] == [
        "tail",
        "-n",
        "20",
        "-F",
        "/var/log/circus-daemons/web/current",
    ]
