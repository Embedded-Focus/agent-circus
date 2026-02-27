"""Configuration management for Agent Circus CLI."""

import json
import logging
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from .exceptions import ConfigurationError

CONFIG_DIR_NAME = ".agent-circus"

COMPOSE_FILE_NAME = "compose.yaml"

COMPOSE_SHADOW_FILE_NAME = "compose.shadow.json"

COMPOSE_AGENT_CONFIGS_FILE_NAME = "compose.agent-configs.json"

COMPOSE_MCP_FILE_NAME = "compose.mcp.json"

CONFIG_FILE_NAME = "config.toml"

DOCKERFILE_NAME = "Dockerfile"

AVAILABLE_SERVICES = ["claude-code", "codex", "mistral-vibe"]

VCS_MARKERS: tuple[str, ...] = (".git", ".hg", ".svn", ".bzr", "_darcs")

PROJECT_FILE_MARKERS: tuple[str, ...] = (
    ".projectile",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "Gemfile",
    "mix.exs",
    "composer.json",
)

DEFAULT_CONFIG: dict[str, Any] = {
    "shadow": [],
    "mcp_servers": [],
}

logger = logging.getLogger(__name__)


def sanitize_project_name(name: str) -> str:
    """Sanitize a name for use as a Docker Compose project name.

    Docker Compose requires project names to consist only of lowercase
    alphanumeric characters, hyphens, and underscores, and to start with
    a letter or number.

    :param name: Raw project name (typically ``workspace.name``).
    :type name: str
    :returns: Sanitized project name.
    :rtype: str
    """
    name = name.lower()
    name = re.sub(r"[^a-z0-9_-]", "-", name)
    name = re.sub(r"^[^a-z0-9]+", "", name)
    return name or "project"


def find_project_root(start: Path) -> Path:
    """Walk up from *start* and return the nearest project root.

    A directory is considered a project root when it contains at least one
    VCS directory or known project-file marker (see :data:`VCS_MARKERS` and
    :data:`PROJECT_FILE_MARKERS`).  Falls back to *start* when no marker is
    found anywhere in the ancestor chain.

    :param start: Directory to begin the upward search from.
    :type start: Path
    :returns: Nearest ancestor directory recognised as a project root,
              or *start* if none is found.
    :rtype: Path
    """
    current = start.resolve()
    all_markers = VCS_MARKERS + PROJECT_FILE_MARKERS
    while True:
        if any((current / m).exists() for m in all_markers):
            return current
        parent = current.parent
        if parent == current:  # reached filesystem root
            return start
        current = parent


def get_workspace_path() -> Path:
    """Get the current workspace path.

    Discovers the project root by walking up from the current working
    directory, using the same marker-based heuristic as Emacs Projectile.
    Falls back to the current directory when no marker is found.

    :returns: Absolute path to workspace directory.
    :rtype: Path
    """
    return find_project_root(Path.cwd().resolve())


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


def get_user_config_path() -> Path:
    """Get the path to the user-global configuration file.

    Follows the XDG Base Directory Specification: uses
    ``$XDG_CONFIG_HOME/agent-circus/config.toml``, falling back to
    ``~/.config/agent-circus/config.toml`` when the environment
    variable is unset or empty.

    :returns: Path to the user-global config file.
    :rtype: Path
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "agent-circus" / CONFIG_FILE_NAME


def get_project_config_path(workspace: Path) -> Path:
    """Get the path to the project-local configuration file.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Path to the project-local config file.
    :rtype: Path
    """
    return workspace / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def _load_toml(path: Path) -> dict[str, Any]:
    """Load and parse a TOML file.

    :param path: Path to the TOML file.
    :type path: Path
    :returns: Parsed TOML contents.
    :rtype: dict[str, Any]
    :raises ConfigurationError: If the file contains invalid TOML.
    """
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigurationError(f"Invalid TOML in {path}: {e}") from e


def load_config(workspace: Path) -> dict[str, Any]:
    """Load and merge configuration from user-global and project-local files.

    Resolution order (last wins):

    1. Built-in defaults
    2. User-global: ``$XDG_CONFIG_HOME/agent-circus/config.toml``
    3. Project-local: ``<workspace>/.agent-circus/config.toml``

    Missing files are silently skipped.  Project-local values override
    user-global values at the top level (shallow merge).

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Merged configuration dictionary.
    :rtype: dict[str, Any]
    :raises ConfigurationError: If a config file contains invalid TOML.
    """
    config = DEFAULT_CONFIG.copy()

    for path in (get_user_config_path(), get_project_config_path(workspace)):
        if path.is_file():
            logger.debug("Loading config from %s", path)
            layer = _load_toml(path)
            config.update(layer)

    return config


def _mcp_server_url(name: str, server: dict) -> str:
    """Build the Docker-network URL for an MCP sidecar server.

    :param name: MCP server name.
    :param server: Server definition from config.
    :returns: URL reachable from agent containers.
    """
    port = server.get("port", 8080)
    path = server.get("path", "/mcp")
    return f"http://mcp-{name}:{port}{path}"


def build_agent_config_additions(
    config: dict,
) -> dict[str, dict]:
    """Build per-agent config additions from Agent Circus configuration.

    Translates the ``mcp_servers`` list from ``config.toml`` into
    per-agent additions dicts with the correct key names and formats.

    :param config: Merged Agent Circus configuration.
    :returns: Per-agent additions, keyed by agent service name.
    """
    mcp_servers = config.get("mcp_servers", [])
    if not mcp_servers:
        return {}

    # Claude Code: {"mcpServers": {"name": {"type": ..., "url": ...}}}
    claude_mcp: dict[str, dict] = {}
    # Codex: {"mcp_servers": {"name": {"url": ...}}}
    codex_mcp: dict[str, dict] = {}
    # Vibe: {"mcp_servers": [{"name": ..., "transport": ..., "url": ...}]}
    vibe_mcp: list[dict] = []

    for server in mcp_servers:
        name = server["name"]
        transport = server.get("transport", "streamable-http")
        url = _mcp_server_url(name, server)

        # Claude Code requires "http" transport; other agents use the
        # configured transport (defaulting to "streamable-http").
        claude_transport = "http" if transport == "streamable-http" else transport
        claude_mcp[name] = {"type": claude_transport, "url": url}
        codex_mcp[name] = {"url": url}
        vibe_mcp.append({"name": name, "transport": transport, "url": url})

    return {
        "claude-code": {"mcpServers": claude_mcp},
        "codex": {"mcp_servers": codex_mcp},
        "mistral-vibe": {"mcp_servers": vibe_mcp},
    }


def validate_services(services: list[str]) -> list[str]:
    """Validate and return service names.

    :param services: List of service names to validate.
    :returns: Validated list of services.
    :raises ConfigurationError: If invalid service name provided.
    """
    if not services:
        return AVAILABLE_SERVICES.copy()

    invalid = set(services) - set(AVAILABLE_SERVICES)
    if invalid:
        raise ConfigurationError(
            f"Invalid service(s): {', '.join(invalid)}. "
            f"Available: {', '.join(AVAILABLE_SERVICES)}"
        )
    return services


def build_shadow_override(shadow: list[str]) -> str:
    """Build a Docker Compose override that shadows paths with ``/dev/null``.

    For each path in *shadow*, every service gets a read-only bind mount
    of ``/dev/null`` over ``/workspace/<path>``.

    :param shadow: Workspace-relative paths to shadow.
    :returns: Compose override as a JSON string.
    """
    volumes = [f"/dev/null:/workspace/{p}:ro" for p in shadow]
    services = {name: {"volumes": volumes} for name in AVAILABLE_SERVICES}
    return json.dumps({"services": services})
