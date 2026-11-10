"""Detect linked Git worktrees.

A linked worktree (``git worktree add``) keeps its Git metadata in the
main repository, outside the worktree directory. The sandbox only mounts
the workspace, so Git cannot work inside it, and msb-circus refuses to
start there.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from .config import CONFIG_DIR_NAME
from .exceptions import ConfigurationError

#: Worktree directory below the main checkout for worktrees made in the sandbox.
SANDBOX_WORKTREES_DIR = f"{CONFIG_DIR_NAME}/worktrees"


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


def find_missing_worktrees(workspace: Path) -> list[Path]:
    """List registered linked worktrees whose directory is missing on the host.

    Reads ``<workspace>/.git/worktrees/*/gitdir`` without running Git.
    Such worktrees were created in the sandbox outside every mount (they
    exist only in the guest) or were deleted without ``git worktree
    prune``. Unreadable entries are skipped.

    :param workspace: Absolute workspace directory (main checkout).
    :returns: Missing worktree directories, sorted.
    """
    registry = workspace / ".git" / "worktrees"
    if not registry.is_dir():
        return []
    missing: list[Path] = []
    for entry in sorted(registry.iterdir()):
        try:
            line = (entry / "gitdir").read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if not line:
            continue
        dot_git = Path(line)
        if not dot_git.is_absolute():
            dot_git = entry / dot_git
        worktree = Path(os.path.normpath(dot_git)).parent
        if not worktree.exists():
            missing.append(worktree)
    return sorted(missing)


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


def ensure_sandbox_worktrees_dir(workspace: Path) -> Path | None:
    """Create ``.agent-circus/worktrees/`` with a self-ignoring ``.gitignore``.

    Only acts in a main checkout (``<workspace>/.git`` is a directory).
    The ``.gitignore`` contains ``*``, so Git ignores the directory and
    its content without changes to the user's ignore files. An existing
    ``.gitignore`` is left unchanged.

    :param workspace: Absolute workspace directory.
    :returns: The worktree directory, or ``None`` outside a main checkout.
    :raises OSError: If the directory or file cannot be created.
    """
    if not (workspace / ".git").is_dir():
        return None
    directory = workspace / SANDBOX_WORKTREES_DIR
    directory.mkdir(parents=True, exist_ok=True)
    ignore = directory / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    return directory


def has_sandbox_worktrees(workspace: Path) -> bool:
    """Tell whether ``.agent-circus/worktrees/`` contains a subdirectory.

    :param workspace: Absolute workspace directory.
    :returns: ``True`` when at least one worktree directory exists.
    """
    directory = workspace / SANDBOX_WORKTREES_DIR
    try:
        return any(child.is_dir() for child in directory.iterdir())
    except OSError:
        return False


#: Guest path of the git wrapper; precedes /usr/bin in the image's PATH.
GIT_WRAPPER_GUEST_PATH = "/usr/local/bin/git"


def git_wrapper_script(guest_root: str = "", mirror_root: str = "") -> str:
    """Return the guest ``git`` wrapper script for one sandbox.

    Fills the workspace mount points into ``templates/git-wrapper.sh``.
    With both values set, the wrapper rejects worktrees of the repository
    at ``guest_root`` and suggests ``mirror_root`` instead, because only
    worktrees made there record paths that are valid on the host.

    :param guest_root: Guest mount point of the workspace (e.g.
        ``/workspace``); empty if unknown.
    :param mirror_root: Guest mount point of the git worktree mirror (the
        workspace host path); empty when the mirror is off.
    :returns: Script text.
    """
    template = (Path(__file__).parent / "templates" / "git-wrapper.sh").read_text(
        encoding="utf-8"
    )
    return template.replace("@GUEST_ROOT@", shlex.quote(guest_root)).replace(
        "@MIRROR_ROOT@", shlex.quote(mirror_root)
    )
