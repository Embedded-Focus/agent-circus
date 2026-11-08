"""Tests for the msb-circus lifecycle commands: up, stop, start, destroy."""

import json
import socket
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

import msb_circus.cli as cli_module
from msb_circus.cli import app
from msb_circus.config import sandbox_name_for_workspace
from msb_circus.exceptions import MsbCommandError
from msb_circus.gitconfig import (
    GIT_GUEST_DIR,
    GIT_GUEST_HOST_CONFIG,
    GIT_GUEST_MANAGED_CONFIG,
    GIT_GUEST_SIGNING_KEY,
)
from msb_circus.ssh_agent import agent_link_path

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
        if args[0] == "exec" and args[-2:] == ["cat", "/proc/1/comm"]:
            return SimpleNamespace(
                returncode=0,
                stdout=self.behavior.get("pid1", "init.krun\n"),
                stderr="",
            )
        if args[0] == "exec" and args[-3:] == ["sh", "-c", "kill -TERM 1"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "list":
            list_outputs = self.behavior.get("list_outputs")
            if list_outputs:
                if len(list_outputs) > 1:
                    return SimpleNamespace(
                        returncode=0, stdout=list_outputs.pop(0), stderr=""
                    )
                return SimpleNamespace(returncode=0, stdout=list_outputs[0], stderr="")
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
        if args[0] == "exec" and "ssh-add" in args:
            # SSH agent probe: exit code scripted via ssh_add_rcs (one per
            # call, last one repeats) or ssh_add_rc
            rcs = self.behavior.get("ssh_add_rcs")
            rc = rcs.pop(0) if rcs and len(rcs) > 1 else (rcs or [None])[0]
            if rc is None:
                rc = self.behavior.get("ssh_add_rc", 0)
            return SimpleNamespace(returncode=rc, stdout="", stderr="")
        if args[0] == "exec" and any("BEGIN msb-circus hosts" in a for a in args):
            # hosts block write: exit code scripted via hosts_rc
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
        if args[0] == "exec" and any(GIT_GUEST_MANAGED_CONFIG in a for a in args):
            # managed git config write: exit code scripted via gitconfig_rc
            return SimpleNamespace(
                returncode=self.behavior.get("gitconfig_rc", 0),
                stdout="",
                stderr="",
            )
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
    monkeypatch.setattr(cli_module, "SSH_AGENT_PROBE_DELAY", 0)
    monkeypatch.setattr(cli_module, "STOP_POLL_INTERVAL", 0)
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
    _enable_docker(tmp_path)
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
    assert args[args.index("--mount-owned") + 1] == (
        "/var/lib/docker:kind=disk,size=10G"
    )
    assert args[args.index("--init") + 1] == "auto"
    assert args[args.index("--env") + 1] == "CIRCUS_DOCKERD_SHUTDOWN_TIMEOUT=20"
    assert "--detach" in args
    assert args[-1] == "docker.io/library/msb-agents:26"
    assert "dockerd is ready" in result.output


def _enable_docker(tmp_path: Path, size: str = "10G") -> None:
    """Write a config that opts into docker via a dedicated disk."""
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_bytes(
        f'[msb.sandbox]\ndocker_disk = "{size}"\n'.encode()
    )


def test_up_skips_dockerd_probe_when_docker_is_disabled(tmp_path, fake_msb):
    """No disk config means no usable docker: no probe, but a visible warning."""
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    probe_calls = [
        c for c in fake_msb["calls"] if c[0] == "exec" and c[-2:] == ["docker", "info"]
    ]
    assert probe_calls == []
    assert "dockerd is ready" not in result.output
    assert "docker is not enabled" in result.output
    assert "docker_disk" in result.output  # the fix is actionable


def test_up_warns_docker_disabled_when_reusing_running_sandbox(tmp_path, fake_msb):
    """The reuse path must surface docker unavailability, not silently skip."""
    name = sandbox_name_for_workspace(tmp_path)
    fake_msb["list_output"] = json.dumps(
        [{"name": name, "status": "Running", "id": "abc"}]
    )
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert not [c for c in fake_msb["calls"] if c[0] == "run"]
    assert "docker is not enabled" in result.output


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


def test_up_passes_trust_host_cas_when_configured(tmp_path, fake_msb):
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(b"[msb.network]\ntrust_host_cas = true\n")
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    run_calls = [c for c in fake_msb["calls"] if c[0] == "run"]
    assert len(run_calls) == 1
    assert "--trust-host-cas" in run_calls[0]


def test_up_warns_on_trust_host_cas_drift_when_enabled_but_absent(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(b"[msb.network]\ntrust_host_cas = true\n")
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    fake_msb["host_cas_stdout"] = "absent\n"
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "trust_host_cas" in result.output
    assert "re-create" in result.output
    assert not [c for c in fake_msb["calls"] if c[0] == "run"]


def test_up_warns_on_trust_host_cas_drift_when_disabled_but_present(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    fake_msb["host_cas_stdout"] = "present\n"
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "trust_host_cas" in result.output
    assert "re-create" in result.output


def test_up_does_not_warn_when_trust_host_cas_matches(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(b"[msb.network]\ntrust_host_cas = true\n")
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    fake_msb["host_cas_stdout"] = "present\n"
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "trust_host_cas" not in result.output


def test_up_ignores_trust_host_cas_probe_without_answer(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(b"[msb.network]\ntrust_host_cas = true\n")
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "trust_host_cas" not in result.output


def test_start_warns_on_trust_host_cas_drift(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    fake_msb["host_cas_stdout"] = "present\n"
    result = runner.invoke(app, ["start", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert ["start", name] in fake_msb["calls"]
    assert "trust_host_cas" in result.output
    assert "re-create" in result.output


def _enable_git(tmp_path: Path, extra: str = "") -> str:
    """Write a config with a [git] table; return the fake host HOME path."""
    home = tmp_path / "git-home"
    home.mkdir(exist_ok=True)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_bytes(f"[git]\n{extra}".encode())
    return str(home)


def test_up_forwards_git_config_when_git_table_present(tmp_path, fake_msb, monkeypatch):
    home = _enable_git(tmp_path)
    monkeypatch.setenv("HOME", home)
    host_gitconfig = Path(home) / ".gitconfig"
    host_gitconfig.write_text("[user]\n\temail = host@example.com\n")
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    writes = [
        c
        for c in fake_msb["calls"]
        if c[0] == "exec" and any(GIT_GUEST_DIR in a for a in c)
    ]
    assert writes, "expected the managed gitconfig exec"
    copies = [
        c
        for c in fake_msb["calls"]
        if c[0] == "copy" and c[3] == f"{name}:{GIT_GUEST_HOST_CONFIG}"
    ]
    assert copies == [
        ["copy", "-q", str(host_gitconfig), f"{name}:{GIT_GUEST_HOST_CONFIG}"]
    ]


def test_up_skips_git_forwarding_without_git_table(tmp_path, fake_msb, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert not [c for c in fake_msb["calls"] if any(GIT_GUEST_DIR in a for a in c)]


def test_up_warns_when_explicit_git_config_missing(tmp_path, fake_msb, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _enable_git(tmp_path, 'config_path = "/nonexistent/gitconfig"\n')
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "not found on the host" in result.output
    name = sandbox_name_for_workspace(tmp_path)
    assert not [
        c
        for c in fake_msb["calls"]
        if c[0] == "copy" and c[-1] == f"{name}:{GIT_GUEST_HOST_CONFIG}"
    ]


def test_up_copies_valid_signing_key(tmp_path, fake_msb, monkeypatch, ssh_environ):
    home = _enable_git(
        tmp_path,
        'signing_key_path = "~/.ssh/id_ed25519.pub"\n[ssh]\n',
    )
    monkeypatch.setenv("HOME", home)
    key = Path(home) / ".ssh" / "id_ed25519.pub"
    key.parent.mkdir()
    key.write_text("ssh-ed25519 AAAAExample user@host\n")
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    copies = [
        c
        for c in fake_msb["calls"]
        if c[0] == "copy" and c[3] == f"{name}:{GIT_GUEST_SIGNING_KEY}"
    ]
    assert copies == [["copy", "-q", str(key), f"{name}:{GIT_GUEST_SIGNING_KEY}"]]


def test_up_refuses_private_signing_key(tmp_path, fake_msb, monkeypatch, ssh_environ):
    home = _enable_git(
        tmp_path,
        'signing_key_path = "~/.ssh/id_ed25519"\n[ssh]\n',
    )
    monkeypatch.setenv("HOME", home)
    (Path(home) / ".gitconfig").write_text("[user]\n\temail = host@example.com\n")
    key = Path(home) / ".ssh" / "id_ed25519"
    key.parent.mkdir()
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END-----\n")
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "signing key" in result.output
    assert not [
        c
        for c in fake_msb["calls"]
        if c[0] == "copy" and c[3] == f"{name}:{GIT_GUEST_SIGNING_KEY}"
    ]
    writes = [
        c
        for c in fake_msb["calls"]
        if c[0] == "exec" and any(GIT_GUEST_DIR in a for a in c)
    ]
    assert writes
    assert "signingkey" not in writes[0][-4]


def test_up_warns_when_signing_key_without_ssh(tmp_path, fake_msb, monkeypatch):
    home = _enable_git(tmp_path, 'signing_key_path = "~/.ssh/id_ed25519.pub"\n')
    monkeypatch.setenv("HOME", home)
    key = Path(home) / ".ssh" / "id_ed25519.pub"
    key.parent.mkdir()
    key.write_text("ssh-ed25519 AAAAExample user@host\n")
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "[ssh] is not configured" in result.output


def test_up_continues_when_gitconfig_script_fails(tmp_path, fake_msb, monkeypatch):
    home = _enable_git(tmp_path)
    monkeypatch.setenv("HOME", home)
    (Path(home) / ".gitconfig").write_text("[user]\n\temail = host@example.com\n")
    fake_msb["gitconfig_rc"] = 1
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "Could not write the git config" in result.output
    assert not [
        c
        for c in fake_msb["calls"]
        if c[0] == "copy" and c[-1] == f"{name}:{GIT_GUEST_HOST_CONFIG}"
    ]


def test_start_refreshes_git_config(tmp_path, fake_msb, monkeypatch):
    home = _enable_git(tmp_path)
    monkeypatch.setenv("HOME", home)
    host_gitconfig = Path(home) / ".gitconfig"
    host_gitconfig.write_text("[user]\n\temail = host@example.com\n")
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(app, ["start", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert ["start", name] in fake_msb["calls"]
    writes = [
        c
        for c in fake_msb["calls"]
        if c[0] == "exec" and any(GIT_GUEST_DIR in a for a in c)
    ]
    assert len(writes) == 1
    copies = [
        c
        for c in fake_msb["calls"]
        if c[0] == "copy" and c[3] == f"{name}:{GIT_GUEST_HOST_CONFIG}"
    ]
    assert copies == [
        ["copy", "-q", str(host_gitconfig), f"{name}:{GIT_GUEST_HOST_CONFIG}"]
    ]


def test_up_copies_symlinked_gitconfig_target(tmp_path, fake_msb, monkeypatch):
    home = _enable_git(tmp_path)
    monkeypatch.setenv("HOME", home)
    target = Path(home) / "store" / "gitconfig"
    target.parent.mkdir(parents=True)
    target.write_text("[user]\n\temail = host@example.com\n")
    (Path(home) / ".gitconfig").symlink_to(target)
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    copies = [
        c
        for c in fake_msb["calls"]
        if c[0] == "copy" and c[3] == f"{name}:{GIT_GUEST_HOST_CONFIG}"
    ]
    assert copies == [
        ["copy", "-q", str(target.resolve()), f"{name}:{GIT_GUEST_HOST_CONFIG}"]
    ]


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


DISK_INSPECT_MATCHING = json.dumps(
    {
        "config": {
            "spec": {
                "root_disk": {"kind": "managed", "size_mib": 4096},
                "mounts": [
                    {
                        "type": "Owned",
                        "guest": "/var/lib/docker",
                        "storage": {"kind": "disk", "capacity_mib": 10240},
                        "options": {"readonly": False, "noexec": False},
                    }
                ],
            }
        }
    }
)


def test_up_warns_on_disk_drift_for_existing_sandbox(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    _enable_docker(tmp_path, size="20G")
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    # The sandbox was created with a 10G docker disk; the config now asks
    # for 20G. Disks are creation-time only, so this must warn.
    fake_msb["inspect_json"] = DISK_INSPECT_MATCHING
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "Disk settings" in result.output
    assert "differ" in result.output
    assert not [c for c in fake_msb["calls"] if c[0] == "run"]


def test_up_does_not_warn_when_disk_settings_match(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    _enable_docker(tmp_path)
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    fake_msb["inspect_json"] = DISK_INSPECT_MATCHING
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "differ" not in result.output


def test_up_does_not_warn_when_disk_schema_is_unrecognized(tmp_path, fake_msb):
    """An unreadable inspect output must not produce a false warning."""
    name = sandbox_name_for_workspace(tmp_path)
    _enable_docker(tmp_path)
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    fake_msb["inspect_json"] = json.dumps({"config": {"unrelated": 1}})
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


def test_up_boots_with_git_worktree_mirror_mount(tmp_path, fake_msb, monkeypatch):
    """The mirror adds a second --volume of the workspace at its host path."""
    # Hermetic $HOME so existing agent config directories on the host
    # (~/.claude, ~/.codex) cannot add volumes to the assertion below.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    name = sandbox_name_for_workspace(tmp_path)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(b"[git]\nworktree_mirror = true\n")
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    run_calls = [c for c in fake_msb["calls"] if c[0] == "run"]
    assert len(run_calls) == 1
    volumes = [
        run_calls[0][i + 1] for i, arg in enumerate(run_calls[0]) if arg == "--volume"
    ]
    assert volumes == [f"{tmp_path}:/workspace", f"{tmp_path}:{tmp_path}"]
    assert run_calls[0][run_calls[0].index("--name") + 1] == name


def test_up_warns_on_mount_drift_for_existing_sandbox(tmp_path, fake_msb):
    """Flag enabled after creation: the mount is missing and must warn."""
    name = sandbox_name_for_workspace(tmp_path)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(b"[git]\nworktree_mirror = true\n")
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    # Persisted config has no bind mount at the host path.
    fake_msb["inspect_json"] = json.dumps({"config": {"unrelated": 1}})
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "worktree_mirror" in result.output
    assert "rm -f" in result.output
    assert not [c for c in fake_msb["calls"] if c[0] == "run"]


def test_up_does_not_warn_on_mount_drift_when_mirror_mounted(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(b"[git]\nworktree_mirror = true\n")
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    fake_msb["inspect_json"] = json.dumps(
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
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "worktree_mirror" not in result.output


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


def test_up_probes_dockerd_when_reusing_running_sandbox(tmp_path, fake_msb):
    """The reuse path probes dockerd when docker is enabled."""
    name = sandbox_name_for_workspace(tmp_path)
    _enable_docker(tmp_path)
    fake_msb["list_output"] = json.dumps(
        [{"name": name, "status": "Running", "id": "abc"}]
    )
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    probe_calls = [
        c for c in fake_msb["calls"] if c[0] == "exec" and c[-2:] == ["docker", "info"]
    ]
    assert probe_calls == [["exec", name, "--", "docker", "info"]]
    assert "dockerd is ready" in result.output


def test_up_warns_when_dockerd_never_becomes_ready(tmp_path, fake_msb):
    _enable_docker(tmp_path)
    fake_msb["dockerd_ready"] = False
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "dockerd did not become ready" in result.output
    assert "permission denied" in result.output  # last probe error is surfaced


def test_dockerd_probe_uses_plain_exec_without_no_tty(tmp_path, fake_msb):
    """The probe matches the manually verified invocation (no --no-tty)."""
    _enable_docker(tmp_path)
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


def test_up_boots_without_init_when_none(tmp_path, fake_msb):
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(b'[msb.sandbox]\ninit = "none"\n')
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    run_calls = [c for c in fake_msb["calls"] if c[0] == "run"]
    assert run_calls and "--init" not in run_calls[0]


def test_up_passes_docker_stop_timeout_env(tmp_path, fake_msb):
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(
        b"[msb.sandbox]\nstop_timeout = 60\ndocker_stop_timeout = 30\n"
    )
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    run_calls = [c for c in fake_msb["calls"] if c[0] == "run"]
    assert run_calls
    assert "CIRCUS_DOCKERD_SHUTDOWN_TIMEOUT=30" in run_calls[0]


def test_stop_shuts_down_s6_sandbox_via_init(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    fake_msb["pid1"] = "s6-svscan\n"
    fake_msb["list_outputs"] = [
        json.dumps([{"name": name, "status": "Running"}]),
        json.dumps([{"name": name, "status": "Stopped"}]),
    ]
    result = runner.invoke(app, ["stop", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert f"✓ Stopped {name}" in result.output
    assert [c for c in fake_msb["calls"] if c[-3:] == ["sh", "-c", "kill -TERM 1"]]
    assert not [c for c in fake_msb["calls"] if c[0] == "stop"]


def test_stop_forces_when_s6_shutdown_times_out(tmp_path, fake_msb, monkeypatch):
    name = sandbox_name_for_workspace(tmp_path)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(
        b"[msb.sandbox]\nstop_timeout = 2\ndocker_stop_timeout = 1\n"
    )
    fake_msb["pid1"] = "s6-svscan\n"
    fake_msb["list_output"] = json.dumps([{"name": name, "status": "Running"}])
    clock = {"now": 0.0}

    def fake_monotonic():
        clock["now"] += 1.0
        return clock["now"]

    monkeypatch.setattr(cli_module, "_monotonic", fake_monotonic)
    result = runner.invoke(app, ["stop", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert ["stop", "--force", name] in fake_msb["calls"]
    assert "did not stop within" in result.output


def test_stop_uses_plain_msb_stop_without_s6(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(app, ["stop", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert ["stop", name] in fake_msb["calls"]
    assert not [c for c in fake_msb["calls"] if c[-3:] == ["sh", "-c", "kill -TERM 1"]]


def test_stop_skips_probe_when_init_none(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(b'[msb.sandbox]\ninit = "none"\n')
    result = runner.invoke(app, ["stop", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert ["stop", name] in fake_msb["calls"]
    assert not [c for c in fake_msb["calls"] if c[-2:] == ["cat", "/proc/1/comm"]]


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


def _enable_ssh(tmp_path: Path, extra: str = "") -> None:
    """Write a config that opts into SSH agent forwarding."""
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_bytes(f"[ssh]\n{extra}".encode())


@pytest.fixture
def ssh_environ(monkeypatch):
    """Provide a real host agent socket and short runtime dir; return the socket."""
    import os as _os
    import tempfile as _tempfile

    sock_dir = _tempfile.mkdtemp(prefix="msbc-sock-")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(_os.path.join(sock_dir, "agent.sock"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", _tempfile.mkdtemp(prefix="msbc-rt-"))
    monkeypatch.setenv("SSH_AUTH_SOCK", _os.path.join(sock_dir, "agent.sock"))
    yield sock
    sock.close()


def test_up_with_ssh_boots_with_vsock_route_and_env(tmp_path, fake_msb, ssh_environ):
    _enable_ssh(tmp_path)
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    run_calls = [c for c in fake_msb["calls"] if c[0] == "run"]
    assert len(run_calls) == 1
    args = run_calls[0]
    link = agent_link_path()
    assert args[args.index("--vsock") + 1] == f"{link}:22022"
    assert args[args.index("--env") + 1] == "CIRCUS_DOCKERD_SHUTDOWN_TIMEOUT=20"
    assert args[args.index("--env", args.index("--env") + 1) + 1] == (
        "SSH_AUTH_SOCK=/run/ssh-agent.sock"
    )
    ssh_sock_idx = args.index("SSH_AUTH_SOCK=/run/ssh-agent.sock")
    assert args[args.index("--env", ssh_sock_idx) + 1] == (
        "CIRCUS_SSH_AGENT_VSOCK_PORT=22022"
    )
    # msb rejects --env values with the reserved MSB_ prefix.
    env_values = [v for k, v in pairwise(args) if k == "--env"]
    assert not any(v.startswith("MSB_") for v in env_values)


def test_up_with_ssh_fails_without_agent(tmp_path, fake_msb, monkeypatch):
    _enable_ssh(tmp_path)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "SSH_AUTH_SOCK" in result.output
    assert not [c for c in fake_msb.get("calls", []) if c[0] == "run"]


def test_up_without_ssh_has_no_vsock_route(tmp_path, fake_msb, ssh_environ):
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    run_calls = [c for c in fake_msb["calls"] if c[0] == "run"]
    assert len(run_calls) == 1
    args = run_calls[0]
    assert "--vsock" not in args
    assert "CIRCUS_SSH_AGENT_VSOCK_PORT" not in args


def test_up_with_ssh_warns_when_agent_unreachable(tmp_path, fake_msb, ssh_environ):
    _enable_ssh(tmp_path)
    fake_msb["ssh_add_rc"] = 2
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "Cannot reach the forwarded SSH agent" in result.output
    assert "re-create it" in result.output


def test_up_with_ssh_retries_until_bridge_is_up(tmp_path, fake_msb, ssh_environ):
    _enable_ssh(tmp_path)
    fake_msb["ssh_add_rcs"] = [2, 2, 0]
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "Cannot reach the forwarded SSH agent" not in result.output
    probes = [c for c in fake_msb["calls"] if c[0] == "exec" and "ssh-add" in c]
    assert len(probes) == 3


def test_up_with_ssh_gives_up_after_probe_budget(
    tmp_path, fake_msb, ssh_environ, monkeypatch
):
    monkeypatch.setattr(cli_module, "SSH_AGENT_PROBE_ATTEMPTS", 3)
    _enable_ssh(tmp_path)
    fake_msb["ssh_add_rc"] = 2
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert "Cannot reach the forwarded SSH agent" in result.output
    probes = [c for c in fake_msb["calls"] if c[0] == "exec" and "ssh-add" in c]
    assert len(probes) == 3


def test_up_with_ssh_does_not_retry_when_agent_has_no_keys(
    tmp_path, fake_msb, ssh_environ
):
    _enable_ssh(tmp_path)
    fake_msb["ssh_add_rc"] = 1
    runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    probes = [c for c in fake_msb["calls"] if c[0] == "exec" and "ssh-add" in c]
    assert len(probes) == 1


def test_up_with_ssh_logs_debug_when_agent_has_no_keys(
    tmp_path, fake_msb, ssh_environ, monkeypatch
):
    _enable_ssh(tmp_path)
    fake_msb["ssh_add_rc"] = 1

    class _RecordingLogger:
        """Captures (level, message) pairs for every log call."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def _log(self, level: str, msg: str, *args: object) -> None:
            self.calls.append((level, msg % args if args else msg))

        def debug(self, msg: str, *args: object) -> None:
            self._log("DEBUG", msg, *args)

        def info(self, msg: str, *args: object) -> None:
            self._log("INFO", msg, *args)

        def warning(self, msg: str, *args: object) -> None:
            self._log("WARNING", msg, *args)

        def error(self, msg: str, *args: object) -> None:
            self._log("ERROR", msg, *args)

    recorder = _RecordingLogger()
    monkeypatch.setattr(cli_module, "logger", recorder)
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "has no keys" not in result.output
    expected = (
        "The forwarded SSH agent in "
        f"{sandbox_name_for_workspace(tmp_path)} has no keys; add one "
        "on the host with: ssh-add"
    )
    assert ("DEBUG", expected) in recorder.calls


def test_up_with_ssh_copies_config_files(tmp_path, fake_msb, ssh_environ):
    ssh_dir = tmp_path / "host-ssh"
    ssh_dir.mkdir()
    config_file = ssh_dir / "config"
    config_file.write_text("Host github.com\n")
    _enable_ssh(tmp_path, extra=f'config_path = "{config_file}"\n')
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    copy_calls = [
        c for c in fake_msb["calls"] if c[0] == "copy" and "/root/.ssh/" in c[-1]
    ]
    assert [f"{name}:/root/.ssh/config"] == [c[-1] for c in copy_calls]
    chmod_calls = [
        c
        for c in fake_msb["calls"]
        if c[0] == "exec" and c[-3:] == ["chmod", "600", "/root/.ssh/config"]
    ]
    assert chmod_calls


def test_up_with_ssh_skips_missing_config_file(tmp_path, fake_msb, ssh_environ):
    _enable_ssh(tmp_path, extra=f'config_path = "{tmp_path}/missing"')
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert not [
        c for c in fake_msb["calls"] if c[0] == "copy" and "/root/.ssh/" in c[-1]
    ]


def test_up_with_ssh_copies_symlinked_config_target(tmp_path, fake_msb, ssh_environ):
    ssh_dir = tmp_path / "host-ssh"
    ssh_dir.mkdir()
    target = tmp_path / "store" / "ssh-config"
    target.parent.mkdir(parents=True)
    target.write_text("Host github.com\n")
    link = ssh_dir / "config"
    link.symlink_to(target)
    _enable_ssh(tmp_path, extra=f'config_path = "{link}"\n')
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    copy_calls = [
        c for c in fake_msb["calls"] if c[0] == "copy" and "/root/.ssh/" in c[-1]
    ]
    assert [f"{name}:/root/.ssh/config"] == [c[-1] for c in copy_calls]
    assert copy_calls[0][2] == str(target.resolve())


def _enable_hosts(tmp_path: Path, hosts_text: str = "") -> Path:
    """Write a hosts file and a config forwarding selected entries from it.

    :returns: The hosts file path.
    """
    hosts_file = tmp_path / "test-hosts"
    hosts_file.write_text(hosts_text)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "config.toml").write_bytes(
        f'[hosts]\npatterns = ["*.test.local"]\nfile = "{hosts_file}"\n'.encode()
    )
    return hosts_file


HOSTS_TEXT = (
    "127.0.0.1 localhost\n"
    "10.1.2.3 myserver.test.local alias.test.local\n"
    "10.1.2.4 other.corp.internal\n"
)


def test_up_with_hosts_writes_matching_entries(tmp_path, fake_msb):
    _enable_hosts(tmp_path, HOSTS_TEXT)
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    execs = [c for c in fake_msb["calls"] if c[0] == "exec"]
    writes = [c for c in execs if "BEGIN msb-circus hosts" in c[-3]]
    assert len(writes) == 1
    assert "10.1.2.3 myserver.test.local alias.test.local" in writes[0][-1]
    assert "10.1.2.4 other.corp.internal" not in writes[0][-1]


def test_up_without_hosts_does_not_touch_etc_hosts(tmp_path, fake_msb):
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert not [
        c for c in fake_msb["calls"] if any("BEGIN msb-circus hosts" in a for a in c)
    ]


def test_up_with_hosts_warns_on_write_failure(tmp_path, fake_msb):
    _enable_hosts(tmp_path, HOSTS_TEXT)
    fake_msb["hosts_rc"] = 1
    result = runner.invoke(app, ["up", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "Could not update /etc/hosts" in result.output


def test_start_refreshes_host_entries(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    _enable_hosts(tmp_path, HOSTS_TEXT)
    result = runner.invoke(app, ["start", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    writes = [
        c
        for c in fake_msb["calls"]
        if c[0] == "exec" and "BEGIN msb-circus hosts" in c[-3]
    ]
    assert ["start", name] in fake_msb["calls"]
    assert len(writes) == 1
    assert "10.1.2.3 myserver.test.local" in writes[0][-1]
