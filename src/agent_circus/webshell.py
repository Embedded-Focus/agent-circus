"""Browser shell sessions: token, origin check, PTY and manager.

The dashboard's shell endpoint runs an interactive shell inside a
sandbox VM. The proxy spawns ``msb exec --tty`` behind a host
pseudo-terminal (msb has no attach or resize API of its own), forwards
PTY output and WebSocket input, and maps resizes to ``TIOCSWINSZ`` plus
``SIGWINCH``.

The shell is off by default: the CLI only creates a :class:`ShellManager`
with a token when the user passes ``--shell``. Everything here assumes
Linux or macOS (``pty``, ``fcntl``, ``termios``); Windows is not a
target of agent-circus.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import hmac
import json
import logging
import os
import pty
import secrets
import signal
import struct
import termios
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigurationError, resolve_agent_workdir
from .envs import merged_env
from .exec_ import build_exec_args
from .websocket import OP_BINARY, OP_TEXT, WebSocket, WebSocketClosed

#: Most shell sessions open at the same time.
SHELL_MAX_SESSIONS = 8
#: Seconds to wait for the hello message.
HELLO_TIMEOUT_S = 10.0
#: Seconds between SIGHUP and SIGKILL when a session ends.
KILL_GRACE_S = 3.0
#: Guest command: bash (as in ``agent-circus exec -- bash``), else sh.
SHELL_SCRIPT = "if command -v bash >/dev/null 2>&1; then exec bash; fi; exec sh"
#: Terminal variables; the lowest precedence, the config may override.
TERMINAL_ENV = {"TERM": "xterm-256color", "COLORTERM": "truecolor"}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ShellLaunch:
    """Everything that differs per workspace in the shell's exec argv.

    :ivar env: Merged environment (TERMINAL_ENV, then merged_env).
    :ivar shell_env: Keys of *env* whose values are guest shell words.
    :ivar workdir: Guest working directory.
    """

    env: dict[str, str]
    shell_env: frozenset[str]
    workdir: str


def shell_launch_for_workspace(
    workspace: str | None, environ: Mapping[str, str] | None = None
) -> ShellLaunch:
    """Load the workspace config and return the shell's launch spec.

    Mirrors ``agent-circus exec -- bash`` in that workspace:
    ``load_config``, ``merged_env(config, (), {})`` and
    ``resolve_agent_workdir``. Only the environment and the workdir are
    taken from the config; host-side settings such as ``binary`` are
    ignored on purpose (the workspace is writable from the guest).

    :param workspace: Host path of the sandbox workspace, or ``None``.
    :param environ: Host environment for passthrough (default
        ``os.environ``); injected in tests.
    :raises ConfigurationError: If the workspace is unknown or not a
        directory, the config is invalid, or a required passthrough
        variable is unset.
    """
    if workspace is None:
        raise ConfigurationError(
            "The workspace of this sandbox is unknown (no /workspace "
            "mount). Use agent-circus exec in a terminal."
        )
    if not Path(workspace).is_dir():
        raise ConfigurationError(
            f"Workspace {workspace} does not exist on the host any more."
        )
    from .config import load_config

    try:
        config = load_config(Path(workspace))
    except ConfigurationError as e:
        raise ConfigurationError(
            f"The workspace config cannot be used for a shell: {e}"
        ) from e
    try:
        merged, shell_words = merged_env(config, (), {}, environ=environ)
    except ConfigurationError as e:
        raise ConfigurationError(
            f"{e} Start the proxy from a shell where it is set."
        ) from e
    return ShellLaunch(
        env={**TERMINAL_ENV, **merged},
        shell_env=shell_words,
        workdir=resolve_agent_workdir(config, Path(workspace)),
    )


def build_shell_argv(binary: Path, sandbox: str, launch: ShellLaunch) -> list[str]:
    """Return the host argv that starts the shell in a sandbox.

    Uses :func:`agent_circus.exec_.build_exec_args` with the same keyword
    arguments ``agent-circus exec`` passes, so the guest sees the same
    environment and directory. The only intended differences from the
    ``exec`` command: the command (``SHELL_SCRIPT`` instead of ``bash``),
    the two terminal variables and no ``-e`` overrides.

    :param binary: Path of the msb binary to run (the proxy's own).
    :param sandbox: Sandbox name.
    :param launch: Launch spec from
        :func:`shell_launch_for_workspace`.
    :returns: Full argv, including the binary path.
    """
    shell_env = {k: launch.env[k] for k in launch.shell_env} or None
    return [
        str(binary),
        *build_exec_args(
            sandbox,
            ["sh", "-c", SHELL_SCRIPT],
            env=launch.env or None,
            shell_env=shell_env,
            workdir=launch.workdir,
            tty=True,
        ),
    ]


class PtyProcess:
    """A child process whose stdin, stdout and stderr are a new PTY.

    Spawned with ``start_new_session=True``, so the child leads its own
    process group (``pgid == pid``). The PTY is not its controlling
    terminal, so resizes write the size with ``TIOCSWINSZ`` and signal
    ``SIGWINCH`` to the group explicitly.
    """

    def __init__(self, process: asyncio.subprocess.Process, master_fd: int) -> None:
        """Store the process and the connected pipe transports.

        :param process: The spawned child process.
        :param master_fd: The PTY master file descriptor (a dup of it is
            used for writing).
        """
        self.process = process
        self._master_fd = master_fd
        self._read_transport: Any = None
        self._write_transport: Any = None
        self._reader: asyncio.StreamReader | None = None
        self._terminated = False

    @classmethod
    async def spawn(cls, argv: list[str], cols: int, rows: int) -> PtyProcess:
        """Spawn *argv* on a new PTY of the given size.

        :param argv: Full host argv (binary included).
        :param cols: Terminal columns (1..1000).
        :param rows: Terminal rows (1..500).
        :returns: The running PTY process.
        :raises OSError: If the PTY or the child cannot be started.
        """
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        instance = cls(process, master)
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        read_transport, _protocol = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader),
            os.fdopen(master, "rb", buffering=0),
        )
        instance._read_transport = read_transport
        instance._reader = reader
        write_transport, _ = await loop.connect_write_pipe(
            asyncio.Protocol, os.fdopen(os.dup(master), "wb", buffering=0)
        )
        instance._write_transport = write_transport
        return instance

    def resize(self, cols: int, rows: int) -> None:
        """Set the PTY size and signal the child's process group.

        :param cols: Terminal columns (1..1000).
        :param rows: Terminal rows (1..500).
        """
        if self._terminated:
            return
        fcntl.ioctl(
            self._master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0)
        )
        try:
            os.killpg(self.process.pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass

    async def read(self) -> bytes:
        """Return the next PTY output chunk, or ``b""`` at EOF.

        On Linux a read from the master after the child has gone fails
        with ``OSError(EIO)``; that is mapped to ``b""``.

        :returns: Up to 64 KiB of output.
        """
        assert self._reader is not None
        try:
            return await self._reader.read(65536)
        except OSError as e:
            if e.errno == errno.EIO:
                return b""
            raise

    def write(self, data: bytes) -> None:
        """Write *data* to the PTY input (non-blocking, buffered).

        Writes after the transport closed are ignored.

        :param data: Raw input bytes.
        """
        if self._write_transport is None or self._write_transport.is_closing():
            return
        self._write_transport.write(data)

    async def wait(self) -> int | None:
        """Return the child's exit code, or ``None`` when signalled.

        :returns: The exit code, or ``None``.
        """
        return await self.process.wait()

    async def terminate(self) -> None:
        """End the child and its process group. Idempotent.

        Sends ``SIGHUP``, waits up to KILL_GRACE_S, then ``SIGKILL``,
        and closes the PTY transports. Ignores a child that already
        exited.
        """
        if self._terminated:
            return
        self._terminated = True
        try:
            os.killpg(self.process.pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(self.process.wait(), KILL_GRACE_S)
        except TimeoutError:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await self.process.wait()
        for transport in (self._read_transport, self._write_transport):
            if transport is not None:
                transport.close()


# ---- token -----------------------------------------------------------------


def shell_token_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the shell token file path.

    ``$XDG_RUNTIME_DIR/agent-circus/shell-token`` when XDG_RUNTIME_DIR is
    set (cleared at logout, so there is a new token per login session),
    else ``~/.local/state/agent-circus/shell-token``.

    :param environ: Environment mapping, defaults to :data:`os.environ`.
    :returns: The token file path.
    """
    env = os.environ if environ is None else environ
    runtime_dir = env.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "agent-circus" / "shell-token"
    return Path.home() / ".local" / "state" / "agent-circus" / "shell-token"


def _token_file_unsafe(path: Path) -> str | None:
    """Return a reason string when *path* is unsafe to keep, else None.

    :param path: Token file to inspect.
    :returns: The reason, or ``None`` when the file is private enough.
    """
    try:
        stat = path.stat()
    except OSError:
        return None

    if stat.st_uid != os.getuid():
        return f"{path} is owned by another user"
    if stat.st_mode & 0o077:
        return f"{path} is readable by group or others"
    return None


def load_or_create_shell_token(path: Path) -> str:
    """Return the token from *path*, creating it when missing or unsafe.

    Creates the directory with mode 0700 and the file with mode 0600.
    Replaces a file that is group/other accessible or owned by another
    user (logs a warning), or that does not hold a token of at least 32
    URL-safe characters.

    :param path: Token file path.
    :returns: The token.
    :raises OSError: If the directory or file cannot be created.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    reason = _token_file_unsafe(path)
    if reason is not None:
        logger.warning("Replacing the shell token file: %s", reason)
        path.unlink(missing_ok=True)
    if path.exists():
        try:
            token = path.read_text().strip()
        except OSError:
            token = ""
        if len(token) >= 32:
            return token
        logger.warning("Replacing the shell token file: %s holds no valid token", path)
        path.unlink(missing_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o600)
    return token


def read_shell_token(path: Path) -> str | None:
    """Return the token for the CLI, or ``None`` when missing or unsafe.

    :param path: Token file path.
    :returns: The token, or ``None``.
    """
    if _token_file_unsafe(path) is not None:
        return None
    try:
        token = path.read_text().strip()
    except OSError:
        return None
    return token if len(token) >= 32 else None


def token_matches(expected: str, given: object) -> bool:
    """Constant-time compare of the expected and the given token.

    :param expected: The token from the token file.
    :param given: The token from the hello message (any object).
    :returns: ``True`` only for an exact string match.
    """
    if not isinstance(given, str):
        return False
    return hmac.compare_digest(expected.encode(), given.encode())


# ---- origin check ----------------------------------------------------------


def origin_allowed(origin: str | None, port: int) -> bool:
    """Return whether *origin* is an allowed dashboard origin.

    Only ``http://localhost:<port>``, ``http://127.0.0.1:<port>`` and
    ``http://[::1]:<port>`` are allowed (scheme and host
    case-insensitive). None, "null", https, other ports or other hosts
    give False.

    :param origin: The Origin header value, or ``None``.
    :param port: Port the proxy listens on.
    :returns: ``True`` for a same-origin dashboard request.
    """
    if origin is None:
        return False
    parts = origin.split("://", 1)
    if len(parts) != 2:
        return False
    scheme, host = parts
    if scheme.lower() != "http":
        return False
    return host.lower() in (
        f"localhost:{port}",
        f"127.0.0.1:{port}",
        f"[::1]:{port}",
    )


# ---- session manager -------------------------------------------------------


@dataclass
class ShellInfo:
    """Public state of one open shell session.

    :ivar id: Session id (increasing).
    :ivar sandbox: Sandbox name.
    :ivar client: ``"host:port"`` of the browser.
    :ivar started_at: Epoch seconds of the session start.
    """

    id: int
    sandbox: str
    client: str
    started_at: float


class ShellManager:
    """Run and bound browser shell sessions.

    A manager with ``token=None`` is disabled: every run is refused
    with close code 4503. All methods run on the proxy's event loop;
    blocking work (config loading) is pushed to worker threads.
    """

    def __init__(
        self,
        *,
        token: str | None,
        argv_factory: Callable[[str, str | None], list[str]],
        notify: Callable[[str], None],
        clock: Callable[[], float] = time.time,
        max_sessions: int = SHELL_MAX_SESSIONS,
    ) -> None:
        """Store the token, the argv factory and the audit callback.

        :param token: The shell token, or ``None`` to disable the shell.
        :param argv_factory: ``factory(sandbox, workspace) -> argv``; may
            raise :class:`ConfigurationError`.
        :param notify: Audit callback (one line per open/close).
        :param clock: Epoch-seconds callable (injected for tests).
        :param max_sessions: Maximum number of open sessions.
        """
        self._token = token
        self._argv_factory = argv_factory
        self._notify = notify
        self._clock = clock
        self._max_sessions = max_sessions
        self._next_id = 1
        self._sessions: dict[int, ShellInfo] = {}
        self._processes: dict[int, PtyProcess] = {}

    @property
    def enabled(self) -> bool:
        """Return whether the shell is enabled (a token is set).

        :returns: ``True`` when sessions may be opened.
        """
        return self._token is not None

    def sessions(self) -> list[dict[str, Any]]:
        """Return the open sessions as JSON-ready dicts.

        :returns: One dict per session (id, sandbox, client, started_at).
        """
        return [
            {
                "id": info.id,
                "sandbox": info.sandbox,
                "client": info.client,
                "started_at": info.started_at,
            }
            for info in self._sessions.values()
        ]

    async def _error_and_close(self, ws: WebSocket, code: int, message: str) -> None:
        """Send an error message and close with *code*.

        :param ws: The WebSocket connection.
        :param code: Close status code.
        :param message: Human-readable error text.
        """
        try:
            await ws.send_text(json.dumps({"type": "error", "message": message}))
            await ws.close(code)
        except (WebSocketClosed, ConnectionError, OSError):
            pass

    async def run(
        self,
        ws: WebSocket,
        sandbox_lookup: Callable[[str], Awaitable[dict[str, Any] | None]],
        sandbox: str,
        client: str,
    ) -> None:
        """Run one shell session on an upgraded WebSocket.

        Validates the hello message, the token, the sandbox, the session
        limit and the workspace config, spawns the PTY process and
        forwards both directions until the shell or the connection ends.

        :param ws: The upgraded WebSocket connection.
        :param sandbox_lookup: Returns the sandbox view of a running
            managed sandbox, or ``None``.
        :param sandbox: Sandbox name from the request.
        :param client: ``"host:port"`` of the browser.
        """
        if not self.enabled:
            await self._error_and_close(
                ws,
                4503,
                "The shell is off. Restart the proxy with: agent-circus proxy --shell",
            )
            return
        try:
            frame_type, payload = await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT_S)
        except TimeoutError:
            await self._error_and_close(ws, 1008, "No hello message")
            return
        except WebSocketClosed:
            return
        hello = self._parse_hello(frame_type, payload)
        if hello is None:
            await self._error_and_close(ws, 1008, "Invalid hello message")
            return
        cols, rows, token = hello
        if not token_matches(self._token or "", token):
            await self._error_and_close(ws, 4401, "Missing or wrong shell token")
            return
        view = await sandbox_lookup(sandbox)
        if view is None:
            await self._error_and_close(
                ws,
                4404,
                f"Sandbox {sandbox!r} is not running or not a agent-circus sandbox",
            )
            return
        if len(self._sessions) >= self._max_sessions:
            await self._error_and_close(
                ws,
                4429,
                f"Too many open shells ({self._max_sessions}). Close one and try again.",
            )
            return
        try:
            argv = await asyncio.to_thread(
                self._argv_factory, sandbox, view.get("workspace")
            )
        except ConfigurationError as e:
            await self._error_and_close(ws, 4422, str(e))
            return
        try:
            proc = await PtyProcess.spawn(argv, cols, rows)
        except OSError as e:
            await self._error_and_close(ws, 1011, f"Could not start the shell: {e}")
            return

        session_id = self._next_id
        self._next_id += 1
        info = ShellInfo(
            id=session_id, sandbox=sandbox, client=client, started_at=self._clock()
        )
        self._sessions[session_id] = info
        self._processes[session_id] = proc
        self._notify(f"shell opened: {sandbox} (client {client}, session {session_id})")
        await ws.send_text(
            json.dumps({"type": "ready", "sandbox": sandbox, "session": session_id})
        )
        output_ended_first = False
        try:
            output_ended_first = await self._pump(ws, proc)
        finally:
            await proc.terminate()
            code = await proc.wait()
            duration = self._clock() - info.started_at
            if output_ended_first:
                try:
                    await ws.send_text(json.dumps({"type": "exit", "code": code}))
                    await ws.close(1000)
                except (WebSocketClosed, ConnectionError, OSError):
                    pass
            self._sessions.pop(session_id, None)
            self._processes.pop(session_id, None)
            self._notify(
                f"shell closed: {sandbox} (session {session_id}, exit {code}, "
                f"{int(duration)}s)"
            )

    @staticmethod
    def _parse_hello(frame_type: int, payload: bytes) -> tuple[int, int, str] | None:
        """Parse and validate the hello message.

        :param frame_type: Opcode of the first message.
        :param payload: Message payload.
        :returns: ``(cols, rows, token)``, or ``None`` when invalid.
        """
        if frame_type != OP_TEXT:
            return None
        try:
            data = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict) or data.get("type") != "hello":
            return None
        cols, rows, token = data.get("cols"), data.get("rows"), data.get("token")
        if (
            isinstance(cols, bool)
            or not isinstance(cols, int)
            or not 1 <= cols <= 1000
            or isinstance(rows, bool)
            or not isinstance(rows, int)
            or not 1 <= rows <= 500
            or not isinstance(token, str)
        ):
            return None
        return cols, rows, token

    async def _pump(self, ws: WebSocket, proc: PtyProcess) -> bool:
        """Forward PTY output and WebSocket input until one side ends.

        :param ws: The WebSocket connection.
        :param proc: The PTY process.
        :returns: ``True`` when the shell exited (output hit EOF);
            ``False`` when the WebSocket ended first.
        """

        async def pump_output() -> None:
            while True:
                data = await proc.read()
                if not data:
                    return
                await ws.send_binary(data)

        async def pump_input() -> None:
            while True:
                try:
                    frame_type, payload = await ws.recv()
                except WebSocketClosed:
                    return
                if frame_type == OP_BINARY:
                    proc.write(payload)
                    continue
                try:
                    data = json.loads(payload)
                except (ValueError, UnicodeDecodeError):
                    continue  # ignore other text messages
                if (
                    isinstance(data, dict)
                    and data.get("type") == "resize"
                    and isinstance(data.get("cols"), int)
                    and isinstance(data.get("rows"), int)
                    and not isinstance(data.get("cols"), bool)
                    and not isinstance(data.get("rows"), bool)
                    and 1 <= data["cols"] <= 1000
                    and 1 <= data["rows"] <= 500
                ):
                    proc.resize(data["cols"], data["rows"])

        output_task = asyncio.create_task(pump_output())
        input_task = asyncio.create_task(pump_input())
        done, _pending = await asyncio.wait(
            {output_task, input_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in (output_task, input_task):
            task.cancel()
        await asyncio.gather(output_task, input_task, return_exceptions=True)
        return output_task in done and output_task.exception() is None

    async def close_all(self) -> None:
        """Terminate every open session (proxy shutdown).

        Ends each session's PTY process group (SIGHUP, then SIGKILL),
        so the guest shell and its children end with the proxy.
        """
        for proc in list(self._processes.values()):
            await proc.terminate()
        self._processes.clear()
        self._sessions.clear()
