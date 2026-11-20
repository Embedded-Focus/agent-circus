"""Guest log sources and the guest scripts behind ``agent-circus logs``.

The sandbox writes several log files inside the guest (see the source
table below); the kernel ring buffer is reachable through ``dmesg``.
This module describes those sources and renders POSIX ``sh`` scripts
for the two guest-side jobs: listing existing log files, and attributing
every line to its source (``[label] message``) with plain shell read
loops — the guest needs nothing beyond ``tail`` and ``dmesg``.

Presentation transforms happen host-side in Python:
:func:`normalize_log_line` moves the stamps that sources embed into
their lines to a podman-style leading position; it does not sort lines.
Historical output is grouped by source — kernel first as a display
convention — and is not merged chronologically; follow mode preserves
order within one source but interleaves sources by arrival. All scripts
run inside the guest via ``msb exec``; nothing here talks to msb itself.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

#: Name of the kernel ring buffer source (``dmesg``, not a file).
KERNEL_SOURCE_NAME = "kernel"


@dataclass(frozen=True)
class LogSource:
    """One selectable log source of the sandbox VM.

    :ivar name: Name used on the ``agent-circus logs`` command line.
    :ivar description: One-line description for ``--list`` and errors.
    :ivar pattern: Guest file path or glob of the log files; ``None``
        for non-file sources (the kernel).
    :ivar glob: Whether *pattern* is a glob matching several files.
    :ivar label_prefix: Label inside the square brackets. Fixed sources
        label their lines ``[<label_prefix>]``; glob sources label them
        ``[<label_prefix>.<wildcard match>]``.
    :ivar per_file: For glob sources: POSIX sh statements run for every
        matched file with ``$f`` set to its path; must print the file's
        labeled lines (typically by calling the script's ``prefix``
        helper). Unused for fixed sources.
    """

    name: str
    description: str
    pattern: str | None = None
    glob: bool = False
    label_prefix: str = ""
    per_file: str = ""


#: The kernel ring buffer source. Not a file: the guest script runs
#: ``dmesg`` for it. Printed first as a display convention: its lines
#: cover the boot and carry stamps relative to guest boot (``[    0.064705]``),
#: not wall-clock timestamps.
KERNEL_SOURCE = LogSource(
    name=KERNEL_SOURCE_NAME,
    description="Guest kernel ring buffer (dmesg, boot-relative stamps)",
)

#: Guest log sources of the sandbox in the canonical presentation order
#: of ``agent-circus logs``: kernel first, then bridge, dockerd, hooks and
#: daemons. This order is a display convention, not a claim about exact
#: event times: the bridge and dockerd s6 services start independently,
#: so their relative startup order is not guaranteed. Every source
#: except the kernel is a guest file path or glob; glob sources derive
#: a per-file label guest-side.
GUEST_LOG_SOURCES: tuple[LogSource, ...] = (
    LogSource(
        name="bridge",
        label_prefix="bridge",
        description="SSH agent bridge (s6 service circus-ssh-agent-bridge)",
        pattern="/tmp/ssh-agent-bridge.log",
    ),
    LogSource(
        name="dockerd",
        label_prefix="dockerd",
        description="Docker engine daemon (s6 service circus-dockerd)",
        pattern="/tmp/dockerd.log",
    ),
    LogSource(
        name="hooks",
        label_prefix="hook",
        description="Provision and startup hooks",
        pattern="/var/log/agent-circus/*.log",
        glob=True,
        per_file='n=${f##*/}; n=${n%.log}; prefix "[hook.$n]" "$f"',
    ),
    LogSource(
        name="daemons",
        label_prefix="daemon",
        description="User daemons ([[daemons]], s6-log managed)",
        pattern="/var/log/circus-daemons/*/current",
        glob=True,
        per_file=(
            "n=${f#/var/log/circus-daemons/}; n=${n%/current};"
            ' prefix "[daemon.$n]" "$f"'
        ),
    ),
)


def resolve_log_sources(names: Sequence[str] | None) -> tuple[LogSource, ...]:
    """Resolve the source names of a ``logs`` invocation.

    The kernel source (``dmesg``) is a regular source: it is part of the
    default selection and can be named explicitly. ``None`` or an empty
    *names* selects every source. The kernel is always first, followed
    by the file sources in canonical presentation order — argument order
    does not change the presentation order.

    :param names: Source names from the command line; ``None`` or empty
        selects every source.
    :returns: The selected sources in canonical presentation order,
        kernel first.
    :raises ValueError: If a name is not a known source.
    """
    valid = [source.name for source in GUEST_LOG_SOURCES] + [KERNEL_SOURCE_NAME]
    if not names:
        return (KERNEL_SOURCE, *GUEST_LOG_SOURCES)
    unknown = [name for name in names if name not in valid]
    if unknown:
        raise ValueError(
            "unknown log source(s): "
            + ", ".join(unknown)
            + "; valid sources: "
            + ", ".join(valid)
        )
    selected = [source for source in GUEST_LOG_SOURCES if source.name in names]
    if KERNEL_SOURCE_NAME in names:
        selected.insert(0, KERNEL_SOURCE)
    return tuple(selected)


def log_label(path: str) -> str:
    """Return the display label for a guest log file path.

    Fixed sources match their pattern exactly and label their lines
    ``[<label_prefix>]``. Glob sources match their pattern around one
    wildcard; the wildcard match becomes the suffix, e.g.
    ``[daemon.<name>]`` or ``[hook.<name>]``. Unknown paths fall back to
    ``[<basename>]``.

    :param path: Guest log file path as discovered in the guest.
    :returns: The label without a trailing space.
    """
    for source in GUEST_LOG_SOURCES:
        if source.pattern is None or not source.glob:
            continue
        head, _, tail = source.pattern.partition("*")
        if (
            path.startswith(head)
            and path.endswith(tail)
            and len(path) > len(head) + len(tail)
        ):
            inner = path[len(head) : len(path) - len(tail)]
            return f"[{source.label_prefix}.{inner}]"
    for source in GUEST_LOG_SOURCES:
        if source.pattern is not None and not source.glob and path == source.pattern:
            return f"[{source.label_prefix}]"
    return f"[{Path(path).name}]"


#: Matches a leading s6-log stamp with an optional ``[label] `` prefix
#: in front, e.g. ``[daemon.web] 2026-11-15 10:43:39.777605362  message``.
#: s6-log's ``T`` action stamps lines in local system time without an
#: offset; the generated logger pins ``TZ=UTC0``
#: (see :func:`agent_circus.daemons.render_log_run_script`), so ``+00:00``
#: is correct for lines it wrote. Lines written by an older logger under
#: a different timezone stay ambiguous.
_S6_STAMP_RE = re.compile(
    r"^(?P<pre>(?:\[[^\]]+\] )?)"
    r"(?P<date>\d{4}-\d{2}-\d{2}) (?P<clock>\d{2}:\d{2}:\d{2}\.\d+) +"
    r"(?P<rest>\S.*)$"
)

#: Matches a logrus-style ``time="..."`` prefix (dockerd) with an
#: optional ``[label] `` prefix in front.
_LOGRUS_STAMP_RE = re.compile(
    r"^(?P<pre>(?:\[[^\]]+\] )?)time=\"(?P<ts>[^\"]*)\" (?P<rest>.*)$"
)


def normalize_log_line(line: str) -> str:
    """Move an embedded timestamp to a podman-style leading position.

    Two stamp shapes are recognized (both with an optional ``[label] ``
    prefix in front): a leading s6-log stamp
    (``2026-11-15 08:11:33.743545709 message``) is reformatted to
    ``2026-11-15T08:11:33.743545709+00:00``, and a logrus-style
    ``time="..."`` prefix (dockerd) is stripped to the bare value.
    s6-log stamps carry no timezone; the ``+00:00`` offset is correct
    because the generated logger pins ``TZ=UTC0`` (see
    :func:`agent_circus.daemons.render_log_run_script`). Lines written by
    an older logger under another timezone stay ambiguous. Lines
    without a recognizable stamp print unchanged; no timestamp is
    invented, and lines are never sorted.

    :param line: One log line, as emitted by the guest.
    :returns: The normalized line.
    """
    match = _S6_STAMP_RE.match(line)
    if match:
        pre = match["pre"] or ""
        return f"{match['date']}T{match['clock']}+00:00 {pre}{match['rest']}"
    match = _LOGRUS_STAMP_RE.match(line)
    if match:
        pre = match["pre"] or ""
        return f"{match['ts']} {pre}{match['rest']}"
    return line


def _label_loop(label_expr: str) -> str:
    """Render a POSIX sh read loop prefixing every line with *label_expr*.

    :param label_expr: Shell word for the label, already quoted by the
        caller, e.g. ``"$1"`` (function argument), ``"$l"`` (a loop
        variable) or a ``shlex.quote``-d literal.
    :returns: The complete loop text.
    """
    return f"while IFS= read -r line; do printf '%s %s\\n' {label_expr} \"$line\"; done"


def build_logs_script(
    sources: Sequence[LogSource], lines: int, *, labels: bool = True
) -> str:
    """Render the guest script printing the selected logs.

    With labels (the default), every line of every existing, readable
    log file is printed as ``[label] message`` via a shell read loop;
    the kernel source labels ``dmesg`` output the same way. Without
    labels, each file is printed under a ``==> path <==`` header (the
    ``tail`` multi-file style) followed by its plain lines; the kernel
    prints under a ``==> kernel (dmesg) <==`` header. Unreadable or
    missing files are skipped silently in both modes. The script only
    attributes lines; timestamp normalization (``--timestamps``) is a
    host-side transform applied to the captured output.

    :param sources: Sources to print, in order; the kernel is the source
        without a file pattern.
    :param lines: Number of trailing lines per file (at least 1).
    :param labels: Prefix every line with its source label.
    :returns: The complete guest script text with a final newline.
    """
    n = max(int(lines), 1)
    prefix_loop = _label_loop('"$1"')
    parts = ["set -eu", f"LINES={n}"]
    if labels:
        parts.extend(
            [
                "# Print the last LINES lines of a file, each line prefixed",
                "# with a label. $1 = label, $2 = file; unreadable prints nothing.",
                "prefix() {",
                f'    tail -n "$LINES" -- "$2" 2>/dev/null | {prefix_loop}',
                "}",
            ]
        )
    else:
        parts.extend(
            [
                "# Print the last LINES lines of a file under a tail-style header.",
                "# $1 = file; an unreadable file prints nothing.",
                "plain() {",
                '    if [ -r "$1" ]; then',
                '        echo "==> $1 <=="',
                '        tail -n "$LINES" -- "$1" 2>/dev/null || :',
                "    fi",
                "}",
            ]
        )
    for source in sources:
        if source.pattern is None:
            # The kernel: limited dmesg output, labeled or under a header.
            if labels:
                kernel_label = shlex.quote(f"[{KERNEL_SOURCE_NAME}]")
                parts.extend(
                    [
                        "if dmesg >/dev/null 2>&1; then",
                        (
                            f'    dmesg 2>/dev/null | tail -n "$LINES"'
                            f" | while IFS= read -r line; do"
                            f" printf '%s %s\\n' {kernel_label} \"$line\"; done"
                        ),
                        "fi",
                    ]
                )
            else:
                parts.extend(
                    [
                        "if dmesg >/dev/null 2>&1; then",
                        f'    echo "==> {KERNEL_SOURCE_NAME} (dmesg) <=="',
                        '    dmesg 2>/dev/null | tail -n "$LINES" || :',
                        "fi",
                    ]
                )
        elif source.glob:
            if labels:
                parts.extend(
                    [
                        f"for f in {source.pattern}; do",
                        '    [ -f "$f" ] || continue',
                        f"    {source.per_file}",
                        "done",
                    ]
                )
            else:
                parts.extend(
                    [
                        f"for f in {source.pattern}; do",
                        '    [ -f "$f" ] || continue',
                        '    plain "$f"',
                        "done",
                    ]
                )
        elif labels:
            parts.append(
                f"prefix {shlex.quote(f'[{source.label_prefix}]')} "
                f"{shlex.quote(source.pattern)}"
            )
        else:
            parts.append(f"plain {shlex.quote(source.pattern)}")
    return "\n".join(parts) + "\n"


def build_log_files_script(sources: Sequence[LogSource]) -> str:
    """Render the guest script listing existing log file paths.

    Prints the guest path of every existing, readable log file of the
    given sources, one per line, in source order. Used to discover the
    files before following them.

    :param sources: File sources to scan, in order.
    :returns: The complete guest script text with a final newline.
    """
    patterns = " ".join(
        source.pattern for source in sources if source.pattern is not None
    )
    return (
        "set -eu\n"
        f"for f in {patterns}; do\n"
        '    [ -r "$f" ] || continue\n'
        "    printf '%s\\n' \"$f\"\n"
        "done\n"
    )


#: POSIX sh trap stopping every recorded background job (``$PIDS``)
#: when the follow script is interrupted.
_CLEANUP_TRAP = (
    "trap 'for p in $PIDS; do kill \"$p\" 2>/dev/null || :; done; exit 0' INT TERM HUP"
)


def _kernel_follow_statements(lines: int, label: str | None) -> list[str]:
    """Render the kernel follow statements for a follow script.

    Two stages avoid piping an endless stream into ``tail -n`` (which
    could wait forever because ``dmesg --follow`` never reaches end of
    file): first a limited ``dmesg`` snapshot prints the initial view,
    then ``dmesg --follow-new`` prints only records arriving after it
    started. The snapshot runs in the foreground, so the snapshot lines
    always precede the follower's lines; only a kernel record arriving
    between the two stages can be missed.

    :param lines: Number of trailing snapshot records to print.
    :param label: Label applied to both stages, or ``None`` for plain
        lines.
    :returns: Statements for the snapshot (foreground) and the follower
        (background, PID recorded in ``$PIDS``); the caller must define
        ``PIDS`` and install :data:`_CLEANUP_TRAP` first.
    """
    if label is None:
        snapshot = 'dmesg 2>/dev/null | tail -n "$LINES"'
        follow = "dmesg --follow-new 2>/dev/null"
    else:
        quoted = shlex.quote(label)
        loop = _label_loop(quoted)
        snapshot = f'dmesg 2>/dev/null | tail -n "$LINES" | {loop}'
        follow = f"dmesg --follow-new 2>/dev/null | {loop}"
    return [
        snapshot,
        f"{follow} &",
        'PIDS="$PIDS $!"',
    ]


def build_follow_logs_script(
    pairs: Sequence[tuple[str, str]], lines: int, *, kernel: bool = False
) -> str:
    """Render the guest script following log sources with labeled lines.

    Starts one background ``tail -F`` pipeline per ``(label, path)``
    pair — a shell read loop prefixes each line with the label — and,
    when *kernel* is set, the two-stage kernel follower (snapshot plus
    ``dmesg --follow-new``, see :func:`_kernel_follow_statements`); a
    trap stops all background jobs on ``INT``, ``TERM`` and ``HUP``.
    ``tail -F`` follows the file name, so s6-log rotation keeps
    streaming.

    Shortcomings: the pipelines are separate processes sharing one
    stdout, so output can interleave between them — order is preserved
    within one source, sources arrive by chance — and a label is only
    as correct as the ``log_label`` mapping done on the host.

    :param pairs: ``(label, path)`` tuples; the label includes the
        brackets (see :func:`log_label`).
    :param lines: Number of trailing lines to print per source at start.
    :param kernel: Also follow the kernel ring buffer.
    :returns: The complete guest script text with a final newline; the
        pairs are passed as positional arguments after the script.
    """
    follow_loop = _label_loop('"$l"')
    parts = [
        "set -eu",
        f"LINES={max(int(lines), 1)}",
        "PIDS=",
        _CLEANUP_TRAP,
        'for x in "$@"; do',
        "    l=${x%%|*}",
        "    f=${x#*|}",
        f'    tail -F -n "$LINES" -- "$f" 2>/dev/null | {follow_loop} &',
        '    PIDS="$PIDS $!"',
        "done",
    ]
    if kernel:
        parts.extend(_kernel_follow_statements(lines, f"[{KERNEL_SOURCE_NAME}]"))
    parts.append("wait")
    return "\n".join(parts) + "\n"


def build_plain_follow_script(lines: int, *, kernel: bool = True) -> str:
    """Render the guest script following files (and the kernel) unlabeled.

    Used for ``logs -f --no-labels``: one plain ``tail -F`` follows the
    discovered files (passed as positional arguments; skipped when there
    are none) and, when *kernel* is set, the two-stage kernel follower
    (see :func:`_kernel_follow_statements`) joins it; a trap stops all
    background jobs on ``INT``, ``TERM`` and ``HUP``. Timestamp
    normalization is a host-side transform. See
    :func:`build_follow_logs_script` for the caveats.

    :param lines: Number of trailing lines to print per source at start.
    :param kernel: Also follow the kernel ring buffer.
    :returns: The complete guest script text with a final newline.
    """
    parts = [
        "set -eu",
        f"LINES={max(int(lines), 1)}",
        "PIDS=",
        _CLEANUP_TRAP,
        'if [ "$#" -gt 0 ]; then',
        '    tail -F -n "$LINES" -- "$@" &',
        '    PIDS="$PIDS $!"',
        "fi",
    ]
    if kernel:
        parts.extend(_kernel_follow_statements(lines, None))
    parts.append("wait")
    return "\n".join(parts) + "\n"


def parse_log_files_output(text: str) -> list[str]:
    """Parse the output of :func:`build_log_files_script`.

    Returns the non-empty lines in order, without duplicates. The script
    prints one guest path per line, so any non-empty line is taken
    verbatim as a path.

    :param text: Raw script output.
    :returns: Guest log file paths.
    """
    files: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line and line not in files:
            files.append(line)
    return files
