"""Configuration management for Agent Circus CLI."""

import fnmatch
import json
import logging
import os
import re
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from .exceptions import ConfigurationError

CONFIG_DIR_NAME = ".agent-circus"

COMPOSE_FILE_NAME = "compose.yaml"

COMPOSE_SHADOW_FILE_NAME = "compose.shadow.json"

COMPOSE_AGENT_CONFIGS_FILE_NAME = "compose.agent-configs.json"

COMPOSE_AGENT_CONFIG_MOUNTS_FILE_NAME = "compose.agent-config-mounts.json"

COMPOSE_MCP_FILE_NAME = "compose.mcp.json"

COMPOSE_ADDITIONAL_DIRS_FILE_NAME = "compose.additional-dirs.json"

COMPOSE_SSH_FILE_NAME = "compose.ssh.json"

COMPOSE_GIT_FILE_NAME = "compose.git.json"

COMPOSE_HOSTS_FILE_NAME = "compose.hosts.json"

COMPOSE_CA_CERTS_FILE_NAME = "compose.ca-certs.json"

COMPOSE_ENV_PASSTHROUGH_FILE_NAME = "compose.env-passthrough.json"

COMPOSE_STARTUP_HOOK_FILE_NAME = "compose.startup-hook.json"

COMPOSE_GIT_WORKTREE_MIRROR_FILE_NAME = "compose.git-worktree-mirror.json"

COMPOSE_DATA_STORE_FILE_NAME = "compose.data-store.json"

COMPOSE_PORT_FORWARDS_FILE_NAME = "compose.port-forwards.json"

COMPOSE_LLAMA_CPP_FILE_NAME = "compose.llama-cpp.json"

LLAMA_CPP_IMAGE = "ghcr.io/ggml-org/llama.cpp:server"
LLAMA_CPP_DEFAULT_MODEL = "ggml-org/gemma-3-1b-it-GGUF/gemma-3-1b-it-Q4_K_M.gguf"
LLAMA_CPP_DEFAULT_MODELS_CACHE = "${HOME}/.cache/huggingface"
LLAMA_CPP_DEFAULT_CONTEXT_SIZE = 2048
LLAMA_CPP_CONTAINER_MODELS_PATH = "/models"
LLAMA_CPP_PORT = 8080

DATA_STORE_DEFAULT_MOUNT_BASE = "/home/node/.local/share/agent-circus"

DATA_STORE_SEED_MODES = {"once"}

CA_CERTS_DEFAULT_DIR = "/usr/local/share/ca-certificates"

CONFIG_FILE_NAME = "config.toml"

DOCKERFILE_NAME = "Dockerfile"

HOOKS_DIR_NAME = "hooks"

AVAILABLE_SERVICES = ["claude-code", "codex", "mistral-vibe", "opencode"]

DEFAULT_AGENT_CONFIG_MOUNTS: dict[str, dict[str, str]] = {
    "claude-code": {
        "host": "${HOME}/.claude",
        "container": "/home/node/.claude",
    },
    "codex": {
        "host": "${HOME}/.codex",
        "container": "/home/node/.codex",
    },
    "mistral-vibe": {
        "host": "${HOME}/.vibe",
        "container": "/home/node/.vibe",
    },
    "opencode": {
        "host": "${HOME}/.config/opencode",
        "container": "/home/node/.config/opencode",
    },
}

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
    "env": {},
    "additional_dirs": [],
    "data_stores": [],
    "port_forwards": [],
    "ssh": None,
    "git": None,
    "hosts": None,
    "ca_certs": None,
    "env_passthrough": [],
    "hooks": None,
    "logging": {"level": "INFO", "file": None},
    "llama_cpp": None,
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


def validate_config(config: dict[str, Any]) -> None:
    """Warn about unknown top-level keys in a loaded configuration dict.

    Logs a WARNING for every key not present in :data:`DEFAULT_CONFIG`.
    Unknown keys are ignored rather than rejected so that older installs
    remain compatible with config files written for newer versions.

    :param config: Merged configuration dictionary.
    """
    known = set(DEFAULT_CONFIG)
    for key in config:
        if key not in known:
            logger.warning("Unknown config key %r — ignoring", key)


def write_hook_script(content: str, dest: Path) -> None:
    """Write inline hook script content to *dest*, ensuring it is executable.

    Prepends ``#!/usr/bin/env bash`` if the content does not already start
    with a shebang line.  Creates parent directories as needed.

    :param content: Shell script body from ``config.toml``.
    :param dest: Destination path for the script file.
    """
    if not content.startswith("#!"):
        content = "#!/usr/bin/env bash\n" + content
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)
    dest.chmod(0o755)


def load_user_config() -> dict[str, Any]:
    """Load the user-global configuration file.

    Returns an empty dict when the file does not exist.  Unlike
    :func:`load_config`, no defaults are applied and no project-local
    layer is merged.

    :returns: Parsed user-global config, or ``{}`` if absent.
    :raises ConfigurationError: If the file contains invalid TOML.
    """
    path = get_user_config_path()
    if not path.is_file():
        return {}
    return _load_toml(path)


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

    validate_config(config)
    return config


def read_project_config(workspace: Path) -> dict[str, Any]:
    """Read the project-local config file, returning an empty dict if absent.

    :param workspace: Workspace path.
    :type workspace: Path
    :returns: Parsed project config, or ``{}`` when the file does not exist.
    :rtype: dict[str, Any]
    :raises ConfigurationError: If the file contains invalid TOML.
    """
    path = get_project_config_path(workspace)
    if not path.is_file():
        return {}
    return _load_toml(path)


def write_project_config(workspace: Path, config: dict[str, Any]) -> None:
    """Write *config* to the project-local config file.

    Creates ``.agent-circus/`` if it does not exist.  Keys whose value is
    ``None`` are omitted because TOML has no null type; an absent key is
    equivalent to ``None`` in the default-config fallback.

    :param workspace: Workspace path.
    :type workspace: Path
    :param config: Configuration mapping to serialise.
    :type config: dict[str, Any]
    """
    config_dir = get_config_dir(workspace)
    config_dir.mkdir(parents=True, exist_ok=True)
    cleaned = {k: v for k, v in config.items() if v is not None}
    path = get_project_config_path(workspace)
    path.write_bytes(tomli_w.dumps(cleaned).encode())


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

    Translates ``mcp_servers`` and ``llama_cpp`` from ``config.toml`` into
    per-agent additions dicts with the correct key names and formats.

    :param config: Merged Agent Circus configuration.
    :returns: Per-agent additions, keyed by agent service name.
    """
    mcp_servers = config.get("mcp_servers", [])
    llama_cpp_config = config.get("llama_cpp")

    if not mcp_servers and llama_cpp_config is None:
        return {}

    additions: dict[str, dict] = {}

    if mcp_servers:
        # Claude Code: {"mcpServers": {"name": {"type": ..., "url": ...}}}
        claude_mcp: dict[str, dict] = {}
        # Codex: {"mcp_servers": {"name": {"url": ...}}}
        codex_mcp: dict[str, dict] = {}
        # Vibe: {"mcp_servers": [{"name": ..., "transport": ..., "url": ...}]}
        vibe_mcp: list[dict] = []
        # OpenCode: {"mcp": {"name": {"type": "remote", "url": ..., "enabled": true}}}
        opencode_mcp: dict[str, dict] = {}

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
            opencode_mcp[name] = {"type": "remote", "url": url, "enabled": True}

        additions["claude-code"] = {"mcpServers": claude_mcp}
        additions["codex"] = {"mcp_servers": codex_mcp}
        additions["mistral-vibe"] = {"mcp_servers": vibe_mcp}
        additions["opencode"] = {"mcp": opencode_mcp}

    if llama_cpp_config is not None:
        opencode_additions = additions.setdefault("opencode", {})
        opencode_additions["provider"] = _build_llama_cpp_opencode_provider(
            llama_cpp_config
        )

    return additions


def _build_llama_cpp_opencode_provider(llama_cpp_config: dict) -> dict:
    """Build the OpenCode ``provider`` block for the llama.cpp integration.

    :param llama_cpp_config: ``[llama_cpp]`` table from merged config.
    :returns: Dict suitable for merging into the OpenCode ``provider`` key.
    """
    model = llama_cpp_config.get("model", LLAMA_CPP_DEFAULT_MODEL)
    context_size = llama_cpp_config.get("context_size", LLAMA_CPP_DEFAULT_CONTEXT_SIZE)
    model_id = Path(model).stem
    return {
        "llama.cpp": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "llama-server (local)",
            "options": {
                "baseURL": f"http://llama-cpp:{LLAMA_CPP_PORT}/v1",
            },
            "models": {
                model_id: {
                    "name": model_id,
                    "limit": {"context": context_size, "output": context_size},
                },
            },
        },
    }


def get_companion_services(config: dict) -> list[str]:
    """Return companion service names active in the given config.

    Companion services are non-agent sidecars injected via Compose
    overrides: MCP servers (``mcp-<name>``) and the llama.cpp server
    (``llama-cpp``) when configured.

    :param config: Merged Agent Circus configuration.
    :returns: Docker Compose service names, in config order.
    """
    services: list[str] = []
    for server in config.get("mcp_servers", []):
        services.append(f"mcp-{server['name']}")
    if config.get("llama_cpp") is not None:
        services.append("llama-cpp")
    return services


def validate_services(
    services: list[str],
    companion_services: list[str] | None = None,
) -> list[str]:
    """Validate and return service names.

    When *services* is empty, all agent services are returned (companions
    are excluded — they start automatically via ``depends_on``).  When
    non-empty, names are validated against agent services plus any
    *companion_services* active in the current configuration.

    :param services: List of service names to validate.
    :param companion_services: Additional valid service names from the
        active config (e.g. ``["mcp-myserver", "llama-cpp"]``).
    :returns: Validated list of services.
    :raises ConfigurationError: If any name is invalid.
    """
    if not services:
        return AVAILABLE_SERVICES.copy()

    companions = companion_services or []
    all_valid = set(AVAILABLE_SERVICES) | set(companions)
    invalid = set(services) - all_valid
    if invalid:
        available = [*AVAILABLE_SERVICES, *companions]
        raise ConfigurationError(
            f"Invalid service(s): {', '.join(sorted(invalid))}. "
            f"Available: {', '.join(available)}"
        )
    return services


def build_env_dockerfile_lines(env: dict[str, str]) -> list[str]:
    """Build Dockerfile ``ENV`` instruction lines from an env mapping.

    Each entry is emitted as a separate ``ENV key="value"`` line so that
    Docker treats each variable as an independent layer, which is
    important for ``$VARNAME`` expansion (e.g. ``PATH=/foo:$PATH``
    expands ``$PATH`` from the previous layer's value).

    Values are double-quoted to prevent Docker from interpreting
    whitespace-separated tokens as additional ``key=value`` pairs
    (legacy multi-variable ``ENV`` syntax).

    :param env: Mapping of environment variable names to values.
    :returns: List of Dockerfile ``ENV`` instruction strings, one per variable.
    :rtype: list[str]
    """

    def _quote(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

    return [f"ENV {key}={_quote(value)}" for key, value in env.items()]


def build_env_profile_script(env: dict[str, str]) -> str:
    """Build a ``/etc/profile.d`` shell script that exports env vars.

    Unlike Dockerfile ``ENV`` instructions (which are evaluated at image build
    time and discarded by login shells that re-source ``/etc/profile``), a
    ``/etc/profile.d`` script is sourced *after* ``/etc/profile`` so it runs
    with the login-shell PATH already in place.  Shell variable references such
    as ``$PATH`` in the values therefore expand at container runtime against the
    live environment — ``PATH="$PATH:/home/node/go/bin"`` correctly appends to
    whatever PATH the login shell has set.

    :param env: Mapping of environment variable names to values.
    :returns: Shell script content suitable for ``/etc/profile.d/``.
    :rtype: str
    """

    def _sh_quote(v: str) -> str:
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["#!/bin/sh"]
    lines += [f"export {key}={_sh_quote(value)}" for key, value in env.items()]
    return "\n".join(lines) + "\n"


def build_additional_dirs_override(additional_dirs: list[dict]) -> str:
    """Build a Docker Compose override that bind-mounts extra host directories.

    Each entry is mounted at ``/workspaces/<name>`` inside every agent
    container, where *name* defaults to the basename of the host path.

    :param additional_dirs: List of directory entries from ``config.toml``.
        Each entry must have a ``path`` key (absolute host path) and may
        optionally have ``readonly`` (bool, default ``False``) and ``name``
        (str, default basename of *path*).
    :returns: Compose override as a JSON string.
    """
    volumes = []
    for entry in additional_dirs:
        host_path = entry["path"]
        name = entry.get("name") or Path(host_path).name
        mode = "ro" if entry.get("readonly", False) else "cached"
        volumes.append(f"{host_path}:/workspaces/{name}:{mode}")
    services = {svc: {"volumes": volumes} for svc in AVAILABLE_SERVICES}
    return json.dumps({"services": services})


def _data_store_mount_path(entry: dict) -> str:
    """Return the container mount path for a data store entry.

    :param entry: Data store entry from ``config.toml``.
    :returns: Explicit or default container mount path.
    """
    name = entry["name"]
    return entry.get("mount_path", f"{DATA_STORE_DEFAULT_MOUNT_BASE}/{name}")


def _data_store_services(entry: dict) -> list[str]:
    """Return validated services for a data store entry.

    :param entry: Data store entry from ``config.toml``.
    :returns: Services the store should be mounted into.
    :raises ConfigurationError: If ``services`` is not a list of valid services.
    """
    services = entry.get("services")
    if services is None:
        return AVAILABLE_SERVICES.copy()
    if (
        not isinstance(services, list)
        or not services
        or any(not isinstance(service, str) for service in services)
    ):
        raise ConfigurationError("Data store services must be a non-empty list")
    return validate_services(services)


def _data_store_seed_mode(entry: dict) -> str:
    """Return the validated seed mode for a data store entry.

    :param entry: Data store entry from ``config.toml``.
    :returns: Seed mode, currently only ``"once"``.
    :raises ConfigurationError: If the seed mode is unsupported.
    """
    mode = entry.get("seed_mode", "once")
    if mode not in DATA_STORE_SEED_MODES:
        raise ConfigurationError(
            "Data store seed_mode must be one of: "
            f"{', '.join(sorted(DATA_STORE_SEED_MODES))}"
        )
    return mode


def _data_store_seed_from(entry: dict) -> str | None:
    """Return the validated seed source path for a data store entry.

    Docker Compose handles ``${VAR}`` interpolation but does not reliably expand
    shell-style ``~`` in volume source paths, so tilde-prefixed paths are
    rejected with an explicit configuration error.

    :param entry: Data store entry from ``config.toml``.
    :returns: Seed source path, or ``None`` when unset.
    :raises ConfigurationError: If the path starts with ``~``.
    """
    seed_from = entry.get("seed_from")
    if seed_from is None:
        return None
    if not isinstance(seed_from, str) or not seed_from:
        raise ConfigurationError("Data store seed_from must be a non-empty string")
    if seed_from.startswith("~"):
        raise ConfigurationError(
            "Data store seed_from must use an absolute path or Compose "
            "environment interpolation like ${HOME}/.codex; '~' is not supported"
        )
    return seed_from


def get_claimed_agent_config_mounts(data_stores: list[dict]) -> set[tuple[str, str]]:
    """Return default agent config mount paths claimed by data stores.

    A claim means a data store applies to a service and mounts at that service's
    default agent configuration directory. The default host config mount is then
    suppressed for that service/path.

    :param data_stores: Data store entries from ``config.toml``.
    :returns: ``(service, container_path)`` tuples claimed by data stores.
    """
    claimed: set[tuple[str, str]] = set()
    for entry in data_stores:
        mount_path = _data_store_mount_path(entry)
        for service in _data_store_services(entry):
            default_mount = DEFAULT_AGENT_CONFIG_MOUNTS.get(service)
            if default_mount and mount_path == default_mount["container"]:
                claimed.add((service, mount_path))
    return claimed


def build_agent_config_mounts_override(
    claimed_mounts: set[tuple[str, str]] | None = None,
) -> str:
    """Build default host agent configuration directory mounts.

    These mounts preserve the historical template behavior, except any
    ``(service, container_path)`` claimed by a data store is omitted so the
    container never receives duplicate mounts for the same agent config dir.

    :param claimed_mounts: Service/path pairs claimed by data stores.
    :returns: Compose override as a JSON string.
    """
    claimed_mounts = claimed_mounts or set()
    services: dict[str, dict[str, list[str]]] = {}
    for service in AVAILABLE_SERVICES:
        default_mount = DEFAULT_AGENT_CONFIG_MOUNTS.get(service)
        volumes: list[str] = []
        if (
            default_mount
            and (service, default_mount["container"]) not in claimed_mounts
        ):
            volumes.append(
                f"{default_mount['host']}:{default_mount['container']}:cached"
            )
        services[service] = {"volumes": volumes}
    return json.dumps({"services": services})


def build_data_store_override(
    data_stores: list[dict],
    data_base_dir: Path,
) -> str:
    """Build a Docker Compose override that bind-mounts project data store directories.

    Each named store is mounted at its configured ``mount_path`` (or a default under
    :data:`DATA_STORE_DEFAULT_MOUNT_BASE`) inside every agent container.  Host
    directories are located under ``data_base_dir/<name>/``.

    :param data_stores: List of data store entries from ``config.toml``.
        Each entry must have a ``name`` key and may optionally have ``mount_path``,
        ``services``, ``seed_from``, and ``seed_mode``.
    :param data_base_dir: Base directory on the host under which per-store
        subdirectories reside.
    :returns: Compose override as a JSON string.
    """
    services = {svc: {"volumes": []} for svc in AVAILABLE_SERVICES}
    for entry in data_stores:
        name = entry["name"]
        mount_path = _data_store_mount_path(entry)
        host_path = str(data_base_dir / name)
        store_volumes = [f"{host_path}:{mount_path}:cached"]
        if _data_store_seed_from(entry):
            _data_store_seed_mode(entry)
        for service in _data_store_services(entry):
            services[service]["volumes"].extend(store_volumes)
    return json.dumps({"services": services})


def build_git_worktree_mirror_override(workspace: Path) -> str:
    """Build a Docker Compose override that mirrors the workspace at its host path.

    Adds a second bind mount for the workspace at its exact host absolute path
    inside every agent container (in addition to the standard ``/workspace``
    mount).  This makes Git's recorded absolute paths valid inside the
    container, enabling transparent Git worktree operations.

    :param workspace: Absolute host path of the workspace.
    :returns: Compose override as a JSON string.
    """
    host_path = str(workspace)
    volume = f"{host_path}:{host_path}:cached"
    services = {svc: {"volumes": [volume]} for svc in AVAILABLE_SERVICES}
    return json.dumps({"services": services})


def _validate_port(value: Any, field: str) -> int:
    """Validate a TCP/UDP port number from config.

    :param value: Raw config value.
    :param field: Field name for error messages.
    :returns: Validated port number.
    :raises ConfigurationError: If the value is not an integer in ``1..65535``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"Port forward {field} must be an integer")
    if not 1 <= value <= 65535:
        raise ConfigurationError(f"Port forward {field} must be in range 1..65535")
    return value


def build_port_forwards_override(port_forwards: list[dict]) -> str:
    """Build a Docker Compose override that publishes container ports.

    :param port_forwards: Port forward entries from ``config.toml``.
    :returns: Compose override as a JSON string.
    :raises ConfigurationError: If an entry is invalid.
    """
    services: dict[str, dict[str, list[str]]] = {}

    for entry in port_forwards:
        if not isinstance(entry, dict):
            raise ConfigurationError("Port forward entries must be tables")

        service = entry.get("service")
        if not service:
            raise ConfigurationError("Port forward service is required")
        if service not in AVAILABLE_SERVICES:
            raise ConfigurationError(
                f"Invalid port forward service: {service}. "
                f"Available: {', '.join(AVAILABLE_SERVICES)}"
            )

        if "container_port" not in entry:
            raise ConfigurationError("Port forward container_port is required")
        container_port = _validate_port(entry["container_port"], "container_port")
        host_port = _validate_port(entry.get("host_port", container_port), "host_port")

        host = entry.get("host", "127.0.0.1")
        if not isinstance(host, str) or not host:
            raise ConfigurationError("Port forward host must be a non-empty string")

        protocol = entry.get("protocol", "tcp")
        if protocol not in ("tcp", "udp"):
            raise ConfigurationError("Port forward protocol must be 'tcp' or 'udp'")

        service_config = services.setdefault(service, {"ports": []})
        service_config["ports"].append(
            f"{host}:{host_port}:{container_port}/{protocol}"
        )

    return json.dumps({"services": services})


def build_ssh_override(
    config_path: str | None = None,
    known_hosts_path: str | None = None,
) -> str:
    """Build a Docker Compose override for SSH agent forwarding and config files.

    Always mounts the host's SSH agent socket (``${SSH_AUTH_SOCK}``) at
    ``/run/ssh-agent.sock`` inside every agent container and sets
    ``SSH_AUTH_SOCK`` accordingly.  The socket path is resolved by
    Docker Compose via environment variable substitution at runtime —
    no key material is copied into the container.

    Optionally mounts ``~/.ssh/config`` and/or ``~/.ssh/known_hosts`` at
    intermediate read-only paths (``/run/ssh-host/config`` and
    ``/run/ssh-host/known_hosts``).  The container entrypoint copies these
    files into ``/home/node/.ssh/`` at startup with correct ownership.

    The caller is responsible for verifying that ``SSH_AUTH_SOCK`` is
    set in the host environment before calling this function.

    :param config_path: Absolute host path to an SSH config file, or ``None``.
    :param known_hosts_path: Absolute host path to a known_hosts file, or ``None``.
    :returns: Compose override as a JSON string.
    """
    volumes = ["${SSH_AUTH_SOCK}:/run/ssh-agent.sock:ro"]
    if config_path:
        volumes.append(f"{config_path}:/run/ssh-host/config:ro")
    if known_hosts_path:
        volumes.append(f"{known_hosts_path}:/run/ssh-host/known_hosts:ro")
    env = {"SSH_AUTH_SOCK": "/run/ssh-agent.sock"}
    services = {
        svc: {"volumes": volumes, "environment": env} for svc in AVAILABLE_SERVICES
    }
    return json.dumps({"services": services})


def build_git_override(
    config_path: str,
    signing_key_path: str | None = None,
) -> str:
    """Build a Docker Compose override that forwards the host Git configuration.

    Mounts the host gitconfig at ``/run/git-host/config:ro``.  When
    *signing_key_path* is provided, the signing public key is also mounted at
    ``/run/git-host/signingkey.pub:ro``.  ``GIT_CONFIG_GLOBAL`` is set to
    ``/home/node/.gitconfig`` so that the container entrypoint's generated
    file (which includes the host config and overrides path-dependent values)
    is used by Git.

    :param config_path: Absolute host path to the gitconfig file.
    :param signing_key_path: Absolute host path to the SSH signing public key,
        or ``None``.
    :returns: Compose override as a JSON string.
    """
    volumes = [f"{config_path}:/run/git-host/config:ro"]
    if signing_key_path:
        volumes.append(f"{signing_key_path}:/run/git-host/signingkey.pub:ro")
    env = {"GIT_CONFIG_GLOBAL": "/home/node/.gitconfig"}
    services = {
        svc: {"volumes": volumes, "environment": env} for svc in AVAILABLE_SERVICES
    }
    return json.dumps({"services": services})


def parse_hosts_file(path: str = "/etc/hosts") -> list[tuple[str, list[str]]]:
    """Parse a hosts file into a list of ``(ip, [name, ...])`` tuples.

    Blank lines and lines starting with ``#`` are skipped.  Each
    remaining line must begin with an IP address followed by one or
    more hostnames (primary + optional aliases).

    :param path: Path to the hosts file.  Defaults to ``/etc/hosts``.
    :type path: str
    :returns: List of ``(ip, names)`` tuples, one per non-comment line.
    :rtype: list[tuple[str, list[str]]]
    """
    entries: list[tuple[str, list[str]]] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                ip, names = parts[0], parts[1:]
                entries.append((ip, names))
    except OSError:
        pass
    return entries


def _compile_patterns(
    patterns: list[str],
) -> tuple[list[str], list[re.Pattern[str]]]:
    """Split *patterns* into fnmatch globs and compiled regex objects.

    Patterns prefixed with ``re:`` are compiled as case-insensitive regular
    expressions.  All other patterns are lowercased for case-insensitive
    fnmatch matching.

    :param patterns: Raw pattern list from config.
    :returns: ``(glob_patterns, compiled_re)`` tuple.
    """
    glob_patterns: list[str] = []
    compiled_re: list[re.Pattern[str]] = []
    for p in patterns:
        if p.startswith("re:"):
            compiled_re.append(re.compile(p[3:], re.IGNORECASE))
        else:
            glob_patterns.append(p.lower())
    return glob_patterns, compiled_re


def _match_pattern(
    name: str,
    glob_patterns: list[str],
    compiled_re: list[re.Pattern[str]],
) -> bool:
    """Return ``True`` if *name* matches any glob or regex pattern.

    :param name: String to test (hostname or filename).
    :param glob_patterns: Pre-lowercased fnmatch glob strings.
    :param compiled_re: Pre-compiled regex patterns.
    :returns: ``True`` when at least one pattern matches.
    """
    name_lower = name.lower()
    for pat in glob_patterns:
        if fnmatch.fnmatch(name_lower, pat):
            return True
    for rx in compiled_re:
        if rx.search(name):
            return True
    return False


def filter_hosts(
    entries: list[tuple[str, list[str]]],
    patterns: list[str],
) -> list[str]:
    """Filter host entries by patterns and return ``hostname:ip`` strings.

    Each pattern is matched case-insensitively against every name on a
    line.  If any name matches, **all** names from that line are included
    in the output (preserving alias semantics).

    Pattern syntax:

    - Plain patterns are treated as **fnmatch globs** (``*`` matches any
      sequence of characters, ``?`` matches a single character).
    - Patterns prefixed with ``re:`` are matched as Python **regular
      expressions** using :func:`re.search` (unanchored).

    :param entries: Parsed host entries from :func:`parse_hosts_file`.
    :param patterns: List of glob or ``re:``-prefixed regex patterns.
    :returns: Deduplicated list of ``"hostname:ip"`` strings.
    :rtype: list[str]
    """
    result: list[str] = []
    seen: set[str] = set()
    glob_patterns, compiled_re = _compile_patterns(patterns)

    for ip, names in entries:
        if any(_match_pattern(n, glob_patterns, compiled_re) for n in names):
            for name in names:
                entry = f"{name}:{ip}"
                if entry not in seen:
                    seen.add(entry)
                    result.append(entry)

    return result


def match_files(directory: str, patterns: list[str]) -> list[str]:
    """Return absolute paths of files in *directory* whose name matches *patterns*.

    Only regular files (not subdirectories or symlinks to directories) are
    returned.  Returns an empty list when *directory* does not exist.

    Pattern syntax: same as :func:`filter_hosts` — fnmatch glob by default,
    ``re:`` prefix for Python regex matched against the basename.

    :param directory: Absolute path to the directory to scan.
    :param patterns: List of glob or ``re:``-prefixed regex patterns.
    :returns: Sorted list of absolute file paths whose basename matches.
    :rtype: list[str]
    """
    try:
        entries = Path(directory).iterdir()
    except OSError:
        return []
    glob_patterns, compiled_re = _compile_patterns(patterns)
    result = []
    for entry in entries:
        if entry.is_file() and _match_pattern(entry.name, glob_patterns, compiled_re):
            result.append(str(entry))
    return sorted(result)


def build_ca_certs_override(cert_paths: list[str]) -> str:
    """Build a Docker Compose override that bind-mounts CA certificate files.

    Each certificate is mounted read-only at
    ``/run/ca-host/<basename>`` inside every agent container.  The
    container entrypoint copies them into the system certificate store
    and runs ``update-ca-certificates`` at startup.

    :param cert_paths: Absolute host paths to ``.crt`` files to forward.
    :type cert_paths: list[str]
    :returns: Compose override as a JSON string.
    :rtype: str
    """
    volumes = [f"{p}:/run/ca-host/{Path(p).name}:ro" for p in cert_paths]
    services = {svc: {"volumes": volumes} for svc in AVAILABLE_SERVICES}
    return json.dumps({"services": services})


def filter_env(environ: dict[str, str], patterns: list[str]) -> list[str]:
    """Return environment variable names from *environ* that match *patterns*.

    Only the **names** are returned — values are never read or stored, so
    they remain on the host and are resolved by Docker Compose at container
    start time.

    Pattern syntax: same as :func:`filter_hosts` — fnmatch glob by default,
    ``re:`` prefix for Python regex matched case-insensitively against the
    variable name.

    :param environ: Mapping of variable names to values (typically
        :data:`os.environ`).
    :param patterns: List of glob or ``re:``-prefixed regex patterns.
    :returns: Sorted, deduplicated list of matching variable names.
    :rtype: list[str]
    """
    glob_patterns, compiled_re = _compile_patterns(patterns)
    return sorted(
        {name for name in environ if _match_pattern(name, glob_patterns, compiled_re)}
    )


def build_env_passthrough_override(var_names: list[str]) -> str:
    """Build a Docker Compose override that passes host env vars into containers.

    Each variable is listed by name only (no value) so Docker Compose
    inherits it from the host environment at container start time — values
    never appear in the override file on disk.

    :param var_names: Environment variable names to forward.
    :type var_names: list[str]
    :returns: Compose override as a JSON string.
    :rtype: str
    """
    env_map = {name: None for name in var_names}
    services = {svc: {"environment": env_map} for svc in AVAILABLE_SERVICES}
    return json.dumps({"services": services})


def build_hosts_override(extra_hosts: list[str]) -> str:
    """Build a Docker Compose override that injects extra ``/etc/hosts`` entries.

    Applies the same ``extra_hosts`` list to every agent service so that
    selected host entries from the host machine are resolvable inside
    containers.

    :param extra_hosts: List of ``"hostname:ip"`` strings to inject.
    :type extra_hosts: list[str]
    :returns: Compose override as a JSON string.
    :rtype: str
    """
    services = {svc: {"extra_hosts": extra_hosts} for svc in AVAILABLE_SERVICES}
    return json.dumps({"services": services})


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


def build_startup_hook_override(startup_script_path: Path) -> str:
    """Build a Docker Compose override that bind-mounts a startup hook script.

    Mounts *startup_script_path* read-only at
    ``/workspace/.agent-circus/hooks/startup.sh`` inside every agent
    container, shadowing any file at that path coming from the workspace
    bind-mount.  Docker's volume semantics give the more specific path
    precedence, so this effectively makes ``config.toml`` win over a
    workspace-level ``startup.sh``.

    :param startup_script_path: Absolute host path to the startup script
        (typically in the XDG state directory).
    :returns: Compose override as a JSON string.
    :rtype: str
    """
    volume = f"{startup_script_path}:/workspace/.agent-circus/hooks/startup.sh:ro"
    services = {svc: {"volumes": [volume]} for svc in AVAILABLE_SERVICES}
    return json.dumps({"services": services})


def build_llama_cpp_override(llama_cpp_config: dict) -> str:
    """Build a Docker Compose override that adds a llama.cpp server sidecar.

    Defines a ``llama-cpp`` service running the official llama.cpp server
    image and injects a ``depends_on`` into the ``opencode`` service so it
    waits for the sidecar to start.

    Non-absolute model values are treated as HuggingFace references and
    split at the last ``/`` into ``--hf-repo`` and ``--hf-file`` flags,
    which work with the standard CPU server image.  Absolute paths
    (starting with ``/``) are passed through unchanged via ``-m``.

    Server configuration (host, port, context size) is applied via
    ``LLAMA_ARG_*`` environment variables rather than CLI flags to avoid
    conflicts with defaults already set in the upstream Docker image.

    :param llama_cpp_config: ``[llama_cpp]`` table from merged config.
    :returns: Compose override as a JSON string.
    :raises ConfigurationError: If ``models_cache`` starts with ``~``.
    """
    model = llama_cpp_config.get("model", LLAMA_CPP_DEFAULT_MODEL)
    models_cache = llama_cpp_config.get("models_cache", LLAMA_CPP_DEFAULT_MODELS_CACHE)
    context_size = llama_cpp_config.get("context_size", LLAMA_CPP_DEFAULT_CONTEXT_SIZE)
    extra_args: list[str] = llama_cpp_config.get("extra_args", [])

    if isinstance(models_cache, str) and models_cache.startswith("~"):
        raise ConfigurationError(
            "llama_cpp models_cache must use an absolute path or Compose "
            "environment interpolation like ${HOME}/.cache/huggingface; "
            "'~' is not supported"
        )

    if model.startswith("/"):
        # Absolute local path: pass directly to -m.
        model_args: list[str] = ["-m", model]
    elif model.endswith(".gguf"):
        # "owner/repo/file.gguf": split at last slash into repo + file.
        hf_repo, _, hf_file = model.rpartition("/")
        model_args = ["--hf-repo", hf_repo, "--hf-file", hf_file]
    else:
        # Bare HF repo "owner/repo": let llama-server pick the best file.
        model_args = ["--hf-repo", model]
    command = [*model_args, *extra_args]

    services: dict[str, Any] = {
        "llama-cpp": {
            "image": LLAMA_CPP_IMAGE,
            "command": command,
            "volumes": [
                f"{models_cache}:{LLAMA_CPP_CONTAINER_MODELS_PATH}:cached",
            ],
            "environment": {
                "HF_HOME": LLAMA_CPP_CONTAINER_MODELS_PATH,
                "LLAMA_ARG_HOST": "0.0.0.0",
                "LLAMA_ARG_PORT": str(LLAMA_CPP_PORT),
                "LLAMA_ARG_CTX_SIZE": str(context_size),
            },
        },
        "opencode": {
            "depends_on": ["llama-cpp"],
        },
    }
    return json.dumps({"services": services})
