"""System skills store and installer sandbox helpers.

System skills are skills that every msb-circus sandbox sees. They live in
a host-side store (a "fake home" that the ``skills`` CLI writes) and are
installed by the ``skills`` CLI inside a short-lived installer sandbox,
so the host needs no Node.js. Agent sandboxes mount the store's skill
directory read-only; the installer sandbox is the only sandbox with
write access to the store.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from .exceptions import ConfigurationError

#: Name of the short-lived installer sandbox. It does not use the
#: ``agents-`` prefix, so ps and the dashboard do not treat it as a
#: project sandbox.
INSTALLER_SANDBOX_NAME = "circus-skills"
#: Guest home of the installer; the store's ``.agents`` is mounted below.
INSTALLER_GUEST_AGENTS_DIR = "/root/.agents"
#: Guest path of the read-only system skill mount in agent sandboxes.
SYSTEM_SKILLS_GUEST_DIR = "/root/.agents/skills"
#: Environment for the installer: no telemetry.
INSTALLER_ENV = {"DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"}
#: Subcommands that get ``-g`` added.
GLOBAL_SUBCOMMANDS = frozenset({"add", "remove", "rm", "list", "ls", "update"})
#: Subcommands passed through without ``-g``.
PLAIN_SUBCOMMANDS = frozenset({"check", "find"})
#: Pass-through flags (no ``-g`` needed; they do not touch the store).
PASSTHROUGH_FLAGS = frozenset({"--help", "-h", "--version", "-v"})

#: Default store location below ``$HOME`` when ``XDG_DATA_HOME`` is unset.
_DEFAULT_DATA_HOME = ".local/share"


def system_skills_store(
    configured: str | None, environ: Mapping[str, str] | None = None
) -> Path:
    """Resolve the host path of the system skill store.

    Precedence: the configured ``[msb.skills] store`` value (``~`` is
    expanded, must be absolute after expansion), then
    ``$XDG_DATA_HOME/msb-circus/system-skills`` (only when absolute; the
    XDG spec says to ignore relative values), then
    ``$HOME/.local/share/msb-circus/system-skills``.

    :param configured: Configured store path, or ``None`` for the default.
    :param environ: Environment mapping read for ``XDG_DATA_HOME`` and
        ``HOME``; defaults to :data:`os.environ` so tests can pass a dict.
    :returns: Resolved store path (not created).
    :raises ConfigurationError: If the configured path is invalid (see
        :func:`expand_store_path`).
    """
    if configured is not None:
        return expand_store_path(configured)
    env = os.environ if environ is None else environ
    data_home = env.get("XDG_DATA_HOME")
    # The XDG Base Directory spec says to ignore relative values.
    if data_home and Path(data_home).is_absolute():
        base = Path(data_home)
    else:
        base = Path(env.get("HOME", str(Path.home()))) / _DEFAULT_DATA_HOME
    return base / "msb-circus" / "system-skills"


def expand_store_path(configured: str) -> Path:
    """Expand and validate a configured ``[msb.skills] store`` path.

    :param configured: Configured store path; ``~`` and ``~user`` are
        expanded.
    :returns: The expanded absolute path.
    :raises ConfigurationError: If ``~user`` names an unknown user or
        the path is relative after expansion.
    """
    try:
        path = Path(configured).expanduser()
    except RuntimeError as e:
        raise ConfigurationError(
            f"msb.skills.store cannot be expanded ({configured!r}): {e}"
        ) from e
    if not path.is_absolute():
        raise ConfigurationError(
            f"msb.skills.store must be an absolute path (got {configured!r})"
        )
    return path


def installer_lock_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the host path of the skills installer lock file.

    ``$XDG_STATE_HOME/msb-circus/skills-installer.lock``; if
    ``XDG_STATE_HOME`` is unset, empty or relative (the XDG spec says to
    ignore relative values), ``$HOME/.local/state/msb-circus/skills-installer.lock``.

    :param environ: Environment mapping read for ``XDG_STATE_HOME`` and
        ``HOME``; defaults to :data:`os.environ` so tests can pass a dict.
    :returns: Lock file path (not created).
    """
    env = os.environ if environ is None else environ
    state_home = env.get("XDG_STATE_HOME")
    if state_home and Path(state_home).is_absolute():
        base = Path(state_home)
    else:
        base = Path(env.get("HOME", str(Path.home()))) / ".local/state"
    return base / "msb-circus" / "skills-installer.lock"


def store_skills_dir(store: Path) -> Path:
    """Return the skill directory inside the store that agents see.

    :param store: Store root (see :func:`system_skills_store`).
    :returns: ``store/.agents/skills``.
    """
    return store / ".agents" / "skills"


def ensure_store(store: Path) -> Path:
    """Create the store's skill directory when it is missing.

    :param store: Store root.
    :returns: The created (or existing) skill directory.
    :raises OSError: If the directory cannot be created.
    """
    skills_dir = store_skills_dir(store)
    skills_dir.mkdir(parents=True, exist_ok=True)
    return skills_dir


def installer_mounts(store: Path) -> list[tuple[str, str]]:
    """Return the read-write mounts for the installer sandbox.

    The installer is the only sandbox that may write the store: its
    ``.agents`` directory (skills and the CLI's lock file) is mounted at
    the installer's ``~/.agents``, and the ``skills`` CLI is run with
    ``-g`` so it writes real copies there.

    :param store: Store root.
    :returns: ``(host path, guest path)`` pairs (read-write, no ``:ro``).
    """
    return [(str(store / ".agents"), INSTALLER_GUEST_AGENTS_DIR)]


def system_skill_mounts(store: Path) -> list[tuple[str, str]]:
    """Return the read-only system skill mounts for agent sandboxes.

    :param store: Store root.
    :returns: ``(host path, guest spec)`` pairs; the guest path carries
        the ``:ro`` option. msb enforces read-only on the host side.
    """
    return [(str(store_skills_dir(store)), f"{SYSTEM_SKILLS_GUEST_DIR}:ro")]


def build_skills_command(args: Sequence[str]) -> list[str]:
    """Build the ``skills`` CLI argv for the installer sandbox.

    Subcommands that write the store get ``-g`` inserted after the
    subcommand (skipped when ``-g``/``--global`` is already present);
    read-only subcommands and help/version flags pass through unchanged.
    Everything else is rejected: those commands work on a project
    directory, which the installer sandbox throws away after the run.

    :param args: Forwarded user arguments (subcommand first).
    :returns: argv starting with ``skills``.
    :raises ValueError: If *args* is empty or names a disallowed
        subcommand.
    """
    if not args:
        raise ValueError(
            "Usage: msb-circus skills add|remove|list|update|check|find "
            "[arguments]. Try 'msb-circus skills --help'."
        )
    subcommand = args[0]
    if subcommand in GLOBAL_SUBCOMMANDS:
        rest = args[1:]
        if "-g" in rest or "--global" in rest:
            return ["skills", subcommand, *rest]
        return ["skills", subcommand, "-g", *rest]
    if subcommand in PLAIN_SUBCOMMANDS or subcommand in PASSTHROUGH_FLAGS:
        return ["skills", *args]
    allowed = sorted(GLOBAL_SUBCOMMANDS | PLAIN_SUBCOMMANDS)
    raise ValueError(
        f"Unsupported skills subcommand {subcommand!r}; allowed: "
        + ", ".join(allowed)
        + " (project-scoped commands would write into the sandbox, "
        "which is thrown away after the run)"
    )
