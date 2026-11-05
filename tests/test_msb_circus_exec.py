"""Tests for the ACP stream pump and the exec command."""

import io
import json
import logging
import os
import selectors
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

import msb_circus.cli as cli_module
from msb_circus.cli import app
from msb_circus.config import sandbox_name_for_workspace
from msb_circus.envs import parse_env_assignments
from msb_circus.exceptions import ConfigurationError
from msb_circus.exec_ import run_acp_stream

runner = CliRunner()


def _stderr_stream_handlers() -> list[logging.StreamHandler]:
    """Return root logger StreamHandlers that are not FileHandlers."""
    return [
        handler
        for handler in logging.root.handlers
        if isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
    ]


def _file_handlers() -> list[logging.FileHandler]:
    """Return root logger FileHandlers."""
    return [h for h in logging.root.handlers if isinstance(h, logging.FileHandler)]


@pytest.fixture(autouse=True)
def _clean_root_handlers():
    """Keep the root logger pristine around each test."""
    handlers = logging.root.handlers[:]
    level = logging.root.level
    logging.root.handlers.clear()
    yield
    logging.root.handlers[:] = handlers
    logging.root.setLevel(level)


class FakeInvoker:
    """Records invocations; scripted list/inspect output."""

    behavior: ClassVar[dict] = {}

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        self.behavior.setdefault("calls", []).append(args)
        if args[:2] == ["image", "inspect"]:
            return SimpleNamespace(
                returncode=self.behavior.get("inspect_rc", 0),
                stdout="",
                stderr="",
            )
        if args[0] == "list":
            return SimpleNamespace(
                returncode=0,
                stdout=self.behavior.get("list_output", "[]"),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")


@pytest.fixture
def fake_msb(monkeypatch):
    """Patch the CLI invoker (no host subprocess, no ACP pump)."""
    FakeInvoker.behavior = {}
    FakeInvoker.behavior["inspect_rc"] = 0
    FakeInvoker.behavior["list_output"] = "[]"
    recorded: dict = {}

    def fake_run_acp_stream(argv, **kwargs):
        recorded["acp_argv"] = argv
        recorded["acp_kwargs"] = kwargs
        return recorded.get("acp_returncode", 0)

    def fake_interactive_run(argv, **kwargs):
        recorded["interactive_argv"] = argv
        return SimpleNamespace(returncode=recorded.get("interactive_rc", 0))

    monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: Path("/fake"))
    monkeypatch.setattr(cli_module, "check_version", lambda binary: "0.7.12")
    monkeypatch.setattr(cli_module, "probe_version", lambda binary: "0.7.12")
    monkeypatch.setattr(cli_module, "MsbInvoker", FakeInvoker)
    monkeypatch.setattr(cli_module, "run_acp_stream", fake_run_acp_stream)
    monkeypatch.setattr(cli_module.subprocess, "run", fake_interactive_run)
    monkeypatch.setattr(cli_module, "DOCKERD_PROBE_ATTEMPTS", 1)
    return recorded


def echo_agent() -> list[str]:
    """Agent script that echoes stdin to stdout byte-for-byte."""
    return [
        sys.executable,
        "-c",
        (
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); "
            "sys.stdout.buffer.flush()"
        ),
    ]


def test_acp_stream_roundtrip_is_byte_faithful():
    payload = b'{"jsonrpc":"2.0","method":"initialize"}\r\n'
    payload += "unicode: héllo — ünïcode ✓\n".encode()
    out = io.BytesIO()
    returncode = run_acp_stream(
        echo_agent(), stdin=io.BytesIO(payload), stdout=out, stderr=io.BytesIO()
    )
    assert returncode == 0
    assert out.getvalue() == payload  # no bytes added, removed, or reframed


def test_acp_stream_observer_sees_exact_bytes():
    payload = b'{"jsonrpc":"2.0","id":1}\n'
    observed: dict[str, bytearray] = {}

    def observe(direction: str, chunk: bytes) -> None:
        observed.setdefault(direction, bytearray()).extend(chunk)

    out = io.BytesIO()
    returncode = run_acp_stream(
        echo_agent(),
        stdin=io.BytesIO(payload),
        stdout=out,
        stderr=io.BytesIO(),
        observer=observe,
    )

    assert returncode == 0
    assert bytes(observed["client-to-agent"]) == payload
    assert bytes(observed["agent-to-client"]) == payload


def test_acp_stream_large_message_arrives_intact():
    payload = b"x" * 1_048_576 + b"\n"
    out = io.BytesIO()
    returncode = run_acp_stream(
        echo_agent(), stdin=io.BytesIO(payload), stdout=out, stderr=io.BytesIO()
    )
    assert returncode == 0
    assert out.getvalue() == payload


def test_acp_stream_propagates_exit_code():
    out = io.BytesIO()
    returncode = run_acp_stream(
        [sys.executable, "-c", "import sys; sys.exit(3)"],
        stdin=io.BytesIO(b""),
        stdout=out,
        stderr=io.BytesIO(),
    )
    assert returncode == 3


def test_acp_stream_heavy_stderr_does_not_deadlock_stdout():
    agent = [
        sys.executable,
        "-c",
        textwrap.dedent(
            """
            import sys
            for _ in range(2000):
                sys.stderr.write("noise" * 256)  # ~2 MB
            sys.stderr.flush()
            data = sys.stdin.buffer.read()
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            """
        ),
    ]
    out = io.BytesIO()
    returncode = run_acp_stream(
        agent, stdin=io.BytesIO(b"payload\n"), stdout=out, stderr=io.BytesIO()
    )
    assert returncode == 0
    assert out.getvalue() == b"payload\n"


def test_acp_stream_terminates_hung_agent_after_stdin_eof():
    out = io.BytesIO()
    returncode = run_acp_stream(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=io.BytesIO(b""),  # immediate EOF
        stdout=out,
        stderr=io.BytesIO(),
        grace_seconds=0.2,
    )
    assert returncode == 0  # clean disconnect, no hang, no leaked process


def test_acp_stream_forwards_before_stdin_eof():
    """A small request must reach the agent while stdin stays open.

    Regression test: the input pump used ``BufferedReader.read(n)``, which
    blocks until the full chunk or EOF. An editor that writes one small
    request and then waits for the reply (acp.el's initialize) deadlocked
    the stream — the request never reached the agent.
    """
    stdin_read, stdin_write = os.pipe()
    stdout_read, stdout_write = os.pipe()
    agent = [
        sys.executable,
        "-c",
        (
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.readline()); "
            "sys.stdout.buffer.flush(); sys.stdin.buffer.read()"
        ),
    ]
    result: dict = {}

    def run():
        result["returncode"] = run_acp_stream(
            agent,
            stdin=os.fdopen(stdin_read, "rb"),  # buffered, like the editor's pipe
            stdout=os.fdopen(stdout_write, "wb", buffering=0),
            stderr=io.BytesIO(),
            grace_seconds=1.0,
        )

    thread = threading.Thread(target=run)
    thread.start()
    try:
        os.write(stdin_write, b'{"jsonrpc":"2.0","id":1}\n')
        selector = selectors.DefaultSelector()
        selector.register(stdout_read, selectors.EVENT_READ)
        received = b""
        deadline = time.monotonic() + 10
        while b'"jsonrpc"' not in received and time.monotonic() < deadline:
            if selector.select(0.2):
                chunk = os.read(stdout_read, 65536)
                if not chunk:
                    break
                received += chunk
        selector.close()
        assert received == b'{"jsonrpc":"2.0","id":1}\n'
    finally:
        os.close(stdin_write)  # end the session like an editor disconnect
    thread.join(timeout=15)
    assert result["returncode"] == 0


def test_parse_env_assignments():
    assert parse_env_assignments(["A=1", "B=x=y"]) == {"A": "1", "B": "x=y"}
    with pytest.raises(ConfigurationError, match="expected KEY=VALUE"):
        parse_env_assignments(["NOEQUALS"])
    with pytest.raises(ConfigurationError, match="key must not be empty"):
        parse_env_assignments(["=value"])


def test_exec_defaults_to_configured_agent_command(tmp_path, fake_msb):
    result = runner.invoke(app, ["exec", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    argv = fake_msb["interactive_argv"]
    assert argv[1:3] == ["exec", "--tty"]
    assert argv[-1] == "pi"


def test_exec_acp_uses_stream_quiet_without_tty(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(
        app,
        ["exec", "--workspace", str(tmp_path), "--acp", "--", "opencode", "acp"],
    )
    assert result.exit_code == 0
    argv = fake_msb["acp_argv"]
    assert "--stream" in argv and "--quiet" in argv
    assert "--tty" not in argv and "--no-stdin" not in argv
    assert name in argv
    assert argv[-2:] == ["opencode", "acp"]


def test_exec_acp_trace_connects_stream_observer(tmp_path, fake_msb, monkeypatch):
    class FakeTrace:
        directory = tmp_path / ".agent-circus" / "traces" / "trace-id"

        def record_stream(self, direction, data):
            pass

        def start_collectors(self, binary, name):
            pass

        def finish(self, returncode):
            fake_msb["trace_returncode"] = returncode

    trace = FakeTrace()
    created: dict = {}

    def create_trace(**kwargs):
        created.update(kwargs)
        return trace

    monkeypatch.setattr(cli_module.TraceSession, "create", create_trace)
    result = runner.invoke(
        app,
        [
            "exec",
            "--workspace",
            str(tmp_path),
            "--acp",
            "--trace",
            "--trace-dir",
            "diagnostics",
            "--",
            "pi-acp",
        ],
    )

    assert result.exit_code == 0
    assert created["root"] == tmp_path / "diagnostics"
    assert created["config"].enabled is True
    assert fake_msb["acp_kwargs"]["observer"] == trace.record_stream
    assert callable(fake_msb["acp_kwargs"]["on_started"])
    assert fake_msb["trace_returncode"] == 0


def test_exec_passes_env_and_overrides(tmp_path, fake_msb):
    result = runner.invoke(
        app,
        [
            "exec",
            "--workspace",
            str(tmp_path),
            "-e",
            "OPENAI_API_KEY=sk-test",
            "-e",
            "EXTRA=1",
            "--",
            "pi",
        ],
    )
    assert result.exit_code == 0
    argv = fake_msb["interactive_argv"]
    assert argv[argv.index("--env") + 1] == "OPENAI_API_KEY=sk-test"
    assert argv[argv.index("--env", argv.index("--env") + 1) + 1] == "EXTRA=1"


def test_exec_fails_fast_on_missing_required_passthrough(tmp_path, fake_msb):
    from msb_circus.config import CONFIG_DIR_NAME, CONFIG_FILE_NAME

    (tmp_path / CONFIG_DIR_NAME).mkdir()
    (tmp_path / CONFIG_DIR_NAME / CONFIG_FILE_NAME).write_bytes(
        b'env_passthrough = ["MISSING_KEY"]\n'
    )
    result = runner.invoke(app, ["exec", "--workspace", str(tmp_path), "--", "pi"])
    assert result.exit_code == 1
    assert "MISSING_KEY" in result.output
    assert "calls" not in FakeInvoker.behavior or not [
        c for c in FakeInvoker.behavior["calls"] if c[0] == "exec"
    ]


def test_exec_rejects_malformed_env_assignment(tmp_path, fake_msb):
    result = runner.invoke(
        app, ["exec", "--workspace", str(tmp_path), "-e", "MALFORMED", "--", "pi"]
    )
    assert result.exit_code == 1
    assert "expected KEY=VALUE" in result.output


def test_exec_boots_sandbox_when_not_running(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(app, ["exec", "--workspace", str(tmp_path), "--", "pi"])
    assert result.exit_code == 0
    run_calls = [c for c in FakeInvoker.behavior["calls"] if c[0] == "run"]
    assert len(run_calls) == 1
    assert run_calls[0][run_calls[0].index("--name") + 1] == name


def test_build_write_trust_args_single_dir():
    from msb_circus.exec_ import build_write_trust_args

    args = build_write_trust_args("agents-proj", ["/workspace"])
    assert args[:3] == ["exec", "agents-proj", "--"]
    assert args[3:5] == ["sh", "-c"]
    script = args[5]
    assert 'mkdir -p "$HOME/.pi/agent"' in script
    assert 'cat > "$HOME/.pi/agent/trust.json"' in script
    assert "<<'MSB_CIRCUS_EOF'" in script
    assert '{"/workspace": true}' in script


def test_build_write_trust_args_multiple_dirs_deduplicated():
    from msb_circus.exec_ import build_write_trust_args

    args = build_write_trust_args(
        "agents-proj", ["/workspace", "/workspaces/other", "/workspace"]
    )
    script = args[-1]
    assert '{"/workspace": true, "/workspaces/other": true}' in script


def test_build_write_trust_args_escapes_shell_metacharacters():
    from msb_circus.exec_ import build_write_trust_args

    args = build_write_trust_args("agents-proj", ["/tmp/a'b\"c $HOME dir"])
    script = args[-1]
    # The payload is embedded verbatim inside a quoted heredoc, so shell
    # metacharacters in the path need no escaping.
    assert '{"/tmp/a\'b\\"c $HOME dir": true}' in script


def test_build_write_trust_args_rejects_empty_dirs():
    from msb_circus.exec_ import build_write_trust_args

    with pytest.raises(ValueError, match="at least one directory"):
        build_write_trust_args("agents-proj", [])


def test_build_repair_codex_daemon_args_repairs_dangling_symlinks():
    from msb_circus.exec_ import build_repair_codex_daemon_args

    args = build_repair_codex_daemon_args("agents-proj")
    assert args[:3] == ["exec", "agents-proj", "--"]
    assert args[3:5] == ["sh", "-c"]
    script = args[5]

    # Simulate the guest state: an absolute, dangling daemon ``current``
    # symlink (host paths do not exist in the guest) and a stale absolute
    # control-socket symlink from another machine.
    with tempfile.TemporaryDirectory() as home:
        daemon_dir = Path(home) / ".codex/packages/app-server-daemon"
        (daemon_dir / "releases/0.157.1-x86_64-unknown-linux-musl/bin").mkdir(
            parents=True
        )
        (daemon_dir / "releases/0.157.1-x86_64-unknown-linux-musl/bin/codex").touch()
        (daemon_dir / "current").symlink_to(
            "/home/node/.codex/packages/app-server-daemon/releases/"
            "0.157.1-x86_64-unknown-linux-musl"
        )
        control_dir = Path(home) / ".codex/app-server-control"
        control_dir.mkdir(parents=True)
        (control_dir / "app-server-control.sock").symlink_to(
            "/tmp/codex-daemon-1000/some-hash"
        )

        env = {**os.environ, "HOME": home}
        subprocess.run(
            ["sh", "-c", script],
            env=env,
            check=True,
            capture_output=True,
        )

        current = daemon_dir / "current"
        assert current.is_symlink()
        assert os.readlink(current) == ("releases/0.157.1-x86_64-unknown-linux-musl")
        assert (current / "bin/codex").exists()
        assert not (control_dir / "app-server-control.sock").is_symlink()


def test_build_repair_codex_daemon_args_leaves_live_installs_alone():
    from msb_circus.exec_ import build_repair_codex_daemon_args

    args = build_repair_codex_daemon_args("agents-proj")
    script = args[5]

    with tempfile.TemporaryDirectory() as home:
        daemon_dir = Path(home) / ".codex/packages/app-server-daemon"
        (daemon_dir / "releases/0.157.1-x86_64-unknown-linux-musl").mkdir(parents=True)
        # A working relative symlink and a resolvable absolute one must be
        # preserved; a live socket file must not be removed.
        relative = daemon_dir / "current"
        relative.symlink_to("releases/0.157.1-x86_64-unknown-linux-musl")
        (daemon_dir / "other").symlink_to(
            str(daemon_dir / "releases/0.157.1-x86_64-unknown-linux-musl")
        )
        control_dir = Path(home) / ".codex/app-server-control"
        control_dir.mkdir(parents=True)
        socket = control_dir / "app-server-control.sock"
        socket.touch()

        env = {**os.environ, "HOME": home}
        subprocess.run(
            ["sh", "-c", script],
            env=env,
            check=True,
            capture_output=True,
        )

        assert os.readlink(relative) == "releases/0.157.1-x86_64-unknown-linux-musl"
        assert (daemon_dir / "other").is_symlink()
        assert socket.exists()


def test_build_repair_codex_daemon_args_is_noop_without_codex_home():
    from msb_circus.exec_ import build_repair_codex_daemon_args

    args = build_repair_codex_daemon_args("agents-proj")
    script = args[5]

    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "HOME": home}
        subprocess.run(
            ["sh", "-c", script],
            env=env,
            check=True,
            capture_output=True,
        )
        assert not (Path(home) / ".codex").exists()


def test_exec_acp_routes_logs_to_given_file_only(tmp_path, fake_msb):
    log_file = tmp_path / "acp.log"
    result = runner.invoke(
        app,
        [
            "--log-file",
            str(log_file),
            "exec",
            "--acp",
            "--workspace",
            str(tmp_path),
            "--",
            "pi-acp",
        ],
    )
    assert result.exit_code == 0
    assert [h.baseFilename for h in _file_handlers()] == [str(log_file)]
    assert _stderr_stream_handlers() == []


def test_exec_acp_without_log_file_uses_default_file(tmp_path, fake_msb):
    result = runner.invoke(
        app, ["exec", "--acp", "--workspace", str(tmp_path), "--", "pi-acp"]
    )
    assert result.exit_code == 0
    file_handlers = _file_handlers()
    assert len(file_handlers) == 1
    assert Path(file_handlers[0].baseFilename) == cli_module.DEFAULT_ACP_LOG_FILE
    assert _stderr_stream_handlers() == []


def test_exec_without_acp_keeps_stderr_logging(tmp_path, fake_msb):
    result = runner.invoke(
        app, ["exec", "--workspace", str(tmp_path), "--", "echo", "hi"]
    )
    assert result.exit_code == 0
    assert _stderr_stream_handlers()


def test_exec_acp_status_messages_go_to_log_file(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    FakeInvoker.behavior["list_output"] = json.dumps(
        [{"name": name, "status": "Running"}]
    )
    log_file = tmp_path / "acp.log"
    result = runner.invoke(
        app,
        [
            "--log-file",
            str(log_file),
            "exec",
            "--acp",
            "--workspace",
            str(tmp_path),
            "--",
            "pi-acp",
        ],
    )
    assert result.exit_code == 0
    for handler in _file_handlers():
        handler.flush()
    assert "reusing it" in log_file.read_text()


def test_exec_acp_config_warnings_go_to_log_file(tmp_path, fake_msb):
    # Reproduces the reported failure mode: unsupported config keys must
    # not reach stderr in ACP mode (acp.el treats stderr as API errors).
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(b'runtime = "podman"\n')
    log_file = tmp_path / "acp.log"
    result = runner.invoke(
        app,
        [
            "--log-file",
            str(log_file),
            "exec",
            "--acp",
            "--workspace",
            str(tmp_path),
            "--",
            "pi-acp",
        ],
    )
    assert result.exit_code == 0
    for handler in _file_handlers():
        handler.flush()
    assert "Unsupported config key 'runtime'" in log_file.read_text()


# A fake msb that mimics the real binary's stdin behavior: every piped
# ``exec`` reads one chunk of host stdin (like the real run_piped /
# forward_stdin path) and forwards it to the guest command. Probe-style
# commands (``docker info``, the trust-write ``sh -c``) discard their stdin.
_FAKE_MSB = """#!/bin/sh
case "$1" in
  --version) echo "msb 0.7.12"; exit 0;;
  run) exit 0;;
esac
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then exit 0; fi
if [ "$1" = "list" ]; then echo "[]"; exit 0; fi
if [ "$1" = "exec" ]; then
  shift
  d="{drain_dir}/drain.$$"
  dd bs=8192 count=1 of="$d" 2>/dev/null
  while [ $# -gt 0 ]; do case "$1" in --) shift; break;; *) shift;; esac; done
  case "$1" in
    docker) rm -f "$d"; exit 0;;
    sh) rm -f "$d"; exit 0;;
    *) cat "$d" 2>/dev/null; rm -f "$d"; exec "$@";;
  esac
fi
exit 0
"""


def test_exec_acp_survives_stdin_consuming_probes(tmp_path):
    """The ACP stream must survive helper execs that read host stdin.

    Regression test: the pi trust write and the dockerd probe used to inherit
    the caller's stdin. Real msb always reads host stdin in its piped exec
    path, so those probes consumed the editor's initialize request before the
    ACP pump could forward it — the agent never responded and the session
    hung silently.
    """
    drain_dir = tmp_path / "drain"
    drain_dir.mkdir()
    fake_msb = tmp_path / "msb"
    fake_msb.write_text(_FAKE_MSB.format(drain_dir=drain_dir))
    fake_msb.chmod(0o755)
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(f'[msb]\nbinary = "{fake_msb}"\n')

    payload = (
        b'{"jsonrpc":"2.0","method":"initialize","id":1,"params":'
        b'{"clientInfo":{"name":"agent-shell"},"protocolVersion":1}}\n'
    )
    child_code = (
        "import sys; from msb_circus.cli import run_cli; "
        "sys.argv = ['msb-circus', '--log-level', 'DEBUG', "
        "'--log-file', '/dev/null', 'exec', '--acp', '--workspace', "
        f"'{tmp_path}', '--', 'cat']; run_cli()"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,  # raw pipes; read() then returns whatever is available
    )
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(payload)
    proc.stdin.flush()
    # Emulate acp.el: the request is written once and the editor's stdin
    # pipe stays open for the whole session. The response must arrive while
    # stdin is still open — the probe execs (trust write, dockerd probe)
    # must not have consumed it, and the pump must not buffer it until EOF.
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    received = b""
    deadline = time.monotonic() + 10
    while payload.strip() not in received and time.monotonic() < deadline:
        if selector.select(0.2):
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            received += chunk
    selector.close()
    assert payload.strip() in received
    proc.stdin.close()
    proc.wait(timeout=30)
