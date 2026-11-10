"""``msb exec`` argv builders and the ACP stream pump."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence

from .config import (
    SSH_AGENT_GUEST_SOCKET,
    SSH_GUEST_DIR,
)
from .gitconfig import GIT_GUEST_GLOBAL_CONFIG, GIT_GUEST_MANAGED_CONFIG

S6_PID1_COMM = "s6-svscan"
"""``/proc/1/comm`` of a sandbox whose PID 1 is s6-overlay."""

ACP_FLAGS: tuple[str, ...] = ("--stream", "--quiet")
INTERACTIVE_FLAGS: tuple[str, ...] = ("--tty",)

_COPY_CHUNK = 65536

logger = logging.getLogger(__name__)

#: Heredoc delimiter for the pi trust-file script; JSON payloads are single-
#: line, so a delimiter collision is impossible.
_TRUST_HEREDOC_DELIMITER = "MSB_CIRCUS_EOF"

#: First marker line of the msb-circus managed block in the guest hosts file.
HOSTS_BLOCK_BEGIN = "# BEGIN msb-circus hosts (managed, do not edit)"
#: Last marker line of the msb-circus managed block in the guest hosts file.
HOSTS_BLOCK_END = "# END msb-circus hosts"

#: Guest path of the hosts file the managed block is written into.
GUEST_HOSTS_FILE = "/etc/hosts"

HOST_CAS_GUEST_PATH = "/.msb/tls/host-cas.pem"
"""Bundle that msb writes into the guest for ``--trust-host-cas``."""


def _hosts_script(path: str) -> str:
    """Build the POSIX sh script replacing the managed hosts block.

    Removes an existing managed block (the lines between the marker
    lines) and appends the new block passed as ``$1`` between the
    markers; an empty ``$1`` only removes the block. Lines outside the
    markers are preserved. The file is rewritten in place
    (``cat > <path>``) so its inode, owner, and mode stay unchanged.

    :param path: Guest hosts file path.
    :returns: Script text; run it as ``sh -c SCRIPT sh "$BLOCK"``.
    """
    return (
        "set -e\n"
        f'begin="{HOSTS_BLOCK_BEGIN}"\n'
        f'end="{HOSTS_BLOCK_END}"\n'
        "tmp=$(mktemp)\n"
        'awk -v b="$begin" -v e="$end" '
        f"'$0==b{{skip=1;next}} $0==e{{skip=0;next}} !skip' {path} > \"$tmp\"\n"
        'if [ -n "$1" ]; then\n'
        '  { printf "%s\\n" "$begin"; printf "%s\\n" "$1"; printf "%s\\n" "$end"; } >> "$tmp"\n'
        "fi\n"
        f'cat "$tmp" > {path}\n'
        'rm -f "$tmp"\n'
    )


def build_write_hosts_args(name: str, block: str) -> list[str]:
    """Build argv replacing the msb-circus block in the guest ``/etc/hosts``.

    Removes an existing managed block and appends *block* between the
    marker lines; an empty *block* only removes the managed block. Lines
    outside the markers are preserved. The file is rewritten in place
    (``cat > /etc/hosts``) so its inode, owner and mode stay unchanged.

    :param name: Sandbox name.
    :param block: Rendered host lines from
        :func:`msb_circus.hosts.render_host_block`.
    :returns: Argv list for :class:`msb_circus.msb.MsbInvoker`.
    """
    return build_exec_args(
        name, ["sh", "-c", _hosts_script(GUEST_HOSTS_FILE), "sh", block], tty=None
    )


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


#: Node.js script merging Claude Code folder trust into the guest state file.
#: ``__DIRS__`` is replaced with a JSON array (a valid JS literal). The file
#: is resolved like Claude Code does (``$CLAUDE_CONFIG_DIR`` or ``$HOME``),
#: created when missing, and replaced atomically; unparsable content aborts
#: without touching the file.
_CLAUDE_TRUST_SCRIPT = """\
const fs = require("fs"), os = require("os"), path = require("path");
const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const file = path.join(process.env.CLAUDE_CONFIG_DIR || os.homedir(), ".claude.json");
let state = {};
try { state = JSON.parse(fs.readFileSync(file, "utf8")); }
catch (e) { if (e.code !== "ENOENT") throw e; }
if (!isObj(state)) throw new Error(file + " does not contain a JSON object");
const projects = isObj(state.projects) ? state.projects : {};
for (const dir of __DIRS__) {
  const entry = isObj(projects[dir]) ? projects[dir] : {};
  entry.hasTrustDialogAccepted = true;
  projects[dir] = entry;
}
state.projects = projects;
const tmp = file + ".msb-circus.tmp";
fs.writeFileSync(tmp, JSON.stringify(state, null, 2) + "\\n", { mode: 0o600 });
fs.renameSync(tmp, file);
"""


def build_write_claude_trust_args(name: str, dirs: Sequence[str]) -> list[str]:
    """Build argv for an idempotent ``msb exec`` pre-trusting dirs for Claude Code.

    The generated Node.js script sets ``projects[<dir>].hasTrustDialogAccepted``
    in the guest's ``~/.claude.json`` (``$CLAUDE_CONFIG_DIR/.claude.json`` when
    set), keeping all other keys and creating the file when it is missing.
    Directories are embedded via :func:`json.dumps`, so arbitrary paths arrive
    intact. Node.js is available in the guest because Claude Code needs it.

    :param name: Sandbox name.
    :param dirs: Guest directories to trust (non-empty).
    :returns: Argv list for :class:`msb_circus.msb.MsbInvoker`.
    :raises ValueError: If *dirs* is empty.
    """
    if not dirs:
        raise ValueError("at least one directory is required")
    script = _CLAUDE_TRUST_SCRIPT.replace(
        "__DIRS__", json.dumps(list(dict.fromkeys(dirs)))
    )
    return build_exec_args(name, ["node", "-e", script], tty=None)


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


def build_prepare_ssh_dir_args(name: str) -> list[str]:
    """Build argv creating the guest SSH directory with private permissions.

    :param name: Sandbox name.
    :returns: Argv list for :class:`msb_circus.msb.MsbInvoker`.
    """
    script = f"mkdir -p {SSH_GUEST_DIR} && chmod 700 {SSH_GUEST_DIR}"
    return build_exec_args(name, ["sh", "-c", script], tty=None)


def build_remove_guest_symlink_args(name: str, guest_path: str) -> list[str]:
    """Build argv removing *guest_path* in the guest if it is a symlink.

    Used to repair destinations where an earlier ``msb copy`` of a host
    symlink left a symlink behind. Regular files and directories are
    never touched. The script always exits 0 when the path is not a
    symlink.

    :param name: Sandbox name.
    :param guest_path: Absolute guest path of the copy destination.
    :returns: Argv list for :class:`msb_circus.msb.MsbInvoker`.
    """
    script = 'if [ -L "$1" ]; then rm -f "$1"; fi'
    return build_exec_args(name, ["sh", "-c", script, "sh", guest_path], tty=None)


def build_host_cas_probe_args(name: str) -> list[str]:
    """Build argv reporting whether msb shipped the host CA bundle.

    The script prints ``present`` when the bundle exists and is not
    empty, otherwise ``absent``. Printing a word (instead of relying on
    the exit code) separates the answer from msb exec failures.

    :param name: Sandbox name.
    :returns: Argv list for :class:`msb_circus.msb.MsbInvoker`.
    """
    script = f"if [ -s {HOST_CAS_GUEST_PATH} ]; then echo present; else echo absent; fi"
    return build_exec_args(name, ["sh", "-c", script], tty=None)


def build_ssh_agent_probe_args(name: str) -> list[str]:
    """Build argv listing the forwarded agent's keys inside the guest.

    ``ssh-add -l`` exits 0 when keys are listed, 1 when the agent has no
    identities, and 2 when it cannot connect to the agent.

    :param name: Sandbox name.
    :returns: Argv list for :class:`msb_circus.msb.MsbInvoker`.
    """
    return build_exec_args(
        name,
        ["ssh-add", "-l"],
        env={"SSH_AUTH_SOCK": SSH_AGENT_GUEST_SOCKET},
        tty=None,
    )


def _gitconfig_script() -> str:
    """Return the guest script writing the managed git config.

    Arguments: ``$1`` managed file content, ``$2`` managed file path,
    ``$3`` global gitconfig path. Creates the parent directory of ``$2``,
    writes ``$1`` there atomically, and inserts the include block at the
    top of ``$3`` unless its include line already exists.

    :returns: Script text; run it as ``sh -c SCRIPT sh CONTENT MANAGED GLOBAL``.
    """
    return (
        "set -e\n"
        "managed=$2\n"
        "global=$3\n"
        'mkdir -p "$(dirname "$managed")" "$(dirname "$global")"\n'
        'printf "%s" "$1" > "$managed.tmp"\n'
        'mv "$managed.tmp" "$managed"\n'
        'line=$(printf "\tpath = %s" "$managed")\n'
        'if [ -f "$global" ] && grep -qxF "$line" "$global"; then exit 0; fi\n'
        'tmp="$global.msb-circus.tmp"\n'
        "{\n"
        '  printf "%s\\n" "# msb-circus: include the managed git config (do not edit this block)"\n'
        '  printf "%s\\n" "[include]"\n'
        '  printf "%s\\n" "$line"\n'
        '  if [ -f "$global" ]; then cat "$global"; fi\n'
        '} > "$tmp"\n'
        'mv "$tmp" "$global"\n'
    )


def build_write_gitconfig_args(name: str, content: str) -> list[str]:
    """Build argv writing the managed git config and its include block.

    :param name: Sandbox name.
    :param content: Managed file text from
        :func:`msb_circus.gitconfig.render_managed_gitconfig`.
    :returns: Argv list for :class:`msb_circus.msb.MsbInvoker`.
    """
    return build_exec_args(
        name,
        [
            "sh",
            "-c",
            _gitconfig_script(),
            "sh",
            content,
            GIT_GUEST_MANAGED_CONFIG,
            GIT_GUEST_GLOBAL_CONFIG,
        ],
        tty=None,
    )


def build_pid1_probe_args(name: str) -> list[str]:
    """Build argv printing the guest's PID 1 command name.

    :param name: Sandbox name.
    :returns: Argv list for :class:`msb_circus.msb.MsbInvoker`.
    """
    return build_exec_args(name, ["cat", "/proc/1/comm"], tty=None)


def build_init_shutdown_args(name: str) -> list[str]:
    """Build argv asking the guest's s6 init to shut the sandbox down.

    ``msb stop`` does not reach PID 1 when an init took it over (msb
    0.7.13: the sandbox stays ``draining``). SIGTERM to PID 1 makes
    s6-overlay stop all services in order; the VM ends when PID 1 exits.
    Only use this when PID 1 is s6 (:data:`S6_PID1_COMM`): with msb's own
    agent as PID 1 it would kill the agent.

    :param name: Sandbox name.
    :returns: Argv list for :class:`msb_circus.msb.MsbInvoker`.
    """
    return build_exec_args(name, ["sh", "-c", "kill -TERM 1"], tty=None)


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


def _copy_bytes(
    src,
    dst,
    *,
    observer: Callable[[str, bytes], None] | None = None,
    direction: str = "",
) -> bool:
    """Copy *src* to *dst* in chunks until EOF.

    Reads use ``read1`` when available (buffered and raw streams) so a
    partially filled pipe is forwarded immediately. A blocking
    ``BufferedReader.read(n)`` would wait for the full chunk or EOF — an
    ACP peer that writes one small request and then waits for the reply
    would deadlock the stream. Sources without ``read1`` (e.g.
    :class:`io.BytesIO` in tests) fall back to ``read``.

    :param src: Source binary file-like object (blocking reads).
    :param dst: Destination binary file-like object.
    :param observer: Optional callback that receives each copied chunk.
    :param direction: Direction label passed to *observer*.
    :returns: ``True`` on clean EOF, ``False`` on an I/O error (e.g. a
        broken pipe because the peer went away).
    """
    try:
        read = getattr(src, "read1", None) or src.read
        while True:
            chunk = read(_COPY_CHUNK)
            if not chunk:
                return True
            if observer is not None:
                try:
                    observer(direction, chunk)
                except (OSError, ValueError) as error:
                    logger.warning("ACP trace observer failed: %s", error)
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
    observer: Callable[[str, bytes], None] | None = None,
    on_started: Callable[[subprocess.Popen], None] | None = None,
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
    :param observer: Optional callback for exact stream chunks.
    :param on_started: Optional callback after the child starts.
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
    if on_started is not None:
        on_started(proc)
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
            if not _copy_bytes(
                stdin,
                child_stdin,
                observer=observer,
                direction="client-to-agent",
            ):
                host_gone.set()
        finally:
            try:
                child_stdin.close()
            except OSError:
                pass
            stdin_done.set()

    def pump_errors() -> None:
        if not _copy_bytes(
            child_stderr,
            stderr,
            observer=observer,
            direction="agent-stderr",
        ):
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

    if not _copy_bytes(
        child_stdout,
        stdout,
        observer=observer,
        direction="agent-to-client",
    ):
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


#: Guest script installing an executable file atomically: ``$1`` content,
#: ``$2`` destination path.
_INSTALL_EXECUTABLE_SCRIPT = (
    "set -e\n"
    'tmp="$2.msb-circus.tmp"\n'
    'printf "%s" "$1" > "$tmp"\n'
    'chmod 755 "$tmp"\n'
    'mv -f "$tmp" "$2"\n'
)


def build_install_executable_args(name: str, content: str, path: str) -> list[str]:
    """Build argv writing an executable guest file atomically.

    :param name: Sandbox name.
    :param content: File content.
    :param path: Absolute guest destination path.
    :returns: Argv list for :class:`msb_circus.msb.MsbInvoker`.
    """
    return build_exec_args(
        name,
        ["sh", "-c", _INSTALL_EXECUTABLE_SCRIPT, "sh", content, path],
        tty=None,
    )
