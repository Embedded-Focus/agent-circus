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
    COMPOSE_AGENT_CONFIG_MOUNTS_FILE_NAME,
    COMPOSE_AGENT_CONFIGS_FILE_NAME,
    COMPOSE_CA_CERTS_FILE_NAME,
    COMPOSE_DATA_STORE_FILE_NAME,
    COMPOSE_ENV_PASSTHROUGH_FILE_NAME,
    COMPOSE_GIT_FILE_NAME,
    COMPOSE_GIT_WORKTREE_MIRROR_FILE_NAME,
    COMPOSE_HOST_CONFIG_FILE_NAME,
    COMPOSE_HOSTS_FILE_NAME,
    COMPOSE_LLAMA_CPP_FILE_NAME,
    COMPOSE_MCP_FILE_NAME,
    COMPOSE_PORT_FORWARDS_FILE_NAME,
    COMPOSE_SHADOW_FILE_NAME,
    COMPOSE_SSH_FILE_NAME,
    COMPOSE_STARTUP_HOOK_FILE_NAME,
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


def get_agent_config_mounts_override_path(workspace: Path) -> Path:
    """Get the path for the default agent config mounts compose override file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``compose.agent-config-mounts.json`` in the state directory.
    :rtype: Path
    """
    return get_state_dir(workspace) / COMPOSE_AGENT_CONFIG_MOUNTS_FILE_NAME


def get_host_config_override_path(workspace: Path) -> Path:
    """Get the path for the host config compose override file.

    :param workspace: Workspace path.
    :returns: Path to ``compose.host-config.json`` in the state directory.
    """
    return get_state_dir(workspace) / COMPOSE_HOST_CONFIG_FILE_NAME


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


def get_agent_config_stores_dir(workspace: Path) -> Path:
    """Get the directory containing writable per-agent configuration stores.

    :param workspace: Workspace path.
    :returns: Path to the ``agent-config/`` state subdirectory.
    """
    stores_dir = get_state_dir(workspace) / "agent-config"
    stores_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stores_dir.chmod(0o700)
    return stores_dir


def get_agent_config_store_dir(workspace: Path, agent_name: str) -> Path:
    """Get the writable configuration store for an agent.

    :param workspace: Workspace path.
    :param agent_name: Agent service name.
    :returns: Path to the agent's configuration store.
    """
    store_dir = get_agent_config_stores_dir(workspace) / agent_name
    store_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    store_dir.chmod(0o700)
    return store_dir


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


def get_ssh_override_path(workspace: Path) -> Path:
    """Get the path for the SSH agent forwarding compose override file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``compose.ssh.json`` in the state directory.
    :rtype: Path
    """
    return get_state_dir(workspace) / COMPOSE_SSH_FILE_NAME


def get_git_override_path(workspace: Path) -> Path:
    """Get the path for the Git configuration compose override file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``compose.git.json`` in the state directory.
    :rtype: Path
    """
    return get_state_dir(workspace) / COMPOSE_GIT_FILE_NAME


def get_hosts_override_path(workspace: Path) -> Path:
    """Get the path for the hosts compose override file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``compose.hosts.json`` in the state directory.
    :rtype: Path
    """
    return get_state_dir(workspace) / COMPOSE_HOSTS_FILE_NAME


def get_ca_certs_override_path(workspace: Path) -> Path:
    """Get the path for the CA certificates compose override file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``compose.ca-certs.json`` in the state directory.
    :rtype: Path
    """
    return get_state_dir(workspace) / COMPOSE_CA_CERTS_FILE_NAME


def get_env_passthrough_override_path(workspace: Path) -> Path:
    """Get the path for the environment variable pass-through compose override file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``compose.env-passthrough.json`` in the state directory.
    :rtype: Path
    """
    return get_state_dir(workspace) / COMPOSE_ENV_PASSTHROUGH_FILE_NAME


def get_git_worktree_mirror_override_path(workspace: Path) -> Path:
    """Get the path for the git worktree mirror compose override file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``compose.git-worktree-mirror.json`` in the state directory.
    :rtype: Path
    """
    return get_state_dir(workspace) / COMPOSE_GIT_WORKTREE_MIRROR_FILE_NAME


def get_startup_hook_path(workspace: Path) -> Path:
    """Get the path for the startup hook script in the state directory.

    Creates the ``hooks/`` subdirectory if it does not exist.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``hooks/startup.sh`` in the state directory.
    :rtype: Path
    """
    hooks_dir = get_state_dir(workspace) / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    return hooks_dir / "startup.sh"


def get_data_store_dir(workspace: Path, name: str) -> Path:
    """Get the host directory for a named project data store.

    Creates the directory if it does not exist.

    :param workspace: Workspace path.
    :type workspace: Path
    :param name: Data store name as specified in ``config.toml``.
    :type name: str
    :returns: Path to the ``data/<name>/`` subdirectory in the state directory.
    :rtype: Path
    """
    data_dir = get_state_dir(workspace) / "data" / name
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_data_store_override_path(workspace: Path) -> Path:
    """Get the path for the data store compose override file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``compose.data-store.json`` in the state directory.
    :rtype: Path
    """
    return get_state_dir(workspace) / COMPOSE_DATA_STORE_FILE_NAME


def get_port_forwards_override_path(workspace: Path) -> Path:
    """Get the path for the port forwards compose override file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``compose.port-forwards.json`` in the state directory.
    :rtype: Path
    """
    return get_state_dir(workspace) / COMPOSE_PORT_FORWARDS_FILE_NAME


def get_startup_hook_override_path(workspace: Path) -> Path:
    """Get the path for the startup hook compose override file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``compose.startup-hook.json`` in the state directory.
    :rtype: Path
    """
    return get_state_dir(workspace) / COMPOSE_STARTUP_HOOK_FILE_NAME


def get_llama_cpp_override_path(workspace: Path) -> Path:
    """Get the path for the llama.cpp compose override file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to ``compose.llama-cpp.json`` in the state directory.
    :rtype: Path
    """
    return get_state_dir(workspace) / COMPOSE_LLAMA_CPP_FILE_NAME
