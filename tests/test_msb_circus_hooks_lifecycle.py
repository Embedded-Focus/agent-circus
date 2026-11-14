"""Tests for project hooks during the msb-circus lifecycle commands."""

import contextlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest
from typer.testing import CliRunner

import msb_circus.cli as cli_module
from msb_circus.cli import app
from msb_circus.config import parse_config, sandbox_name_for_workspace
from msb_circus.exceptions import MsbCommandError
from msb_circus.msb import MsbInvoker

runner = CliRunner()


@contextlib.contextmanager
def capture_cli_debug_records():
    """Capture log records from the ``msb_circus.cli`` logger at DEBUG level.

    Attaches a handler directly to the CLI logger because
    ``setup_logging(force=True)`` replaces root handlers during the
    command run, which would lose a caplog-attached handler.

    :yields: List that collects the emitted :class:`logging.LogRecord`.
    """
    cli_logger = logging.getLogger("msb_circus.cli")
    records: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = CaptureHandler(level=logging.DEBUG)
    cli_logger.addHandler(handler)
    old_level = cli_logger.level
    cli_logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        cli_logger.removeHandler(handler)
        cli_logger.setLevel(old_level)


def log_messages(records: list[logging.LogRecord]) -> list[str]:
    """Return the messages of all captured records as strings.

    :param records: Records collected by :func:`capture_cli_debug_records`.
    :returns: Message texts of the records.
    """
    return [record.getMessage() for record in records]


class FakeInvoker:
    """Records invocations; scripted per-subcommand behavior."""

    behavior: ClassVar[dict] = {}

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        self.behavior.setdefault("calls", []).append(args)
        if args[0] == "exec" and any("provision new" in a for a in args):
            # hook check script
            self.behavior.setdefault("check_calls", []).append(args)
            return SimpleNamespace(
                returncode=self.behavior.get("check_rc", 0),
                stdout=self.behavior.get("check_stdout", ""),
                stderr="",
            )
        if args[0] == "exec" and any("flock 9" in a for a in args):
            # hook run script; args end with [..., "sh", <hook>, <mode>]
            hook = args[-2]
            self.behavior.setdefault("hook_runs", []).append((hook, args[-1], kwargs))
            rc = self.behavior.get("hook_rcs", {}).get(hook, 0)
            return SimpleNamespace(returncode=rc, stdout="", stderr="")
        if args[:2] == ["volume", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout=self.behavior.get("volume_list", "[]"),
                stderr="",
            )
        if args[0] == "run" and self.behavior.get("run_stderr"):
            raise MsbCommandError(
                "msb run failed", returncode=1, stderr=self.behavior["run_stderr"]
            )
        if args[:2] == ["image", "inspect"]:
            return SimpleNamespace(
                returncode=self.behavior.get("inspect_rc", 1),
                stdout="",
                stderr="",
            )
        if args[0] == "inspect":
            return SimpleNamespace(
                returncode=0,
                stdout=self.behavior.get("inspect_json", "{}"),
                stderr="",
            )
        if args[0] == "list":
            return SimpleNamespace(
                returncode=0,
                stdout=self.behavior.get("list_output", json.dumps([])),
                stderr="",
            )
        if args[0] == "exec" and args[-2:] == ["cat", "/proc/1/comm"]:
            return SimpleNamespace(
                returncode=0,
                stdout=self.behavior.get("pid1", "init.krun\n"),
                stderr="",
            )
        if args[0] == "exec" and args[-3:] == ["sh", "-c", "kill -TERM 1"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "exec" and args[-2:] == ["docker", "info"]:
            # dockerd probe: succeed only when enabled
            if not self.behavior.get("dockerd_ready", True):
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="permission denied"
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "exec" and "ssh-add" in args:
            # SSH agent probe
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "exec" and any("BEGIN msb-circus hosts" in a for a in args):
            # hosts block write
            return SimpleNamespace(
                returncode=self.behavior.get("hosts_rc", 0), stdout="", stderr=""
            )
        if args[0] == "exec" and "trust.json" in args[-1]:
            if self.behavior.get("trust_fail"):
                return SimpleNamespace(returncode=1, stdout="", stderr="denied")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "exec" and any("host-cas.pem" in a for a in args):
            return SimpleNamespace(
                returncode=0,
                stdout=self.behavior.get("host_cas_stdout", ""),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")


@pytest.fixture
def fake_msb(monkeypatch):
    """Patch the CLI invoker, config loading, and host subprocess calls."""
    FakeInvoker.behavior = {}
    host_calls: list[list[str]] = []

    def fake_host_run(args, **kwargs):
        host_calls.append(list(args))
        if "--output" in args:
            Path(args[args.index("--output") + 1]).touch()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: Path("/fake"))
    monkeypatch.setattr(cli_module, "check_version", lambda binary: "0.7.12")
    monkeypatch.setattr(cli_module, "MsbInvoker", FakeInvoker)
    monkeypatch.setattr(cli_module.subprocess, "run", fake_host_run)
    monkeypatch.setattr(cli_module, "DOCKERD_PROBE_ATTEMPTS", 1)
    monkeypatch.setattr(cli_module, "SSH_AGENT_PROBE_DELAY", 0)
    monkeypatch.setattr(cli_module, "STOP_POLL_INTERVAL", 0)
    FakeInvoker.behavior["host_calls"] = host_calls
    FakeInvoker.behavior["inspect_rc"] = 0  # image cached by default
    return FakeInvoker.behavior


def _write_config(tmp_path: Path, body: str) -> None:
    """Write a project config into ``tmp_path/.agent-circus/config.toml``.

    :param tmp_path: Workspace directory.
    :param body: TOML document text.
    """
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_text(body)


HOOKS_CONFIG = """\
[msb.hooks]
provision = "echo provision"
startup = "echo startup"

[[msb.daemons]]
name = "web"
command = ["sleep", "1"]
"""


def _hook_run_indexes(calls: list[list[str]]) -> list[int]:
    """Return the indexes of hook-run exec calls.

    :param calls: Recorded invoker calls.
    :returns: Indexes of exec calls that run a hook script.
    """
    return [
        i
        for i, c in enumerate(calls)
        if c[0] == "exec" and c[-2] in ("provision", "startup")
    ]


def _daemon_install_indexes(calls: list[list[str]]) -> list[int]:
    """Return the indexes of the daemon install exec calls.

    :param calls: Recorded invoker calls.
    :returns: Indexes of exec calls that install daemons via s6.
    """
    return [
        i for i, c in enumerate(calls) if c[0] == "exec" and "s6-svscanctl" in c[-1]
    ]


def test_up_new_sandbox_runs_provision_then_startup_before_daemons(tmp_path, fake_msb):
    _write_config(tmp_path, HOOKS_CONFIG)
    fake_msb["check_stdout"] = "provision new\nstartup due\n"
    with capture_cli_debug_records() as records:
        result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    hook_runs = fake_msb["hook_runs"]
    assert [(hook, mode) for hook, mode, _ in hook_runs] == [
        ("provision", "ensure"),
        ("startup", "ensure"),
    ]
    calls = fake_msb["calls"]
    installs = _daemon_install_indexes(calls)
    runs = _hook_run_indexes(calls)
    assert installs and max(runs) < installs[0]
    assert any("hook finished" in message for message in log_messages(records))


def test_up_without_hooks_makes_no_hook_exec_calls(tmp_path, fake_msb):
    _write_config(
        tmp_path,
        '[[msb.daemons]]\nname = "web"\ncommand = ["sleep", "1"]\n',
    )
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "check_calls" not in fake_msb
    assert "hook_runs" not in fake_msb
    assert not [
        c
        for c in fake_msb["calls"]
        if c[0] == "exec" and ("provision new" in c[-1] or "flock 9" in c[-1])
    ]
    assert _daemon_install_indexes(fake_msb["calls"])


def test_up_runs_only_due_hooks(tmp_path, fake_msb):
    _write_config(tmp_path, HOOKS_CONFIG)
    fake_msb["check_stdout"] = "startup due\n"
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert [(hook, mode) for hook, mode, _ in fake_msb["hook_runs"]] == [
        ("startup", "ensure")
    ]


def test_up_running_sandbox_runs_due_provision(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    _write_config(tmp_path, HOOKS_CONFIG)
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    fake_msb["check_stdout"] = "provision new\n"
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert not [c for c in fake_msb["calls"] if c[0] in ("run", "start")]
    assert [(hook, mode) for hook, mode, _ in fake_msb["hook_runs"]] == [
        ("provision", "ensure")
    ]


def test_start_runs_due_hooks(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    _write_config(tmp_path, HOOKS_CONFIG)
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Stopped"}])
    fake_msb["check_stdout"] = "provision new\nstartup due\n"
    result = runner.invoke(app, ["start", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert ["start", name] in fake_msb["calls"]
    assert [(hook, mode) for hook, mode, _ in fake_msb["hook_runs"]] == [
        ("provision", "ensure"),
        ("startup", "ensure"),
    ]


def test_exec_running_sandbox_warns_about_due_provision_without_running_it(
    tmp_path, fake_msb
):
    name = sandbox_name_for_workspace(tmp_path)
    _write_config(tmp_path, HOOKS_CONFIG)
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    fake_msb["check_stdout"] = "provision changed\n"
    with capture_cli_debug_records() as records:
        result = runner.invoke(app, ["exec", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert not [
        (hook, mode, _)
        for hook, mode, _ in fake_msb.get("hook_runs", [])
        if hook == "provision"
    ]
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert any("msb-circus up" in r.getMessage() for r in warnings)


def test_exec_running_sandbox_runs_due_startup(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    _write_config(tmp_path, HOOKS_CONFIG)
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    fake_msb["check_stdout"] = "startup due\n"
    result = runner.invoke(app, ["exec", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert [(hook, mode) for hook, mode, _ in fake_msb["hook_runs"]] == [
        ("startup", "ensure")
    ]


def test_exec_booting_sandbox_runs_provision(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    _write_config(tmp_path, HOOKS_CONFIG)
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Stopped"}])
    fake_msb["check_stdout"] = "provision new\n"
    result = runner.invoke(app, ["exec", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert [c for c in fake_msb["calls"] if c[0] == "start"]
    assert [(hook, mode) for hook, mode, _ in fake_msb["hook_runs"]] == [
        ("provision", "ensure")
    ]


def test_hooks_show_output_unless_quiet(tmp_path):
    config = parse_config({"msb": {"hooks": {"provision": "echo provision"}}})
    for quiet, expected in ((False, False), (True, True)):
        FakeInvoker.behavior = {"check_stdout": "provision new\n"}
        invoker = cast(MsbInvoker, FakeInvoker(Path("/fake")))
        ok = cli_module._ensure_hooks(
            invoker, config, "sbx", tmp_path, run_provision=True, quiet=quiet
        )
        assert ok
        ((hook, mode, kwargs),) = FakeInvoker.behavior["hook_runs"]
        assert (hook, mode) == ("provision", "ensure")
        assert kwargs["capture_output"] is expected


def test_required_provision_failure_skips_startup_and_daemons_and_exits_1(
    tmp_path, fake_msb
):
    _write_config(tmp_path, HOOKS_CONFIG)
    fake_msb["check_stdout"] = "provision new\nstartup due\n"
    fake_msb["hook_rcs"] = {"provision": 1}
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert [(hook, mode) for hook, mode, _ in fake_msb["hook_runs"]] == [
        ("provision", "ensure")
    ]
    assert not _daemon_install_indexes(fake_msb["calls"])


def test_optional_startup_failure_warns_and_registers_daemons(tmp_path, fake_msb):
    _write_config(tmp_path, HOOKS_CONFIG)
    fake_msb["check_stdout"] = "provision new\nstartup due\n"
    fake_msb["hook_rcs"] = {"startup": 2}
    with capture_cli_debug_records() as records:
        result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert [(hook, mode) for hook, mode, _ in fake_msb["hook_runs"]] == [
        ("provision", "ensure"),
        ("startup", "ensure"),
    ]
    assert _daemon_install_indexes(fake_msb["calls"])
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert any("startup hook failed" in r.getMessage() for r in warnings)


def test_optional_provision_failure_continues_with_startup(tmp_path, fake_msb):
    _write_config(
        tmp_path,
        "[msb.hooks.provision]\n"
        'script = "echo provision"\n'
        "required = false\n"
        "[msb.hooks.startup]\n"
        'script = "echo startup"\n',
    )
    fake_msb["check_stdout"] = "provision new\nstartup due\n"
    fake_msb["hook_rcs"] = {"provision": 1}
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert [(hook, mode) for hook, mode, _ in fake_msb["hook_runs"]] == [
        ("provision", "ensure"),
        ("startup", "ensure"),
    ]


def test_unset_passthrough_variable_fails_hook(tmp_path, fake_msb, monkeypatch):
    monkeypatch.delenv("SURELY_UNSET_VAR_XYZ", raising=False)
    _write_config(
        tmp_path,
        "[msb.hooks.provision]\n"
        'script = "echo provision"\n'
        "required = true\n"
        'env_passthrough = ["SURELY_UNSET_VAR_XYZ"]\n',
    )
    fake_msb["check_stdout"] = "provision new\n"
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "SURELY_UNSET_VAR_XYZ" in result.output
    assert "hook_runs" not in fake_msb


def test_missing_hook_file_fails_required_hook(tmp_path, fake_msb):
    _write_config(
        tmp_path,
        '[msb.hooks.provision]\nfile = "nope.sh"\n',
    )
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "nope.sh" in result.output
    assert "hook_runs" not in fake_msb


def test_hook_check_failure_warns_and_continues(tmp_path, fake_msb):
    _write_config(tmp_path, HOOKS_CONFIG)
    fake_msb["check_rc"] = 1
    with capture_cli_debug_records() as records:
        result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "hook_runs" not in fake_msb
    assert _daemon_install_indexes(fake_msb["calls"])
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert any("Could not check the hooks" in r.getMessage() for r in warnings)


def test_hook_run_passes_env_workdir_and_mode(tmp_path, fake_msb):
    _write_config(
        tmp_path,
        "[msb.hooks.provision]\n"
        'script = "echo provision"\n'
        'env = { FOO = "bar" }\n'
        'workdir = "/tmp"\n',
    )
    fake_msb["check_stdout"] = "provision new\n"
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    run_calls = [
        c
        for c in fake_msb["calls"]
        if c[0] == "exec" and c[-2:] == ["provision", "ensure"]
    ]
    assert len(run_calls) == 1
    args = run_calls[0]
    assert args[args.index("--env") + 1] == "FOO=bar"
    assert args[args.index("--workdir") + 1] == "/tmp"
    assert args[-2:] == ["provision", "ensure"]
