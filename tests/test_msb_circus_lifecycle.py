"""Tests for the msb-circus lifecycle commands: up, stop, start, destroy."""

import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

import msb_circus.cli as cli_module
from msb_circus.cli import app
from msb_circus.config import sandbox_name_for_workspace
from msb_circus.exceptions import MsbCommandError

runner = CliRunner()


class FakeInvoker:
    """Records invocations; scripted per-subcommand behavior."""

    behavior: ClassVar[dict] = {}

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        self.behavior.setdefault("calls", []).append(args)
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
        if args[0] == "exec" and args[-2:] == ["docker", "info"]:
            # dockerd probe: succeed only when enabled
            if not self.behavior.get("dockerd_ready", True):
                return SimpleNamespace(
                    returncode=1, stdout="", stderr="permission denied"
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "exec" and "trust.json" in args[-1]:
            if self.behavior.get("trust_fail"):
                return SimpleNamespace(returncode=1, stdout="", stderr="denied")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "load" and self.behavior.get("load_fail"):
            raise MsbCommandError("msb load failed", returncode=1)
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
    FakeInvoker.behavior["host_calls"] = host_calls
    FakeInvoker.behavior["inspect_rc"] = 0  # image cached by default
    return FakeInvoker.behavior


def test_up_errors_when_image_missing_without_build(tmp_path, fake_msb):
    fake_msb["inspect_rc"] = 1
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "msb-circus build" in result.output


def test_up_boots_fresh_sandbox_with_documented_workflow(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    run_calls = [c for c in fake_msb["calls"] if c[0] == "run"]
    assert len(run_calls) == 1
    args = run_calls[0]
    assert args[args.index("--name") + 1] == name
    assert args[args.index("--memory") + 1] == "4G"
    assert args[args.index("--volume") + 1] == f"{tmp_path}:/workspace"
    assert args[args.index("--net") + 1] == "public,private"
    assert args[args.index("--workdir") + 1] == "/workspace"
    assert "--detach" in args
    assert args[-1] == "docker.io/library/msb-agents:26"
    assert "dockerd is ready" in result.output


def test_up_reuses_running_sandbox(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "already running" in result.output
    assert not [c for c in fake_msb["calls"] if c[0] in ("run", "start")]


def test_up_starts_stopped_sandbox(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Stopped"}])
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert ["start", name] in fake_msb["calls"]
    assert not [c for c in fake_msb["calls"] if c[0] == "run"]


def test_up_publishes_configured_port_forwards(tmp_path, fake_msb):
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(
        b"[[port_forwards]]\n"
        b"container_port = 8000\n\n"
        b"[[port_forwards]]\n"
        b'guest_port = 8080\nhost_port = 18080\nhost = "0.0.0.0"\nprotocol = "udp"\n'
    )
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    run_calls = [c for c in fake_msb["calls"] if c[0] == "run"]
    assert len(run_calls) == 1
    args = run_calls[0]
    assert args[args.index("--port") + 1] == "127.0.0.1:8000:8000"
    assert args[args.index("--port", args.index("--port") + 1) + 1] == (
        "0.0.0.0:18080:8080/udp"
    )


def test_up_fails_when_configured_port_is_already_bound(tmp_path, fake_msb):
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(
        b"[[port_forwards]]\ncontainer_port = 8000\n"
    )
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        host_port = blocker.getsockname()[1]
        (config_dir / "config.toml").write_bytes(
            f"[[port_forwards]]\ncontainer_port = 8000\nhost_port = {host_port}\n".encode()
        )
        result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
        assert result.exit_code == 1
        assert "already bound" in result.stderr
        assert not [c for c in fake_msb["calls"] if c[0] == "run"]
    finally:
        blocker.close()


def test_up_skips_port_check_when_reusing_running_sandbox(tmp_path, fake_msb):
    """A running sandbox holds its own forwarded ports; the probe must not fail."""
    name = sandbox_name_for_workspace(tmp_path)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        host_port = blocker.getsockname()[1]
        (config_dir / "config.toml").write_bytes(
            f"[[port_forwards]]\ncontainer_port = 8000\nhost_port = {host_port}\n".encode()
        )
        fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
        result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
        assert result.exit_code == 0
    finally:
        blocker.close()


def test_up_warns_on_port_drift_for_existing_sandbox(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(
        b"[[port_forwards]]\ncontainer_port = 8000\n"
    )
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    # The sandbox was created with a different mapping than configured now.
    fake_msb["inspect_json"] = json.dumps(
        {
            "config": {
                "network": {
                    "ports": [
                        {
                            "guest_port": 9000,
                            "host_bind": "127.0.0.1",
                            "host_port": 9000,
                            "protocol": "tcp",
                        }
                    ]
                }
            }
        }
    )
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "Port forwards" in result.output
    assert "differ" in result.output
    assert not [c for c in fake_msb["calls"] if c[0] == "run"]


def test_up_does_not_warn_when_port_forwards_match(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(
        b"[[port_forwards]]\ncontainer_port = 8000\n"
    )
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    fake_msb["inspect_json"] = json.dumps(
        {
            "config": {
                "network": {
                    "ports": [
                        {
                            "guest_port": 8000,
                            "host_bind": "127.0.0.1",
                            "host_port": 8000,
                            "protocol": "tcp",
                        }
                    ]
                }
            }
        }
    )
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "differ" not in result.output


def test_up_builds_when_requested(tmp_path, fake_msb):
    fake_msb["inspect_rc"] = 1  # force the build path
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path), "--build"])
    assert result.exit_code == 0
    # host build + save happened, then boot
    assert fake_msb["host_calls"][0][:2] == ["podman", "build"]
    assert fake_msb["host_calls"][1][:2] == ["podman", "save"]
    assert [c[0] for c in fake_msb["calls"] if c[0] == "run"] == ["run"]


def test_up_mounts_additional_dirs(tmp_path, fake_msb):
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (tmp_path / "shared-libs").mkdir()
    (tmp_path / "other-project").mkdir()
    (config_dir / "config.toml").write_bytes(
        b"[[additional_dirs]]\n"
        b'path = "'
        + str(tmp_path / "shared-libs").encode()
        + b'"\nreadonly = true\n\n[[additional_dirs]]\n'
        b'path = "' + str(tmp_path / "other-project").encode() + b'"\nname = "other"\n'
    )
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    run_call = next(c for c in fake_msb["calls"] if c[0] == "run")
    mount_dirs = [run_call[i + 1] for i, a in enumerate(run_call) if a == "--volume"]
    assert f"{tmp_path / 'shared-libs'}:/workspaces/shared-libs:ro" in mount_dirs
    assert f"{tmp_path / 'other-project'}:/workspaces/other" in mount_dirs


def test_up_fails_when_additional_dir_does_not_exist(tmp_path, fake_msb):
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(
        b'[[additional_dirs]]\npath = "' + str(tmp_path / "missing").encode() + b'"\n'
    )
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "does not exist on the host" in result.output
    assert not [c for c in FakeInvoker.behavior.get("calls", []) if c[0] == "run"]


def test_up_fails_when_additional_dir_is_a_file(tmp_path, fake_msb):
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (tmp_path / "afile").write_text("x")
    (config_dir / "config.toml").write_bytes(
        b'[[additional_dirs]]\npath = "' + str(tmp_path / "afile").encode() + b'"\n'
    )
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "exists but is not a directory" in result.output


def test_up_reports_mount_collision_as_clean_error(tmp_path, fake_msb):
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(
        b'[msb.mounts]\n"/srv/data" = "/workspaces/shared-libs"\n'
        b'[[additional_dirs]]\npath = "/home/user/shared-libs"\n'
    )
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "conflicts with the existing mount" in result.output
    assert not [c for c in FakeInvoker.behavior.get("calls", []) if c[0] == "run"]


def test_up_warns_when_dockerd_never_becomes_ready(tmp_path, fake_msb):
    fake_msb["dockerd_ready"] = False
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "dockerd did not become ready" in result.output
    assert "permission denied" in result.output  # last probe error is surfaced


def test_dockerd_probe_uses_plain_exec_without_no_tty(tmp_path, fake_msb):
    """The probe matches the manually verified invocation (no --no-tty)."""
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    probe_calls = [
        c for c in fake_msb["calls"] if c[0] == "exec" and c[-2:] == ["docker", "info"]
    ]
    assert probe_calls == [
        ["exec", sandbox_name_for_workspace(tmp_path), "--", "docker", "info"]
    ]


def test_stop_and_start_use_workspace_sandbox(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    assert runner.invoke(app, ["stop", "--workspace", str(tmp_path)]).exit_code == 0
    assert ["stop", name] in fake_msb["calls"]
    assert runner.invoke(app, ["start", "--workspace", str(tmp_path)]).exit_code == 0
    assert ["start", name] in fake_msb["calls"]


def test_destroy_forces_removal_with_confirmation(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(app, ["destroy", "--workspace", str(tmp_path)], input="y\n")
    assert result.exit_code == 0
    assert ["remove", "--force", name] in fake_msb["calls"]
    assert f"✓ Destroyed {name}" in result.output


def test_destroy_aborts_without_confirmation(tmp_path, fake_msb):
    result = runner.invoke(app, ["destroy", "--workspace", str(tmp_path)], input="n\n")
    assert result.exit_code == 0
    assert "Aborted." in result.output
    assert "calls" not in fake_msb or not [
        c for c in fake_msb["calls"] if c[0] == "remove"
    ]


def test_destroy_force_skips_prompt(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(app, ["destroy", "-f", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert ["remove", "--force", name] in fake_msb["calls"]


def test_up_writes_pi_trust_for_workdir_and_additional_dirs(tmp_path, fake_msb):
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (tmp_path / "shared-libs").mkdir()
    (config_dir / "config.toml").write_bytes(
        b"[[additional_dirs]]\n"
        b'path = "' + str(tmp_path / "shared-libs").encode() + b'"\n'
    )
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    trust_calls = [
        c for c in fake_msb["calls"] if c[0] == "exec" and "trust.json" in c[-1]
    ]
    assert len(trust_calls) == 1
    script = trust_calls[0][-1]
    assert '{"/workspace": true, "/workspaces/shared-libs": true}' in script


def test_up_writes_pi_trust_when_reusing_running_sandbox(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert [c for c in fake_msb["calls"] if c[0] == "exec" and "trust.json" in c[-1]]


def test_up_survives_pi_trust_write_failure(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    fake_msb["trust_fail"] = True
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "pre-trust" in result.output
