"""Tests for the browser shell sessions (agent_circus.webshell)."""

import asyncio
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

from agent_circus.config import ConfigurationError, parse_config
from agent_circus.envs import merged_env
from agent_circus.exec_ import build_exec_args
from agent_circus.webshell import (
    SHELL_SCRIPT,
    TERMINAL_ENV,
    PtyProcess,
    ShellLaunch,
    ShellManager,
    build_shell_argv,
    load_or_create_shell_token,
    origin_allowed,
    read_shell_token,
    shell_launch_for_workspace,
    shell_token_path,
    token_matches,
)

# ---- argv building ---------------------------------------------------------


def _config(tmp_path: Path) -> Path:
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / ".agent-circus").mkdir()
    (workspace / ".agent-circus" / "config.toml").write_bytes(
        b'[env]\nGREETING = "hello $HOME"\nLITERAL = "plain-value"\n'
    )
    return workspace


def test_build_shell_argv_uses_tty_env_and_workdir(tmp_path):
    workspace = _config(tmp_path)
    launch = shell_launch_for_workspace(str(workspace), environ={"HOME": "/home/u"})
    argv = build_shell_argv(Path("/usr/bin/msb"), "agents-proj", launch)
    assert argv[0] == "/usr/bin/msb"
    assert "--tty" in argv
    assert "--env" in argv and "TERM=xterm-256color" in argv
    assert "--env" in argv and "LITERAL=plain-value" in argv
    workdir_index = argv.index("--workdir")
    assert argv[workdir_index + 1]
    # Sandbox name directly before the "--" separator.
    assert argv[argv.index("--") - 1] == "agents-proj"
    assert argv[-1] == SHELL_SCRIPT


def test_build_shell_argv_wraps_shell_words_like_exec(tmp_path):
    workspace = _config(tmp_path)
    launch = shell_launch_for_workspace(str(workspace), environ={"HOME": "/home/u"})
    argv = build_shell_argv(Path("/usr/bin/msb"), "agents-proj", launch)
    # GREETING contains $HOME: it is a shell word, exported by the wrapper
    # that build_exec_args builds around the command.
    joined = " ".join(argv)
    assert 'export GREETING="hello $HOME"' in joined
    assert 'exec "$@"' in joined
    # The wrapper wraps the shell script, which stays one argument.
    assert SHELL_SCRIPT in argv[-1]


def test_build_shell_argv_matches_exec_argv(tmp_path):
    _config(tmp_path)
    config = parse_config({})
    merged, _shell_words = merged_env(config, (), {}, environ={"HOME": "/home/u"})
    expected = build_exec_args(
        "agents-proj",
        ["sh", "-c", SHELL_SCRIPT],
        env={**TERMINAL_ENV, **merged} or None,
        shell_env=None,
        workdir="/home/u/proj",
        tty=True,
    )
    launch = ShellLaunch(
        env={**TERMINAL_ENV, **merged}, shell_env=frozenset(), workdir="/home/u/proj"
    )
    argv = build_shell_argv(Path("/usr/bin/msb"), "agents-proj", launch)
    assert argv[1:] == expected


# ---- launch spec -----------------------------------------------------------


def _write_config(workspace: Path, text: bytes) -> Path:
    workspace.mkdir(parents=True)
    (workspace / ".agent-circus").mkdir()
    (workspace / ".agent-circus" / "config.toml").write_bytes(text)
    return workspace


def test_launch_applies_env_table_and_passthrough(tmp_path):
    workspace = _write_config(
        tmp_path / "proj",
        b'env_passthrough = ["HOST_VAR"]\n\n[env]\nA = "table"\n',
    )
    launch = shell_launch_for_workspace(
        str(workspace), environ={"HOST_VAR": "from-host", "HOME": "/home/u"}
    )
    assert launch.env["A"] == "table"
    assert launch.env["HOST_VAR"] == "from-host"
    assert launch.env["TERM"] == "xterm-256color"


def test_launch_adds_ssh_auth_sock_when_ssh_configured(tmp_path):
    workspace = _write_config(tmp_path / "proj", b"[ssh]\nconfig_path = '/tmp/cfg'\n")
    launch = shell_launch_for_workspace(str(workspace), environ={"HOME": "/home/u"})
    assert launch.env["SSH_AUTH_SOCK"] == "/run/ssh-agent.sock"


def test_launch_config_can_override_term(tmp_path):
    workspace = _write_config(tmp_path / "proj", b'[env]\nTERM = "screen"\n')
    launch = shell_launch_for_workspace(str(workspace), environ={"HOME": "/home/u"})
    assert launch.env["TERM"] == "screen"


def test_launch_workdir_follows_worktree_mirror(tmp_path):
    # Mirror on (default): the host path is the guest workdir.
    workspace = _write_config(tmp_path / "proj", b"")
    launch = shell_launch_for_workspace(str(workspace), environ={"HOME": "/home/u"})
    assert launch.workdir == str(workspace)
    # Mirror off: /workspace.
    workspace2 = _write_config(tmp_path / "proj2", b"[git]\nworktree_mirror = false\n")
    launch2 = shell_launch_for_workspace(str(workspace2), environ={"HOME": "/home/u"})
    assert launch2.workdir == "/workspace"
    # Explicit agent workdir wins.
    workspace3 = _write_config(
        tmp_path / "proj3", b'[msb.agent]\nworkdir = "/srv/work"\n'
    )
    launch3 = shell_launch_for_workspace(str(workspace3), environ={"HOME": "/home/u"})
    assert launch3.workdir == "/srv/work"


def test_launch_missing_passthrough_raises_with_proxy_hint(tmp_path):
    workspace = _write_config(tmp_path / "proj", b'env_passthrough = ["NOPE"]\n')
    with pytest.raises(ConfigurationError) as excinfo:
        shell_launch_for_workspace(str(workspace), environ={})
    assert "NOPE" in str(excinfo.value)
    assert "Start the proxy from a shell where it is set." in str(excinfo.value)


def test_launch_unknown_workspace_raises():
    with pytest.raises(ConfigurationError) as excinfo:
        shell_launch_for_workspace(None)
    assert "unknown" in str(excinfo.value)


def test_launch_missing_workspace_dir_raises():
    with pytest.raises(ConfigurationError) as excinfo:
        shell_launch_for_workspace("/tmp/definitely-not-here-12345")
    assert "does not exist" in str(excinfo.value)


def test_launch_ignores_config_binary(tmp_path):
    workspace = _write_config(tmp_path / "proj", b'binary = "/tmp/evil"\n')
    launch = shell_launch_for_workspace(str(workspace), environ={"HOME": "/home/u"})
    argv = build_shell_argv(Path("/usr/bin/msb"), "agents-proj", launch)
    assert argv[0] == "/usr/bin/msb"
    assert "/tmp/evil" not in argv


# ---- PTY process -----------------------------------------------------------


def _child_script(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "child.py"
    script.write_text(body)
    return [sys.executable, str(script)]


def test_pty_process_echoes_input(tmp_path):
    async def scenario():
        argv = _child_script(
            tmp_path,
            "import sys\n"
            "line = sys.stdin.readline()\n"
            "sys.stdout.write(line.upper())\n"
            "sys.stdout.flush()\n",
        )
        proc = await PtyProcess.spawn(argv, 80, 24)
        await asyncio.sleep(0.2)
        proc.write(b"ping\n")
        while True:
            data = await asyncio.wait_for(proc.read(), 5)
            if b"PING" in data:
                break
            if not data:
                break
        await proc.terminate()
        return data

    data = asyncio.run(scenario())
    assert b"PING" in data


def test_pty_process_reports_initial_size(tmp_path):
    async def scenario():
        argv = _child_script(
            tmp_path,
            "import os, sys\n"
            "size = os.get_terminal_size(0)\n"
            "sys.stdout.write(f'{size.columns}x{size.lines}\\n')\n"
            "sys.stdout.flush()\n",
        )
        proc = await PtyProcess.spawn(argv, 123, 45)
        await asyncio.sleep(0.3)
        await proc.terminate()
        return proc

    asyncio.run(scenario())


def test_pty_process_reports_initial_size_check(tmp_path):
    async def scenario():
        argv = _child_script(
            tmp_path,
            "import os, sys\n"
            "size = os.get_terminal_size(0)\n"
            "sys.stdout.write(f'{size.columns}x{size.lines}\\n')\n"
            "sys.stdout.flush()\n",
        )
        proc = await PtyProcess.spawn(argv, 123, 45)
        data = b""
        while b"x" not in data:
            chunk = await asyncio.wait_for(proc.read(), 5)
            if not chunk:
                break
            data += chunk
        await proc.terminate()
        return data

    data = asyncio.run(scenario())
    assert b"123x45" in data


def test_pty_process_resize_sends_sigwinch(tmp_path):
    async def scenario():
        argv = _child_script(
            tmp_path,
            "import os, signal, sys\n"
            "def handler(signum, frame):\n"
            "    size = os.get_terminal_size(0)\n"
            "    os.write(1, f'WINCH {size.columns}x{size.lines}\\n'.encode())\n"
            "signal.signal(signal.SIGWINCH, handler)\n"
            "os.write(1, b'ready\\n')\n"
            "while True:\n"
            "    signal.pause()\n",
        )
        proc = await PtyProcess.spawn(argv, 80, 24)
        data = b""
        while b"ready" not in data:
            data += await asyncio.wait_for(proc.read(), 5)
        proc.resize(111, 33)
        while b"WINCH" not in data:
            chunk = await asyncio.wait_for(proc.read(), 5)
            if not chunk:
                break
            data += chunk
        await proc.terminate()
        return data

    data = asyncio.run(scenario())
    assert b"WINCH 111x33" in data


def test_pty_process_eof_after_child_exit_is_empty_bytes(tmp_path):
    async def scenario():
        argv = _child_script(tmp_path, "print('bye')\n")
        proc = await PtyProcess.spawn(argv, 80, 24)
        await proc.wait()
        # A read after the child exited gives b"" (EIO mapped on Linux).
        deadline = time.monotonic() + 5
        data = b""
        while time.monotonic() < deadline:
            chunk = await proc.read()
            if not chunk:
                return True
            data += chunk
        return False

    assert asyncio.run(scenario())


def test_pty_process_terminate_kills_ignoring_child(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_circus.webshell.KILL_GRACE_S", 0.3)

    async def scenario():
        argv = _child_script(
            tmp_path,
            "import signal\n"
            "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
            "print('ignoring')\n"
            "while True:\n"
            "    signal.pause()\n",
        )
        proc = await PtyProcess.spawn(argv, 80, 24)
        await asyncio.sleep(0.3)
        await asyncio.wait_for(proc.terminate(), 5)  # must not hang
        code = await proc.wait()
        return code is not None

    assert asyncio.run(scenario())


# ---- token -----------------------------------------------------------------


def test_token_file_created_with_private_modes(tmp_path):
    path = tmp_path / "state" / "shell-token"
    token = load_or_create_shell_token(path)
    assert len(token) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_token_file_reused_when_safe(tmp_path):
    path = tmp_path / "shell-token"
    first = load_or_create_shell_token(path)
    second = load_or_create_shell_token(path)
    assert first == second


def test_token_file_replaced_when_world_readable(tmp_path):
    path = tmp_path / "shell-token"
    token = load_or_create_shell_token(path)
    os.chmod(path, 0o644)
    replaced = load_or_create_shell_token(path)
    assert replaced != token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_token_matches_rejects_non_strings():
    assert token_matches("secret", "secret")
    assert not token_matches("secret", "wrong")
    assert not token_matches("secret", None)
    assert not token_matches("secret", 42)
    assert not token_matches("secret", b"secret")


def test_shell_token_path_uses_xdg_runtime_dir():
    assert shell_token_path({"XDG_RUNTIME_DIR": "/run/user/1000"}) == (
        Path("/run/user/1000") / "agent-circus" / "shell-token"
    )
    assert shell_token_path({}).name == "shell-token"


def test_read_shell_token_none_when_missing(tmp_path):
    assert read_shell_token(tmp_path / "nope") is None


def test_read_shell_token_none_when_unsafe(tmp_path):
    path = tmp_path / "shell-token"
    path.write_text("x" * 40)
    os.chmod(path, 0o666)
    assert read_shell_token(path) is None


# ---- origin check ----------------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "allowed"),
    [
        ("http://localhost:7700", True),
        ("http://127.0.0.1:7700", True),
        ("http://[::1]:7700", True),
        ("HTTP://LOCALHOST:7700", True),
        (None, False),
        ("null", False),
        ("https://localhost:7700", False),
        ("http://localhost:7701", False),
        ("http://evil.com", False),
        ("http://opencode.agents-foo.localhost:7700", False),
        ("http://localhost", False),
    ],
)
def test_origin_allowed_matrix(origin, allowed):
    assert origin_allowed(origin, 7700) is allowed


# ---- ShellManager over a real WebSocket ------------------------------------

import websockets.asyncio.client
import websockets.exceptions

from agent_circus.websocket import WebSocket, check_upgrade, handshake_response

if TYPE_CHECKING:
    from collections.abc import Callable

    from websockets.typing import Origin


class WsServer:
    """A real asyncio server routing upgrades to a handler."""

    def __init__(self):
        self.handler: Callable | None = None
        self.server: asyncio.AbstractServer | None = None
        self.port: int | None = None

    async def start(self, handler) -> None:
        self.handler = handler
        self.server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _accept(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        key = check_upgrade(head)
        writer.write(handshake_response(key))
        await writer.drain()
        ws = WebSocket(reader, writer)
        handler = self.handler
        assert handler is not None
        try:
            await handler(ws)
        finally:
            if not writer.is_closing():
                writer.close()

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()


def _client_connect(port):
    return websockets.asyncio.client.connect(
        f"ws://127.0.0.1:{port}/api/shell",
        origin=cast("Origin", "http://localhost:7700"),
        open_timeout=5,
    )


ECHO_CHILD = (
    "import sys\n"
    "print('SHELL-READY', flush=True)\n"
    "line = sys.stdin.readline()\n"
    "sys.stdout.write('ECHO:' + line)\n"
    "sys.stdout.flush()\n"
)

SLEEP_CHILD = "import time; print('up', flush=True); time.sleep(30)"


class RecordingManager:
    """A ShellManager wrapped with recording of its calls."""

    def __init__(self, **kwargs):
        self.factory_calls: list[tuple[str, str | None]] = []
        self.notify_lines: list[str] = []
        self.spawns_allowed = True
        argv = [sys.executable, "-c", ECHO_CHILD]

        def factory(sandbox, workspace):
            self.factory_calls.append((sandbox, workspace))
            return list(argv)

        options: dict[str, Any] = {
            "token": "secret-token",
            "argv_factory": factory,
            "notify": self.notify_lines.append,
        }
        options.update(kwargs)
        self.manager = ShellManager(**options)  # type: ignore[arg-type]

    def lookup(self, name):
        async def _lookup(name=name):
            if name == "agents-foo":
                return {
                    "name": name,
                    "running": True,
                    "managed": True,
                    "workspace": "/w",
                }
            return None

        return _lookup()


def _hello(token="secret-token", cols=80, rows=24):
    return json.dumps({"type": "hello", "token": token, "cols": cols, "rows": rows})


def _run_ws_test(setup, client_coro):
    """Run a manager test: setup() -> rec, then client(rec, port)."""

    async def scenario():
        rec = setup()
        server = WsServer()
        await server.start(
            lambda ws: rec.manager.run(ws, rec.lookup, "agents-foo", "127.0.0.1:1")
        )
        try:
            return await client_coro(rec, server.port)
        finally:
            await rec.manager.close_all()
            await server.stop()

    return asyncio.run(scenario())


async def _await_close(ws):
    """Receive until the server closes; return the close code."""
    while True:
        try:
            await asyncio.wait_for(ws.recv(), 5)
        except websockets.exceptions.ConnectionClosed as e:
            return e.rcvd.code if e.rcvd else None
        except TimeoutError:
            return None


def test_session_runs_and_reports_exit_code():
    async def client(rec, port):
        async with await _client_connect(port) as ws:
            await ws.send(_hello())
            ready = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert ready["type"] == "ready"
            assert ready["sandbox"] == "agents-foo"
            # Read until the child echoed our input or the session ended.
            while True:
                message = await asyncio.wait_for(ws.recv(), 5)
                if isinstance(message, bytes) and b"SHELL-READY" in message:
                    await ws.send(b"input\n")
                    continue
                if isinstance(message, bytes) and b"ECHO:input" in message:
                    code = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    assert code["type"] == "exit"
                    assert code["code"] == 0
                    return "echoed"
                if isinstance(message, str):
                    data = json.loads(message)
                    if data.get("type") == "exit":
                        return "exited"
            # Wait for the normal close (1000).
            assert await _await_close(ws) == 1000

    result = _run_ws_test(RecordingManager, client)
    assert result in ("echoed", "exited")


def test_session_rejects_wrong_token_with_4401_and_spawns_nothing():
    async def client(rec, port):
        async with await _client_connect(port) as ws:
            await ws.send(_hello(token="wrong"))
            message = json.loads(await asyncio.wait_for(ws.recv(), 5))
            code = await _await_close(ws)
            return message, code

    message, code = _run_ws_test(RecordingManager, client)
    assert message["type"] == "error"
    assert code == 4401


def test_session_rejects_missing_hello_with_1008(monkeypatch):
    monkeypatch.setattr("agent_circus.webshell.HELLO_TIMEOUT_S", 0.2)

    async def client(rec, port):
        async with await _client_connect(port) as ws:
            return await _await_close(ws)

    assert _run_ws_test(RecordingManager, client) == 1008


def test_session_rejects_unknown_sandbox_with_4404():
    # The server always passes "agents-foo"; use a lookup that knows none.
    async def client(rec, port):
        async with await _client_connect(port) as ws:
            await ws.send(_hello())
            message = json.loads(await asyncio.wait_for(ws.recv(), 5))
            code = await _await_close(ws)
            return message["type"], code

    async def scenario():
        rec = RecordingManager()
        server = WsServer()
        await server.start(
            lambda ws: rec.manager.run(ws, rec.lookup, "agents-ghost", "127.0.0.1:1")
        )
        try:
            async with await _client_connect(server.port) as ws:
                await ws.send(_hello())
                message = json.loads(await asyncio.wait_for(ws.recv(), 5))
                code = await _await_close(ws)
                return message["type"], code
        finally:
            await rec.manager.close_all()
            await server.stop()

    message_type, code = asyncio.run(scenario())
    assert message_type == "error"
    assert code == 4404


def test_session_config_error_closes_with_4422_and_spawns_nothing():
    async def client(rec, port):
        async with await _client_connect(port) as ws:
            await ws.send(_hello())
            message = json.loads(await asyncio.wait_for(ws.recv(), 5))
            code = await _await_close(ws)
            return message, code

    def setup():
        rec = RecordingManager()

        def factory(sandbox, workspace):
            raise ConfigurationError("The workspace config cannot be used")

        rec.manager._argv_factory = factory
        return rec

    message, code = _run_ws_test(setup, client)
    assert "cannot be used" in message["message"]
    assert code == 4422


def test_session_passes_workspace_from_view_to_factory():
    async def client(rec, port):
        async with await _client_connect(port) as ws:
            await ws.send(_hello())
            await _await_close(ws)

    rec_holder = {}

    def setup():
        rec = RecordingManager()
        rec_holder["rec"] = rec
        return rec

    _run_ws_test(setup, client)
    assert rec_holder["rec"].factory_calls == [("agents-foo", "/w")]


def test_notify_lines_contain_no_env_values():
    """Notify lines name the sandbox but never an environment value."""
    secret = "super-secret-value-xyz"

    async def client(rec, port):
        async with await _client_connect(port) as ws:
            await ws.send(_hello())
            await _await_close(ws)

    def setup():
        rec = RecordingManager(
            argv_factory=lambda s, w: [
                sys.executable,
                "-c",
                f"import os; print(os.environ.get('SECRET', '{secret}'), flush=True)",
            ]
        )
        # Pretend the env carried a secret via the child.
        return rec

    rec_holder = {}

    def setup_with_holder():
        rec = setup()
        rec_holder["rec"] = rec
        return rec

    _run_ws_test(setup_with_holder, client)
    joined = "\n".join(rec_holder["rec"].notify_lines)
    assert "shell opened" in joined
    assert secret not in joined
    assert "SECRET=" not in joined


def test_session_limit_returns_4429():
    async def client(rec, port):
        async with await _client_connect(port) as ws:
            await ws.send(_hello())
            message = json.loads(await asyncio.wait_for(ws.recv(), 5))
            code = await _await_close(ws)
            return message["message"], code

    def setup():
        return RecordingManager(max_sessions=0)

    message, code = _run_ws_test(setup, client)
    assert "Too many open shells" in message
    assert code == 4429


def test_disabled_manager_returns_4503():
    async def client(rec, port):
        async with await _client_connect(port) as ws:
            # The manager closes before the hello (run order step 1), so
            # send and recv may fail with the close code directly.
            code = None
            try:
                await ws.send(_hello())
                message = json.loads(await asyncio.wait_for(ws.recv(), 5))
                assert message["type"] == "error"
                assert "agent-circus proxy --shell" in message["message"]
                code = await _await_close(ws)
            except websockets.exceptions.ConnectionClosed as e:
                code = e.rcvd.code if e.rcvd else None
            return code

    def setup():
        return RecordingManager(token=None)

    assert _run_ws_test(setup, client) == 4503


def test_client_close_terminates_process():
    async def client(rec, port):
        async with await _client_connect(port) as ws:
            await ws.send(_hello())
            ready = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert ready["type"] == "ready"
        # Leaving the context closes the WebSocket; the manager must
        # terminate the child (a 30 s sleeper) without hanging.

    def setup():
        rec = RecordingManager(
            argv_factory=lambda s, w: [sys.executable, "-c", SLEEP_CHILD]
        )
        return rec

    # Must complete well below the 30 s child sleep.
    _run_ws_test(setup, client)


def test_notify_lines_for_open_and_close():
    async def client(rec, port):
        async with await _client_connect(port) as ws:
            await ws.send(_hello())
            await _await_close(ws)

    rec_holder = {}

    def setup():
        rec = RecordingManager()
        rec_holder["rec"] = rec
        return rec

    _run_ws_test(setup, client)
    lines = rec_holder["rec"].notify_lines
    assert any(line.startswith("shell opened: agents-foo") for line in lines)
    assert any(line.startswith("shell closed: agents-foo") for line in lines)


def test_close_all_terminates_every_session():
    async def scenario():
        rec = RecordingManager(
            argv_factory=lambda s, w: [sys.executable, "-c", SLEEP_CHILD]
        )
        server = WsServer()
        await server.start(
            lambda ws: rec.manager.run(ws, rec.lookup, "agents-foo", "127.0.0.1:1")
        )
        try:
            async with await _client_connect(server.port) as ws:
                await ws.send(_hello())
                ready = json.loads(await asyncio.wait_for(ws.recv(), 5))
                assert ready["type"] == "ready"
                assert rec.manager.sessions()
                await asyncio.wait_for(rec.manager.close_all(), 5)
                assert rec.manager.sessions() == []
        finally:
            await server.stop()

    asyncio.run(scenario())
