"""Runtime state management for Agent Circus.

Manages per-workspace runtime state that is transient and
machine-managed, stored under
``$XDG_STATE_HOME/agent-circus/<project>/``.  This includes
generated compose overrides (shadow bind mounts).
"""

import os
from pathlib import Path

from .config import (
    COMPOSE_ADDITIONAL_DIRS_FILE_NAME,
    COMPOSE_AGENT_CONFIGS_FILE_NAME,
    COMPOSE_MCP_FILE_NAME,
    COMPOSE_SHADOW_FILE_NAME,
    sanitize_project_name,
)


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


def get_agent_configs_override_path(workspace: Path) -> Path:
    """Get the path for the agent configs compose override file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``compose.agent-configs.json`` in the state directory.
    :rtype: Path
    """
    return get_state_dir(workspace) / COMPOSE_AGENT_CONFIGS_FILE_NAME


def get_agent_configs_dir(workspace: Path) -> Path:
    """Get the directory for generated agent configuration files.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to the ``agent-configs/`` subdirectory in the state directory.
    :rtype: Path
    """
    configs_dir = get_state_dir(workspace) / "agent-configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    return configs_dir


def get_mcp_override_path(workspace: Path) -> Path:
    """Get the path for the MCP compose override file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``compose.mcp.json`` in the state directory.
    :rtype: Path
    """
    return get_state_dir(workspace) / COMPOSE_MCP_FILE_NAME


def get_additional_dirs_override_path(workspace: Path) -> Path:
    """Get the path for the additional directories compose override file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``compose.additional-dirs.json`` in the state directory.
    :rtype: Path
    """
    return get_state_dir(workspace) / COMPOSE_ADDITIONAL_DIRS_FILE_NAME
