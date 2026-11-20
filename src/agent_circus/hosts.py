"""Forward selected host ``/etc/hosts`` entries into the msb sandbox.

microsandbox offers no static DNS records and applies rootfs patches only
before the first boot, so msb-circus instead writes a managed block into
the guest's ``/etc/hosts`` via ``msb exec`` after every boot, start, and
reuse. Host-side changes therefore apply without re-creating the sandbox,
and guest lines outside the managed block are preserved. The parser and
pattern filter mirror the agent-circus behavior (``parse_hosts_file`` and
``filter_hosts``); the two tools must stay behaviorally identical.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import logging
import re
from collections.abc import Sequence

logger = logging.getLogger(__name__)

#: A parsed hosts-file line: the IP address and its names (primary name
#: plus aliases), in file order.
HostLine = tuple[str, tuple[str, ...]]

#: Guest names must be safe to write verbatim into the guest hosts file:
#: no whitespace, no comment character, no newline.
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")


def parse_hosts_text(text: str) -> list[HostLine]:
    """Parse hosts-file content into ``(ip, names)`` tuples.

    Text after ``#`` is ignored; blank lines and lines with fewer than two
    fields are skipped. No validation of IPs or names happens here.

    :param text: Hosts file content.
    :returns: One tuple per remaining line, in file order.
    """
    lines: list[HostLine] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        lines.append((parts[0], tuple(parts[1:])))
    return lines


def read_hosts_file(path: str) -> list[HostLine]:
    """Read and parse a hosts file.

    :param path: Absolute host path.
    :returns: Parsed lines; an empty list (with a warning) when the file
        cannot be read.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return parse_hosts_text(fh.read())
    except OSError as e:
        logger.warning(
            "Cannot read hosts file %s: %s; no host entries are forwarded", path, e
        )
        return []


def select_host_lines(lines: list[HostLine], patterns: Sequence[str]) -> list[HostLine]:
    """Return the lines with at least one name matching *patterns*.

    Plain patterns are case-insensitive fnmatch globs; ``re:``-prefixed
    patterns are case-insensitive regexes matched with :func:`re.search`.
    A matching line is returned with all its names (alias semantics).
    Duplicate lines are dropped, keeping the first occurrence.

    :param lines: Parsed hosts lines.
    :param patterns: Glob or ``re:``-prefixed regex patterns.
    :returns: Matching lines in input order.
    """
    glob_patterns = [p.lower() for p in patterns if not p.startswith("re:")]
    regexes = [
        re.compile(p[3:], re.IGNORECASE) for p in patterns if p.startswith("re:")
    ]

    def matches(name: str) -> bool:
        lowered = name.lower()
        return any(fnmatch.fnmatch(lowered, g) for g in glob_patterns) or any(
            rx.search(name) for rx in regexes
        )

    result: list[HostLine] = []
    seen: set[HostLine] = set()
    for line in lines:
        if line in seen:
            continue
        if any(matches(name) for name in line[1]):
            seen.add(line)
            result.append(line)
    return result


def validate_host_lines(lines: list[HostLine]) -> list[HostLine]:
    """Drop lines whose IP or names are unsafe to write into the guest.

    The IP must parse with :func:`ipaddress.ip_address` (IPv4 or IPv6);
    zone suffixes such as ``fe80::1%eth0`` fail and drop the line. Names
    must consist of letters, digits, underscores, dots, and hyphens only.

    :param lines: Selected hosts lines.
    :returns: Lines with a valid IP address and only valid names; each
        dropped line is logged as a warning.
    """
    valid: list[HostLine] = []
    for ip, names in lines:
        reason: str | None = None
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            reason = f"invalid IP address {ip!r}"
        else:
            bad = [name for name in names if not _NAME_RE.fullmatch(name)]
            if bad:
                reason = f"invalid name(s) {', '.join(bad)}"
        if reason is not None:
            names_text = " ".join(names)
            logger.warning("Skipping hosts line %s %s: %s", ip, names_text, reason)
            continue
        valid.append((ip, names))
    return valid


def loopback_names(lines: list[HostLine]) -> list[str]:
    """Return the names of lines whose IP is a loopback address.

    :param lines: Validated hosts lines.
    :returns: Names in order (all names of each loopback line).
    """
    names: list[str] = []
    for ip, line_names in lines:
        if ipaddress.ip_address(ip).is_loopback:
            names.extend(line_names)
    return names


def render_host_block(lines: list[HostLine]) -> str:
    """Render lines as ``"<ip> <name> ..."`` text joined by newlines.

    :param lines: Validated hosts lines.
    :returns: Rendered text without trailing newline (the guest script
        adds it) and without markers; empty string for no lines.
    """
    return "\n".join(" ".join((ip, *names)) for ip, names in lines)
