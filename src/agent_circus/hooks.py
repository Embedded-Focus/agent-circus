"""Shell script generation and output parsing for project hooks.

msb-circus runs the two project hooks ``provision`` and ``startup``
inside the microsandbox VM ("guest") via ``msb exec <sandbox> -- sh -c
<script> ...``.  State lives in :data:`HOOK_STATE_DIR`: a ``<hook>.state``
file holds ``key=``, ``exit=`` and ``finished=`` lines.  ``provision`` is
due when its key differs from the SHA-256 of the user script (a failed
run keeps its old key so it stays due); ``startup`` is due when its key
differs from the guest boot id (it stores the boot id even on failure).
Script content and parameters travel in environment variables because
``msb exec`` has no stdin here.  Hook logs accumulate under
:data:`HOOK_LOG_DIR` as ``<hook>.log``.
"""

from __future__ import annotations

import base64
import hashlib
import shlex
from dataclasses import dataclass

#: Guest directory with hook state, scripts and locks.
HOOK_STATE_DIR = "/var/lib/msb-circus/hooks"
#: Guest directory with hook logs (``<hook>.log``).
HOOK_LOG_DIR = "/var/log/msb-circus"
#: Guest file with the boot id; it changes on every boot.
BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
#: Run/check script exit code when the runner itself failed.
EXIT_RUNNER_FAILED = 125
#: Exit code of ``timeout`` when the hook ran too long.
EXIT_TIMEOUT = 124
#: Environment variables that carry runner input (never user variables).
#: Not prefixed with ``MSB_``: msb reserves that prefix and rejects
#: ``--env`` values using it.
ENV_SCRIPT = "CIRCUS_HOOK_B64"
ENV_KEY = "CIRCUS_HOOK_KEY"
ENV_TIMEOUT = "CIRCUS_HOOK_TIMEOUT"
ENV_PROVISION_HASH = "CIRCUS_HOOK_PROVISION_HASH"
ENV_STARTUP = "CIRCUS_HOOK_STARTUP"

#: Hook names accepted by the run script and the argument builders.
HOOK_NAMES = ("provision", "startup")
#: Valid check result words per hook in the check script output.
_CHECK_STATES = {
    "provision": frozenset(("new", "changed")),
    "startup": frozenset(("due",)),
}


def _q(x: str) -> str:
    """Quote a string for safe use in a POSIX shell command.

    :param x: String to quote.
    :returns: The quoted string.
    """
    return shlex.quote(x)


def hook_key_hash(content: str) -> str:
    """Return the SHA-256 hex digest of the UTF-8 script content.

    :param content: User script content.
    :returns: Hex digest string.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def log_path(hook: str, log_dir: str = HOOK_LOG_DIR) -> str:
    """Return the guest log file path of a hook.

    :param hook: Hook name.
    :param log_dir: Guest log directory.
    :returns: The log file path, ``f"{log_dir}/{hook}.log"``.
    """
    return f"{log_dir}/{hook}.log"


def build_check_env(provision_hash: str | None, startup: bool) -> dict[str, str]:
    """Build the environment for the check script.

    :param provision_hash: Provision hash to compare against, or None to
        skip the provision check.
    :param startup: Whether to check the startup hook.
    :returns: Environment variables carrying the check parameters.
    """
    env: dict[str, str] = {}
    if provision_hash is not None:
        env[ENV_PROVISION_HASH] = provision_hash
    if startup:
        env[ENV_STARTUP] = "1"
    return env


def build_check_script(
    *, state_dir: str = HOOK_STATE_DIR, boot_id_file: str = BOOT_ID_FILE
) -> str:
    """Render a POSIX ``sh`` script that reports which hooks are due.

    The script prints one ``<hook> <state>`` line per due hook:
    ``provision new`` / ``provision changed`` when the stored provision
    key differs from the hash in the environment, and ``startup due``
    when the stored startup key differs from the boot id.

    :param state_dir: Guest directory with hook state.
    :param boot_id_file: Guest file with the boot id.
    :returns: The complete check script text with a final newline.
    """
    d = _q(state_dir)
    b = _q(boot_id_file)
    return f"""d={d}
boot=$(cat {b}) || exit {EXIT_RUNNER_FAILED}
key_of() {{ sed -n 's/^key=//p' "$d/$1.state" 2>/dev/null; }}
if [ -n "${{{ENV_PROVISION_HASH}:-}}" ]; then
  stored=$(key_of provision)
  if [ -z "$stored" ]; then
    echo "provision new"
  elif [ "$stored" != "${ENV_PROVISION_HASH}" ]; then
    echo "provision changed"
  fi
fi
if [ -n "${{{ENV_STARTUP}:-}}" ] && [ "$(key_of startup)" != "$boot" ]; then
  echo "startup due"
fi
exit 0
"""


def parse_check_output(text: str) -> dict[str, str]:
    """Parse the output of :func:`build_check_script`.

    :param text: Raw script output.
    :returns: Mapping with a subset of ``{"provision": "new"|"changed",
        "startup": "due"}``.  Blank lines and lines that do not have
        exactly two words with a known hook name are ignored.
    """
    results: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        hook, state = parts
        if hook in _CHECK_STATES and state in _CHECK_STATES[hook]:
            results[hook] = state
    return results


def build_run_env(content: str, key: str | None, timeout: int | None) -> dict[str, str]:
    """Build the environment for the run script.

    :param content: User script content.
    :param key: Key to store for provision on success, or None (startup
        derives the key from the boot id itself).
    :param timeout: Guest timeout in seconds, or None for no timeout.
    :returns: Environment variables carrying the runner input.
    """
    return {
        ENV_SCRIPT: base64.b64encode(content.encode("utf-8")).decode("ascii"),
        ENV_KEY: key or "",
        ENV_TIMEOUT: str(timeout) if timeout else "",
    }


def build_run_script(
    *,
    state_dir: str = HOOK_STATE_DIR,
    log_dir: str = HOOK_LOG_DIR,
    boot_id_file: str = BOOT_ID_FILE,
) -> str:
    """Render a POSIX ``sh`` script that runs one hook in the guest.

    The script is invoked as ``sh -c <script> sh <hook> <ensure|force>``.
    It takes a per-hook ``flock``, writes the user script to
    ``<hook>.sh``, runs it (directly when it starts with ``#!``, else
    with ``bash``), optionally under ``timeout``, sends all output to
    stderr and to the hook log, writes the state file and exits with the
    script's exit code.  A skipped (not due, not forced) run exits 0
    without touching the state.

    :param state_dir: Guest directory with hook state.
    :param log_dir: Guest directory with hook logs.
    :param boot_id_file: Guest file with the boot id.
    :returns: The complete run script text with a final newline.
    """
    d = _q(state_dir)
    logd = _q(log_dir)
    b = _q(boot_id_file)
    return f"""hook=$1
mode=$2
case "$hook" in provision|startup) ;; *) exit {EXIT_RUNNER_FAILED} ;; esac
d={d}
logd={logd}
mkdir -p "$d" "$logd" || exit {EXIT_RUNNER_FAILED}
exec 9>"$d/$hook.lock" || exit {EXIT_RUNNER_FAILED}
flock 9 || exit {EXIT_RUNNER_FAILED}
boot=$(cat {b}) || exit {EXIT_RUNNER_FAILED}
if [ "$hook" = provision ]; then key=${{{ENV_KEY}:-}}; else key=$boot; fi
[ -n "$key" ] || exit {EXIT_RUNNER_FAILED}
stored=$(sed -n 's/^key=//p' "$d/$hook.state" 2>/dev/null)
if [ "$mode" != force ] && [ "$stored" = "$key" ]; then exit 0; fi
printf '%s' "${{{ENV_SCRIPT}:-}}" | base64 -d > "$d/$hook.sh" || exit {EXIT_RUNNER_FAILED}
chmod 700 "$d/$hook.sh" || exit {EXIT_RUNNER_FAILED}
t=${{{ENV_TIMEOUT}:-}}
unset {ENV_SCRIPT} {ENV_KEY} {ENV_TIMEOUT}
if [ "$(head -c 2 "$d/$hook.sh")" = '#!' ]; then
  set -- "$d/$hook.sh"
else
  set -- bash "$d/$hook.sh"
fi
if [ -n "$t" ]; then set -- timeout "$t" "$@"; fi
rm -f "$d/$hook.rc"
{{ "$@" </dev/null 9>&-; echo $? > "$d/$hook.rc"; }} 2>&1 | tee "$logd/$hook.log" >&2
rc=$(cat "$d/$hook.rc" 2>/dev/null)
[ -n "$rc" ] || rc={EXIT_RUNNER_FAILED}
if [ "$rc" -eq 0 ] || [ "$hook" = startup ]; then new=$key; else new=$stored; fi
printf 'key=%s\\nexit=%s\\nfinished=%s\\n' "$new" "$rc" "$(date +%s)" > "$d/$hook.state.tmp" \\
  && mv -f "$d/$hook.state.tmp" "$d/$hook.state"
exit "$rc"
"""


def build_run_args(hook: str, force: bool) -> list[str]:
    """Return the argument list that runs one hook inside the guest.

    The list is meant for ``msb exec <sandbox> --``.

    :param hook: Hook name, ``provision`` or ``startup``.
    :param force: Whether to run even when the hook is not due.
    :returns: ``["sh", "-c", <run script>, "sh", hook, mode]``.
    :raises ValueError: If the hook name is unknown.
    """
    if hook not in HOOK_NAMES:
        raise ValueError(f"unknown hook: {hook!r}")
    return ["sh", "-c", build_run_script(), "sh", hook, "force" if force else "ensure"]


def build_status_script(
    *, state_dir: str = HOOK_STATE_DIR, boot_id_file: str = BOOT_ID_FILE
) -> str:
    """Render a POSIX ``sh`` script that reports the hook state.

    The script prints the boot id and one line per existing state file
    with the hook name and the flattened state fields.

    :param state_dir: Guest directory with hook state.
    :param boot_id_file: Guest file with the boot id.
    :returns: The complete status script text with a final newline.
    """
    d = _q(state_dir)
    b = _q(boot_id_file)
    return f"""d={d}
printf 'boot=%s\\n' "$(cat {b} 2>/dev/null)"
for h in provision startup; do
  [ -f "$d/$h.state" ] || continue
  printf '%s %s\\n' "$h" "$(tr '\\n' ' ' < "$d/$h.state")"
done
"""


@dataclass(frozen=True)
class HookStatus:
    """State of one hook, from the status script.

    :ivar name: Hook name.
    :ivar key: Stored key (``""`` when none).
    :ivar exit_code: Last exit code or None.
    :ivar finished: Epoch seconds of the last run or None.
    """

    name: str
    key: str
    exit_code: int | None
    finished: int | None


def parse_status_output(text: str) -> tuple[str | None, dict[str, HookStatus]]:
    """Parse the output of :func:`build_status_script`.

    :param text: Raw script output.
    :returns: Tuple of the boot id (None when missing or empty) and a
        mapping of hook name to its :class:`HookStatus`.  Unknown hook
        names and malformed lines are skipped; non-integer ``exit`` or
        ``finished`` values become None.
    """
    boot_id: str | None = None
    statuses: dict[str, HookStatus] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("boot="):
            value = line[len("boot=") :].strip()
            boot_id = value or None
            continue
        parts = line.split()
        if len(parts) != 4 or parts[0] not in HOOK_NAMES:
            continue
        fields: dict[str, str] = {}
        for part in parts[1:]:
            key, sep, value = part.partition("=")
            if not sep:
                fields.clear()
                break
            fields[key] = value
        if set(fields) != {"key", "exit", "finished"}:
            continue
        try:
            exit_code: int | None = int(fields["exit"])
        except ValueError:
            exit_code = None
        try:
            finished: int | None = int(fields["finished"])
        except ValueError:
            finished = None
        statuses[parts[0]] = HookStatus(parts[0], fields["key"], exit_code, finished)
    return boot_id, statuses


def build_logs_command(hook: str, lines: int, follow: bool) -> list[str]:
    """Return the guest command that tails a hook's log.

    The command is meant to run inside the guest (e.g. via
    ``msb exec <sandbox>``).

    :param hook: Hook name.
    :param lines: Number of trailing lines to print.
    :param follow: Whether to keep following the log file.
    :returns: The argument list for ``tail``.
    """
    cmd = ["tail", "-n", str(lines), "-F", log_path(hook)]
    if not follow:
        cmd.remove("-F")
    return cmd
