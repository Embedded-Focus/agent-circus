"""Output formatting for the ``msb-circus ps`` command.

The table format mirrors ``docker compose ps`` (and therefore
``agent-circus ps``): a column-aligned table with a header row and
docker-style relative creation times.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .sandbox import SandboxInfo

#: Seconds per minute/hour/day, kept as floats for duration arithmetic.
_SECONDS_PER_HOUR = 3600.0
_SECONDS_PER_DAY = 86400.0

#: Placeholder for missing values in table cells.
_PLACEHOLDER = "-"


def human_duration(seconds: float) -> str:
    """Format a duration in seconds the way ``docker ps`` does.

    Mirrors Docker's ``HumanDuration`` wording, so ``ps`` output reads
    familiar to container users.

    :param seconds: Non-negative duration in seconds.
    :returns: Human-readable duration (e.g. ``"42 seconds"``,
        ``"About an hour"``, ``"3 days"``).
    """
    whole = int(seconds)
    if whole < 0:
        return "<invalid>"
    if whole == 1:
        return "1 second"
    if whole < 60:
        return f"{whole} seconds"
    minutes = whole // 60
    if minutes == 1:
        return "About a minute"
    if minutes < 60:
        return f"{minutes} minutes"
    hours = int(seconds // _SECONDS_PER_HOUR)
    if hours == 1:
        return "About an hour"
    if hours < 48:
        return f"{hours} hours"
    if hours < 24 * 7 * 2:
        return f"{hours // 24} days"
    if hours < 24 * 30 * 2:
        return f"{hours // 24 // 7} weeks"
    if hours < 24 * 365 * 2:
        return f"{hours // 24 // 30} months"
    return f"{int(seconds // _SECONDS_PER_DAY // 365)} years"


def format_created(created_at: float | None, *, now: float) -> str:
    """Format a creation timestamp as a docker-style relative age.

    :param created_at: Epoch seconds of the creation time, or ``None``.
    :param now: Reference epoch seconds (usually the current time).
    :returns: Relative age (e.g. ``"2 minutes ago"``), or ``"-"`` when
        the timestamp is unknown.
    """
    if created_at is None:
        return _PLACEHOLDER
    return f"{human_duration(max(now - created_at, 0.0))} ago"


def format_port_entry(host: str, host_port: int, guest_port: int, protocol: str) -> str:
    """Format one port mapping the way ``podman ps`` does.

    :param host: Host bind address.
    :param host_port: Port on the host.
    :param guest_port: Port inside the sandbox.
    :param protocol: ``"tcp"`` or ``"udp"``.
    :returns: Mapping string (e.g. ``"127.0.0.1:8000->8000/tcp"``).
    """
    return f"{host}:{host_port}->{guest_port}/{protocol}"


def format_sandbox_table(
    sandboxes: list[SandboxInfo], *, now: float | None = None
) -> str:
    """Format sandbox entries as a ``docker compose ps``-style table.

    Columns are ``NAME``, ``ID``, ``CREATED``, ``STATUS``, ``PORTS``
    (podman places ``PORTS`` right before ``NAMES``; this table already
    leads with ``NAME``, so the slot after ``STATUS`` matches). The ID
    is truncated to 12 characters like ``docker ps`` does. Cells are
    padded to the column width and separated by two spaces; trailing
    padding is trimmed. Port mappings render podman-style and are
    comma-joined; a sandbox without published ports gets an empty cell.

    :param sandboxes: Entries to format.
    :param now: Reference time for the relative ages; defaults to the
        current time (injected for deterministic tests).
    :returns: The table without a trailing newline.
    """
    if now is None:
        now = time.time()
    header = ("NAME", "ID", "CREATED", "STATUS", "PORTS")
    rows: list[tuple[str, str, str, str, str]] = []
    for sandbox in sandboxes:
        rows.append(
            (
                sandbox.name,
                sandbox.id[:12] if sandbox.id else _PLACEHOLDER,
                format_created(sandbox.created_at, now=now),
                sandbox.status or _PLACEHOLDER,
                ", ".join(format_port_entry(*port) for port in sandbox.ports),
            )
        )
    widths = [
        max(len(header[i]), *(len(row[i]) for row in rows)) if rows else len(header[i])
        for i in range(len(header))
    ]
    lines = ["  ".join(h.ljust(w) for h, w in zip(header, widths)).rstrip()]
    lines.extend(
        "  ".join(cell.ljust(w) for cell, w in zip(row, widths)).rstrip()
        for row in rows
    )
    return "\n".join(lines)


def format_sandbox_json(sandboxes: list[SandboxInfo]) -> str:
    """Format sandbox entries as a JSON array.

    The shape is stable for scripting: every entry carries ``name``,
    ``id``, ``status``, ``created_at``, ``updated_at`` and ``ports``
    keys, with ``null`` for values msb did not report and an empty
    ``ports`` array when no port mappings are known.

    :param sandboxes: Entries to format.
    :returns: JSON document (2-space indent) without a trailing newline.
    """
    entries: list[dict[str, Any]] = [
        {
            "name": sandbox.name,
            "id": sandbox.id,
            "status": sandbox.status,
            "created_at": sandbox.created_at,
            "updated_at": sandbox.updated_at,
            "ports": [
                {
                    "host": host,
                    "host_port": host_port,
                    "guest_port": guest_port,
                    "protocol": protocol,
                }
                for host, host_port, guest_port, protocol in sandbox.ports
            ],
        }
        for sandbox in sandboxes
    ]
    return json.dumps(entries, indent=2)
