"""``msb exec`` argv builders and the ACP stream pump."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Sequence

ACP_FLAGS: tuple[str, ...] = ("--stream", "--quiet")
INTERACTIVE_FLAGS: tuple[str, ...] = ("--tty",)

_COPY_CHUNK = 65536

#: Heredoc delimiter for the pi trust-file script; JSON payloads are single-
#: line, so a delimiter collision is impossible.
_TRUST_HEREDOC_DELIMITER = "MSB_CIRCUS_EOF"


def build_write_trust_args(name: str, dirs: Sequence[str]) -> list[str]:
    """Build argv for an idempotent ``msb exec`` writing the pi trust file.

    The generated script writes ``$HOME/.pi/agent/trust.json`` containing
    ``{"<dir>": true}`` for every directory in *dirs*, overwriting any
    existing file. The JSON payload is built with :func:`json.dumps` and
    embedded via a quoted heredoc, so paths with quotes, spaces, or shell
    metacharacters arrive intact.

    :param name: Sandbox name.
    :param dirs: Guest directories to trust (non-empty).
    :returns: Argv list for :class:`msb_circus.msb.MsbInvoker`.
    :raises ValueError: If *dirs* is empty.
    """
    if not dirs:
        raise ValueError("at least one directory is required")
    payload = json.dumps(dict.fromkeys(dirs, True))
    script = (
        'mkdir -p "$HOME/.pi/agent" && '
        'cat > "$HOME/.pi/agent/trust.json" '
        f"<<'{_TRUST_HEREDOC_DELIMITER}'\n"
        f"{payload}\n"
        f"{_TRUST_HEREDOC_DELIMITER}"
    )
    return build_exec_args(name, ["sh", "-c", script], tty=None)


def build_repair_codex_daemon_args(name: str) -> list[str]:
    """Build argv for an ``msb exec`` repairing the codex daemon install.

    Codex installs its app-server daemon below ``$CODEX_HOME`` with an
    *absolute* ``current`` symlink (e.g.
    ``/home/node/.codex/packages/app-server-daemon/releases/<triple>``).
    When the host ``~/.codex`` is bind-mounted at ``/root/.codex`` in the
    guest, that symlink dangles and every codex invocation fails with
    "daemon executable not found". The script repoints a dangling absolute
    ``current`` symlink at its releases entry (relative), which resolves on
    the host as well. A stale absolute ``app-server-control.sock`` symlink
    left over from another machine is removed when it dangles, because it
    otherwise aborts daemon startup with ``File exists``. The write is
    idempotent and mutates only dangling symlinks; live installs and real
    sockets are left untouched.

    :param name: Sandbox name.
    :returns: Argv list for :class:`msb_circus.msb.MsbInvoker`.
    """
    script = (
        'd="$HOME/.codex/packages/app-server-daemon"\n'
        'if [ -L "$d/current" ]; then\n'
        '  t=$(readlink "$d/current")\n'
        "  case $t in\n"
        '    /*) [ -e "$d/current" ] || ln -sfn "$(basename "${t%/*}")/$(basename "$t")" "$d/current" ;;\n'
        "  esac\n"
        "fi\n"
        's="$HOME/.codex/app-server-control/app-server-control.sock"\n'
        'if [ -L "$s" ] && [ ! -e "$s" ]; then rm -f "$s"; fi'
    )
    return build_exec_args(name, ["sh", "-c", script], tty=None)


def build_exec_args(
    name: str,
    command: list[str],
    *,
    acp: bool = False,
    env: dict[str, str] | None = None,
    workdir: str | None = None,
    user: str | None = None,
    tty: bool | None = True,
) -> list[str]:
    """Build argv for ``msb exec``.

    Transport modes:

    - Interactive (default): a PTY session, ``--tty``.
    - ACP (``acp=True``): bidirectional streaming without a PTY —
      ``--stream --quiet`` with piped stdin. Never combine ``--stream``
      with ``--tty`` (echo/CRLF translation corrupts JSON lines), and do
      not use ``--no-stdin``.
    - Auto (``tty=None``): no TTY flag; msb decides. Used for scripted
      probes — this matches the invocation verified manually against the
      pinned msb release (``--no-tty`` was observed to fail probes even
      with a healthy dockerd).

    :param name: Sandbox name.
    :param command: Command (and arguments) to run inside the sandbox.
    :param acp: Select ACP streaming mode instead of a PTY session.
    :param env: Optional per-execution environment overrides.
    :param workdir: Optional guest working directory.
    :param user: Optional guest user.
    :param tty: ``True`` for ``--tty``, ``False`` for ``--no-tty``,
        ``None`` to pass no TTY flag (ignored when *acp* is set).
    :returns: Argv list; a ``--`` separator followed by the command is
        appended after the sandbox name (required by the msb CLI:
        ``msb exec [OPTIONS] <NAME> [-- <COMMAND>]...``).
    :raises ValueError: If *command* is empty.
    """
    if not command:
        raise ValueError("command must not be empty")
    args = ["exec"]
    if acp:
        args += ACP_FLAGS
    elif tty is None:
        pass
    elif tty:
        args.append("--tty")
    else:
        args.append("--no-tty")
    for key, value in (env or {}).items():
        args += ["--env", f"{key}={value}"]
    if workdir:
        args += ["--workdir", workdir]
    if user:
        args += ["--user", user]
    args.append(name)
    args.append("--")
    args += command
    return args


def _copy_bytes(src, dst) -> bool:
    """Copy *src* to *dst* in chunks until EOF.

    Reads use ``read1`` when available (buffered and raw streams) so a
    partially filled pipe is forwarded immediately. A blocking
    ``BufferedReader.read(n)`` would wait for the full chunk or EOF — an
    ACP peer that writes one small request and then waits for the reply
    would deadlock the stream. Sources without ``read1`` (e.g.
    :class:`io.BytesIO` in tests) fall back to ``read``.

    :param src: Source binary file-like object (blocking reads).
    :param dst: Destination binary file-like object.
    :returns: ``True`` on clean EOF, ``False`` on an I/O error (e.g. a
        broken pipe because the peer went away).
    """
    try:
        read = getattr(src, "read1", None) or src.read
        while True:
            chunk = read(_COPY_CHUNK)
            if not chunk:
                return True
            dst.write(chunk)
            dst.flush()
    except (OSError, ValueError):
        return False


def _terminate(proc: subprocess.Popen) -> None:
    """Terminate *proc* if it is still running (best effort)."""
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass


def run_acp_stream(
    argv: list[str],
    *,
    stdin=None,
    stdout=None,
    stderr=None,
    grace_seconds: float = 5.0,
) -> int:
    """Run *argv* as a bidirectional, byte-faithful ACP stream.

    Host stdin is forwarded verbatim to the child process, child stdout to
    host stdout, and child stderr to host stderr — the latter concurrently
    on a separate thread so heavy stderr output cannot deadlock the ACP
    channel. Bytes are never buffered, transformed, or re-framed.

    The child is terminated (without affecting the reported exit code,
    which becomes 0) when the host side disappears, and shortly after
    host stdin reaches EOF if the child does not exit on its own — so an
    IDE disconnect never leaves an unintended agent process behind.
    Genuine child exit codes propagate unchanged.

    :param argv: Full argv (including the msb binary) to execute.
    :param stdin: Host stdin (binary); defaults to ``sys.stdin.buffer``.
    :param stdout: Host stdout (binary); defaults to ``sys.stdout.buffer``.
    :param stderr: Host stderr (binary); defaults to ``sys.stderr.buffer``.
    :param grace_seconds: Grace period after stdin EOF or before forced
        kill on termination.
    :returns: Child exit code; 0 when the host side disconnected.
    """
    if stdin is None:
        stdin = sys.stdin.buffer
    if stdout is None:
        stdout = sys.stdout.buffer
    if stderr is None:
        stderr = sys.stderr.buffer

    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    child_stdin = proc.stdin
    child_stdout = proc.stdout
    child_stderr = proc.stderr
    assert child_stdin is not None and child_stdout is not None  # PIPEs
    assert child_stderr is not None
    stdin_done = threading.Event()
    host_gone = threading.Event()
    killed = threading.Event()

    def pump_input() -> None:
        try:
            if not _copy_bytes(stdin, child_stdin):
                host_gone.set()
        finally:
            try:
                child_stdin.close()
            except OSError:
                pass
            stdin_done.set()

    def pump_errors() -> None:
        if not _copy_bytes(child_stderr, stderr):
            host_gone.set()

    def watchdog() -> None:
        """After stdin EOF, kill a child that refuses to exit on its own."""
        stdin_done.wait()
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            _terminate(proc)
            killed.set()

    input_thread = threading.Thread(target=pump_input, daemon=True)
    error_thread = threading.Thread(target=pump_errors, daemon=True)
    watchdog_thread = threading.Thread(target=watchdog, daemon=True)
    input_thread.start()
    error_thread.start()
    watchdog_thread.start()

    if not _copy_bytes(child_stdout, stdout):
        host_gone.set()

    if host_gone.is_set() and proc.poll() is None:
        _terminate(proc)
        killed.set()

    try:
        returncode = proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        killed.set()
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
        returncode = proc.returncode

    for thread in (input_thread, error_thread, watchdog_thread):
        thread.join(timeout=1)

    if host_gone.is_set() or killed.is_set():
        return 0
    return returncode if returncode is not None else 0
