"""Detect linked Git worktrees.

A linked worktree (``git worktree add``) keeps its Git metadata in the
main repository, outside the worktree directory. The sandbox only mounts
the workspace, so Git cannot work inside it, and msb-circus refuses to
start there.
"""

from __future__ import annotations

from pathlib import Path

from .exceptions import ConfigurationError


def find_linked_worktree(path: Path) -> tuple[Path, Path] | None:
    """Find the linked Git worktree that contains ``path``.

    Walks up from ``path`` to the first ``.git`` entry. A ``.git``
    directory marks a regular repository. A ``.git`` file whose
    ``gitdir:`` target contains a ``commondir`` file marks a linked
    worktree; a ``.git`` file without it (e.g. a submodule) does not.
    Unreadable or malformed ``.git`` files count as "not a worktree".

    :param path: Absolute directory to start the search from.
    :returns: ``(worktree root, common Git directory)`` for a linked
        worktree, otherwise ``None``.
    """
    for directory in (path, *path.parents):
        dot_git = directory / ".git"
        if dot_git.is_dir():
            return None
        if dot_git.is_file():
            common_dir = _common_dir_of_git_file(dot_git)
            return None if common_dir is None else (directory, common_dir)
    return None


def _common_dir_of_git_file(dot_git: Path) -> Path | None:
    """Return the common Git directory a ``.git`` file points to.

    :param dot_git: Path of a ``.git`` file (``gitdir: <path>``).
    :returns: Resolved common Git directory when the target is a linked
        worktree's Git directory, otherwise ``None``.
    """
    try:
        first_line = dot_git.read_text(encoding="utf-8").splitlines()[0]
        if not first_line.startswith("gitdir:"):
            return None
        git_dir = dot_git.parent / first_line.removeprefix("gitdir:").strip()
        common = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError, IndexError):
        return None
    return (git_dir / common).resolve()


def ensure_not_linked_worktree(workspace: Path) -> None:
    """Reject a workspace that lies in a linked Git worktree.

    :param workspace: Absolute workspace directory.
    :raises ConfigurationError: If ``workspace`` is in a linked worktree.
    """
    found = find_linked_worktree(workspace)
    if found is None:
        return
    worktree, common_dir = found
    raise ConfigurationError(
        f"The workspace {workspace} is in the linked Git worktree {worktree}. "
        "msb-circus does not run in linked worktrees, because their Git "
        f"metadata lives in {common_dir}, outside the sandbox. Run "
        "msb-circus from the main checkout and manage worktrees there."
    )
