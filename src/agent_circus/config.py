"""Configuration management for Agent Circus CLI."""

from pathlib import Path

CONFIG_DIR_NAME = ".agent-circus"

DEVCONTAINER_JSON = "devcontainer.json"

COMPOSE_FILE_NAME = "compose.yaml"

DOCKERFILE_NAME = "Dockerfile"

AVAILABLE_SERVICES = ["claude-code", "codex", "mistral-vibe"]


def get_workspace_path() -> Path:
    """Get the current workspace path.

    :returns: Absolute path to workspace directory.
    :rtype: Path
    """
    return Path.cwd().resolve()


def get_config_dir(workspace: Path | None = None) -> Path:
    """Get the agent-circus configuration directory.

    :param workspace: Workspace path, defaults to current directory.
    :type workspace: Path | None
    :returns: Path to configuration directory.
    :rtype: Path
    """
    if workspace is None:
        workspace = get_workspace_path()
    return workspace / CONFIG_DIR_NAME


def get_devcontainer_file(workspace: Path | None = None) -> Path:
    """Get the path to the devcontainer.json file.

    :param workspace: Workspace path, defaults to current directory.
    :type workspace: Path | None
    :returns: Path to devcontainer.json file.
    :rtype: Path
    """
    return get_config_dir(workspace) / DEVCONTAINER_JSON


def get_compose_file(workspace: Path | None = None) -> Path:
    """Get the path to the compose.yaml file.

    :param workspace: Workspace path, defaults to current directory.
    :type workspace: Path | None
    :returns: Path to compose.yaml file.
    :rtype: Path
    """
    return get_config_dir(workspace) / COMPOSE_FILE_NAME


def get_dockerfile(workspace: Path | None = None) -> Path:
    """Get the path to the Dockerfile.

    :param workspace: Workspace path, defaults to current directory.
    :type workspace: Path | None
    :returns: Path to Dockerfile.
    :rtype: Path
    """
    return get_config_dir(workspace) / DOCKERFILE_NAME


def config_exists(workspace: Path | None = None) -> bool:
    """Check if agent-circus configuration exists.

    :param workspace: Workspace path, defaults to current directory.
    :type workspace: Path | None
    :returns: True if configuration directory and compose file exist.
    :rtype: bool
    """
    config_dir = get_config_dir(workspace)
    compose_file = get_compose_file(workspace)
    return config_dir.is_dir() and compose_file.is_file()
