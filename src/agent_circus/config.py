"""Configuration management for Agent Circus CLI."""

from pathlib import Path

CONFIG_DIR_NAME = ".agent-circus"

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


def resolve_config(workspace: Path) -> Path | None:
    """Resolve the effective configuration directory.

    Checks whether a deployed ``.agent-circus/`` directory exists in the
    workspace.  Returns the config directory path when found, or ``None``
    to indicate that the caller should use :func:`template_dir_context`
    from the templates module instead (instant mode).

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to the deployed config directory, or ``None``.
    :rtype: Path | None
    """
    config_dir = workspace / CONFIG_DIR_NAME
    if config_dir.is_dir() and (config_dir / COMPOSE_FILE_NAME).is_file():
        return config_dir
    return None
