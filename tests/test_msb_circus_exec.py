"""Tests for the ACP stream pump and the exec command."""

import io
import sys
import textwrap
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
        return recorded.get("acp_returncode", 0)

    def fake_interactive_run(argv, **kwargs):
        recorded["interactive_argv"] = argv
        return SimpleNamespace(returncode=recorded.get("interactive_rc", 0))

    monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: Path("/fake"))
    monkeypatch.setattr(cli_module, "check_version", lambda binary: "0.7.12")
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
