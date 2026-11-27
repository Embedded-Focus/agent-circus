"""Output formatting for the ``agent-circus volumes`` command.

msb volumes are host-level resources, so unlike ``ps`` the table always
lists every volume msb knows about; there is no per-workspace filter.
The table format follows ``agent-circus ps``: column-aligned cells
padded to the column width and separated by two spaces.
"""

from __future__ import annotations

import json
from typing import Any

from .sandbox import VolumeInfo

#: Placeholder for missing values in table cells.
_PLACEHOLDER = "-"


def format_capacity(capacity_bytes: int) -> str:
    """Format a byte count as an msb size string.

    :param capacity_bytes: Size in bytes.
    :returns: ``"<n>G"`` when the size is a whole number of GiB,
        otherwise ``"<n>M"`` (MiB, rounded down).
    """
    gib = 1024 * 1024 * 1024
    if capacity_bytes % gib == 0:
        return f"{capacity_bytes // gib}G"
    return f"{capacity_bytes // (1024 * 1024)}M"


def format_volume_table(volumes: list[VolumeInfo]) -> str:
    """Format volume entries as a column-aligned table.

    Columns are ``NAME``, ``KIND`` and ``SIZE``, where SIZE renders as
    an msb size string (``format_capacity``). A volume without a kind
    or capacity gets a ``-`` cell. Cells are padded to the column width
    and separated by two spaces; trailing padding is trimmed. An empty
    selection still prints the header row, like ``ps`` does.

    :param volumes: Parsed ``msb volume list`` entries.
    :returns: The table without a trailing newline.
    """
    header = ("NAME", "KIND", "SIZE")
    rows: list[tuple[str, str, str]] = [
        (
            volume.name,
            volume.kind or _PLACEHOLDER,
            format_capacity(volume.capacity_bytes)
            if volume.capacity_bytes is not None
            else _PLACEHOLDER,
        )
        for volume in volumes
    ]
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


def format_volume_json(volumes: list[VolumeInfo]) -> str:
    """Format volume entries as a JSON array.

    The shape is stable for scripting: every entry carries ``name``,
    ``kind`` and ``capacity_bytes`` keys, with ``null`` for values msb
    did not report.

    :param volumes: Parsed ``msb volume list`` entries.
    :returns: JSON document (2-space indent) without a trailing newline.
    """
    entries: list[dict[str, Any]] = [
        {
            "name": volume.name,
            "kind": volume.kind,
            "capacity_bytes": volume.capacity_bytes,
        }
        for volume in volumes
    ]
    return json.dumps(entries, indent=2)
