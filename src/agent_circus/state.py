"""Runtime state management for Agent Circus.

Manages per-workspace state that is transient and machine-managed,
stored under ``$XDG_STATE_HOME/agent-circus/<project>/``.  This
includes service reference counts and generated compose overrides.
"""

import fcntl
import json
import logging
import os
from pathlib import Path
from typing import Any

from .config import COMPOSE_SHADOW_FILE_NAME, sanitize_project_name

logger = logging.getLogger(__name__)

STATE_FILE_NAME = "state.json"
LOCK_FILE_NAME = "state.lock"

DEFAULT_STATE: dict[str, Any] = {
    "services": {},
}


def get_state_dir(workspace: Path) -> Path:
    """Get the runtime state directory for a workspace.

    Follows the XDG Base Directory Specification: uses
    ``$XDG_STATE_HOME/agent-circus/<project>/``, falling back to
    ``~/.local/state/agent-circus/<project>/`` when the environment
    variable is unset or empty.

    The directory is created if it does not exist.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to the state directory.
    :rtype: Path
    """
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    state_dir = base / "agent-circus" / sanitize_project_name(workspace.name)
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir


def get_shadow_override_path(workspace: Path) -> Path:
    """Get the path for the shadow compose override file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``compose.shadow.json`` in the state directory.
    :rtype: Path
    """
    return get_state_dir(workspace) / COMPOSE_SHADOW_FILE_NAME


def _read_state(state_dir: Path) -> dict[str, Any]:
    """Read state from disk.

    :param state_dir: State directory path.
    :type state_dir: Path
    :returns: Parsed state dictionary.
    :rtype: dict[str, Any]
    """
    state_file = state_dir / STATE_FILE_NAME
    if not state_file.is_file():
        return {"services": {}}
    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("Corrupt state file %s, resetting", state_file)
        return {"services": {}}


def _write_state(state_dir: Path, state: dict[str, Any]) -> None:
    """Write state to disk.

    :param state_dir: State directory path.
    :type state_dir: Path
    :param state: State dictionary to write.
    :type state: dict[str, Any]
    """
    state_file = state_dir / STATE_FILE_NAME
    state_file.write_text(json.dumps(state, indent=2) + "\n")


def acquire(workspace: Path, service: str) -> int:
    """Increment the reference count for a service.

    Acquires an exclusive file lock during the read-modify-write
    cycle to prevent races between concurrent ``exec`` invocations.

    :param workspace: Workspace path.
    :type workspace: Path
    :param service: Service name.
    :type service: str
    :returns: New reference count after incrementing.
    :rtype: int
    """
    state_dir = get_state_dir(workspace)
    lock_path = state_dir / LOCK_FILE_NAME

    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            state = _read_state(state_dir)
            services = state.setdefault("services", {})
            entry = services.setdefault(service, {"refs": 0})
            entry["refs"] += 1
            _write_state(state_dir, state)
            logger.debug("acquire %s: refs=%d", service, entry["refs"])
            return entry["refs"]
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


def release(workspace: Path, service: str) -> int:
    """Decrement the reference count for a service.

    The count is floored at zero.  Acquires an exclusive file lock
    during the read-modify-write cycle.

    :param workspace: Workspace path.
    :type workspace: Path
    :param service: Service name.
    :type service: str
    :returns: New reference count after decrementing.
    :rtype: int
    """
    state_dir = get_state_dir(workspace)
    lock_path = state_dir / LOCK_FILE_NAME

    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            state = _read_state(state_dir)
            services = state.setdefault("services", {})
            entry = services.setdefault(service, {"refs": 0})
            entry["refs"] = max(0, entry["refs"] - 1)
            _write_state(state_dir, state)
            logger.debug("release %s: refs=%d", service, entry["refs"])
            return entry["refs"]
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


def get_refs(workspace: Path, service: str) -> int:
    """Read the current reference count for a service.

    :param workspace: Workspace path.
    :type workspace: Path
    :param service: Service name.
    :type service: str
    :returns: Current reference count.
    :rtype: int
    """
    state_dir = get_state_dir(workspace)
    state = _read_state(state_dir)
    return state.get("services", {}).get(service, {}).get("refs", 0)
