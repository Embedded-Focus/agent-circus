"""Find skill links that are broken on the host or in the sandbox.

Skill installers place the files in one directory (e.g. ``.agents/skills``)
and link each skill into the directories of the other harnesses. A link
works in the sandbox only when its target is visible through a sandbox
mount at the path the link names. Relative links between mounted
directories usually work; absolute links to host paths usually do not.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath

#: Project skill directories below the workspace that harnesses read.
PROJECT_SKILL_DIRS: tuple[str, ...] = (
    ".agents/skills",
    ".claude/skills",
    ".codex/skills",
    ".pi/skills",
    ".opencode/skills",
    ".opencode/skill",
)

#: User skill directories below ``$HOME`` that live inside the read-write
#: agent config mounts (``~/.claude``, ``~/.codex``).
CONFIG_MOUNT_SKILL_DIRS: tuple[str, ...] = (".claude/skills", ".codex/skills")


class LinkProblem(StrEnum):
    """Reason why a skill link does not work."""

    #: The target does not exist on the host either.
    MISSING = "target missing"
    #: The target exists on the host but not at that path in the sandbox.
    SANDBOX = "target not visible in the sandbox"


@dataclass(frozen=True)
class BrokenSkillLink:
    """A skill link that does not resolve.

    :ivar path: Host path of the link.
    :ivar target: Raw link target as stored in the link.
    :ivar problem: Why the link does not resolve.
    """

    path: Path
    target: str
    problem: LinkProblem


def _strip_options(guest: str) -> str:
    """Remove mount options such as ``:ro`` from a guest mount spec.

    :param guest: Guest mount specification, e.g. ``/root/x:ro``.
    :returns: The guest path alone.
    """
    return guest.split(":", 1)[0]


def _rebase(path: str, mounts: Iterable[tuple[str, str]]) -> str | None:
    """Translate *path* from the first to the second side of a mount pair.

    The pair with the longest matching source prefix wins; on a tie, the
    first pair in order wins.

    :param path: Normalized absolute path on the source side.
    :param mounts: ``(source root, destination root)`` pairs.
    :returns: The translated path, or ``None`` if no root covers *path*.
    """
    best: tuple[int, str] | None = None
    for source, dest in mounts:
        covers = path == source or path.startswith(source.rstrip("/") + "/")
        if covers and (best is None or len(source) > best[0]):
            rest = path[len(source) :].lstrip("/")
            best = (len(source), str(PurePosixPath(dest, rest)) if rest else dest)
    return None if best is None else best[1]


def find_broken_skill_links(
    skill_dirs: Iterable[Path], mounts: Sequence[tuple[str, str]]
) -> list[BrokenSkillLink]:
    """List top-level skill links that do not resolve on the host or guest.

    For each symlink directly below a skill directory, the check first
    tests the host. It then maps the link's location into the guest,
    resolves the link target there (absolute as is, relative against the
    guest location), and maps the result back to the host through the
    mounts. A target that no mount covers is reported as not visible.

    Shortcomings: only top-level entries are checked, not links inside a
    skill. Paths are compared lexically, so a symlink in a mount root or
    in the middle of a target path can cause a wrong result. Targets that
    the image itself provides (outside all mounts) are reported as not
    visible.

    :param skill_dirs: Host skill directories to scan; missing ones are
        skipped.
    :param mounts: Sandbox bind mounts as ``(host path, guest spec)``
        pairs; guest specs may carry options such as ``:ro``.
    :returns: Broken links, sorted by host path.
    """
    to_guest = [
        (os.path.normpath(host), _strip_options(guest)) for host, guest in mounts
    ]
    to_host = [(guest, host) for host, guest in to_guest]
    broken: list[BrokenSkillLink] = []
    seen: set[Path] = set()
    for skill_dir in skill_dirs:
        try:
            entries = sorted(skill_dir.iterdir()) if skill_dir.is_dir() else []
        except OSError:
            continue
        for entry in entries:
            if entry in seen or not entry.is_symlink():
                continue
            seen.add(entry)
            try:
                target = os.readlink(entry)
            except OSError:
                continue
            if not entry.exists():
                broken.append(BrokenSkillLink(entry, target, LinkProblem.MISSING))
                continue
            guest_dir = _rebase(os.path.normpath(entry.parent), to_guest)
            if guest_dir is None:
                continue
            guest_target = os.path.normpath(os.path.join(guest_dir, target))
            host_target = _rebase(guest_target, to_host)
            if host_target is None or not Path(host_target).exists():
                broken.append(BrokenSkillLink(entry, target, LinkProblem.SANDBOX))
    return sorted(broken, key=lambda link: link.path)
