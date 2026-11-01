"""Configuration loading and validation for msb-circus.

msb-circus reads the same ``.agent-circus/config.toml`` file that
agent-circus uses.  Only the keys this tool supports are interpreted
(:data:`SUPPORTED_TOP_LEVEL_KEYS`); every other top-level key is logged
as unsupported and ignored.  msb-circus-specific settings live under the
``[msb]`` table, which agent-circus in turn warns-and-ignores.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .exceptions import ConfigurationError

logger = logging.getLogger(__name__)

CONFIG_DIR_NAME = ".agent-circus"
CONFIG_FILE_NAME = "config.toml"

USER_CONFIG_DIR_ENV_VAR = "XDG_CONFIG_HOME"

#: Top-level config keys interpreted by msb-circus; everything else warns.
SUPPORTED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"msb", "logging", "env_passthrough"}
)

DEFAULT_IMAGE_TAG = "docker.io/library/msb-agents:26"
DEFAULT_MEMORY = "4G"
DEFAULT_NETWORK_MODE = "public,private"
DEFAULT_AGENT_COMMAND: tuple[str, ...] = ("pi",)
DEFAULT_WORKDIR = "/workspace"
DEFAULT_SANDBOX_NAME_PREFIX = "agents"
DEFAULT_MOUNTS: dict[str, str] = {".": "/workspace"}


@dataclass(frozen=True)
class ImageConfig:
    """Image build/import settings (``[msb.image]``).

    :ivar tag: Full image reference to build and import.
    :ivar dockerfile: Dockerfile path relative to the workspace; ``None``
        selects the bundled template.
    :ivar platform: Target platform (e.g. ``linux/amd64``); ``None`` lets
        the host builder pick the native architecture.
    """

    tag: str = DEFAULT_IMAGE_TAG
    dockerfile: str | None = None
    platform: str | None = None


@dataclass(frozen=True)
class SandboxConfig:
    """Sandbox resource settings (``[msb.sandbox]``).

    The sandbox name itself is derived from the workspace directory, not
    configured: ``agents-<sanitized project name>``.
    """

    memory: str = DEFAULT_MEMORY
    cpus: int | None = None


@dataclass(frozen=True)
class NetworkConfig:
    """Network profile settings (``[msb.network]``)."""

    mode: str = DEFAULT_NETWORK_MODE


@dataclass(frozen=True)
class AgentConfig:
    """Default exec target (``[msb.agent]``)."""

    command: tuple[str, ...] = DEFAULT_AGENT_COMMAND
    workdir: str = DEFAULT_WORKDIR


@dataclass(frozen=True)
class MsbConfig:
    """Resolved msb-circus configuration.

    :ivar binary: Optional explicit path to the ``msb`` binary.
    :ivar image: Image build/import settings.
    :ivar sandbox: Sandbox resource settings.
    :ivar network: Network profile settings.
    :ivar agent: Default exec target.
    :ivar mounts: Host path -> guest path mapping (options may be appended
        to the guest path, e.g. ``"/workspace:ro"``). Defaults to mounting
        the workspace at ``/workspace`` per the documented workflow.
    :ivar env: Sandbox-level environment variables (non-secret defaults).
    :ivar env_passthrough: Names of host environment variables required to
        exist on the host and be passed into the sandbox.
    :ivar logging_level: Log level for msb-circus's own logging.
    :ivar logging_file: Optional log file path.
    """

    binary: str | None = None
    image: ImageConfig = field(default_factory=ImageConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    mounts: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    env_passthrough: tuple[str, ...] = ()
    logging_level: str = "INFO"
    logging_file: str | None = None


def get_project_config_path(workspace: Path) -> Path:
    """Return the project-local config file path.

    :param workspace: Workspace directory.
    :returns: Path to ``<workspace>/.agent-circus/config.toml``.
    """
    return workspace / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def get_user_config_path(environ: dict[str, str] | None = None) -> Path:
    """Return the user-global config file path.

    Mirrors agent-circus: ``$XDG_CONFIG_HOME/agent-circus/config.toml``,
    defaulting to ``~/.config/agent-circus/config.toml``.

    :param environ: Environment mapping, defaults to :data:`os.environ`.
    :returns: Path to the user-global config file.
    """
    env = os.environ if environ is None else environ
    xdg = Path(env.get(USER_CONFIG_DIR_ENV_VAR, Path.home() / ".config"))
    return xdg / "agent-circus" / CONFIG_FILE_NAME


def _load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file into a dictionary.

    :param path: File to read.
    :returns: Parsed TOML document.
    :raises ConfigurationError: If the file cannot be read or parsed.
    """
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except OSError as e:
        raise ConfigurationError(f"Cannot read config file {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigurationError(f"Invalid TOML in {path}: {e}") from e


def read_config_layers(
    workspace: Path, environ: dict[str, str] | None = None
) -> dict[str, Any]:
    """Read and shallow-merge the config layers (project-local wins).

    :param workspace: Workspace directory.
    :param environ: Environment mapping for the XDG lookup.
    :returns: Merged raw configuration dictionary.
    :raises ConfigurationError: If a config file is invalid.
    """
    merged: dict[str, Any] = {}
    for path in (get_user_config_path(environ), get_project_config_path(workspace)):
        if path.is_file():
            layer = _load_toml(path)
            merged.update(layer)
    return merged


def warn_unsupported_keys(config: dict[str, Any]) -> None:
    """Log a warning for every unsupported top-level config key.

    :param config: Merged raw configuration dictionary.
    """
    for key in config:
        if key not in SUPPORTED_TOP_LEVEL_KEYS:
            logger.warning("Unsupported config key %r for msb-circus — ignoring", key)


def sanitize_project_name(name: str) -> str:
    """Sanitize a project name for use in sandbox names.

    Lowercases, replaces runs of non-alphanumeric characters with a single
    hyphen, and strips leading/trailing hyphens.

    :param name: Raw project name (usually the workspace directory name).
    :returns: Sanitized name; empty string if nothing remains.
    """
    sanitized = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return sanitized


def resolve_mounts(mounts: dict[str, str], workspace: Path) -> dict[str, str]:
    """Resolve relative host paths in mount specifications.

    A host path of ``.`` is replaced with the absolute workspace path so
    the mount is independent of the CLI's working directory.

    :param mounts: Raw mount mapping from configuration.
    :param workspace: Workspace directory.
    :returns: Mount mapping with resolved host paths.
    """
    resolved: dict[str, str] = {}
    for host, guest in mounts.items():
        resolved[str(workspace) if host == "." else host] = guest
    return resolved


def sandbox_name_for_workspace(workspace: Path) -> str:
    """Derive the sandbox name for a workspace.

    :param workspace: Workspace directory.
    :returns: ``agents-<sanitized project name>``.
    :raises ConfigurationError: If the sanitized name is empty.
    """
    project = sanitize_project_name(workspace.name)
    if not project:
        raise ConfigurationError(
            f"Cannot derive a sandbox name from workspace {str(workspace)!r}"
        )
    return f"{DEFAULT_SANDBOX_NAME_PREFIX}-{project}"


def _parse_image(raw: Any) -> ImageConfig:
    """Parse the ``[msb.image]`` table.

    :param raw: Raw table value or ``None``.
    :returns: Parsed image config.
    :raises ConfigurationError: If the table is malformed.
    """
    if raw is None:
        return ImageConfig()
    if not isinstance(raw, dict):
        raise ConfigurationError("msb.image must be a table")
    tag = raw.get("tag", DEFAULT_IMAGE_TAG)
    if not isinstance(tag, str) or not tag:
        raise ConfigurationError("msb.image.tag must be a non-empty string")
    dockerfile = raw.get("dockerfile")
    if dockerfile is not None and not isinstance(dockerfile, str):
        raise ConfigurationError("msb.image.dockerfile must be a string")
    platform = raw.get("platform")
    if platform is not None and not isinstance(platform, str):
        raise ConfigurationError("msb.image.platform must be a string")
    return ImageConfig(tag=tag, dockerfile=dockerfile, platform=platform)


def _parse_sandbox(raw: Any) -> SandboxConfig:
    """Parse the ``[msb.sandbox]`` table.

    :param raw: Raw table value or ``None``.
    :returns: Parsed sandbox config.
    :raises ConfigurationError: If the table is malformed.
    """
    if raw is None:
        return SandboxConfig()
    if not isinstance(raw, dict):
        raise ConfigurationError("msb.sandbox must be a table")
    memory = raw.get("memory", DEFAULT_MEMORY)
    if not isinstance(memory, str) or not memory:
        raise ConfigurationError("msb.sandbox.memory must be a non-empty string")
    cpus = raw.get("cpus")
    if cpus is not None and not isinstance(cpus, int):
        raise ConfigurationError("msb.sandbox.cpus must be an integer")
    return SandboxConfig(memory=memory, cpus=cpus)


def _parse_network(raw: Any) -> NetworkConfig:
    """Parse the ``[msb.network]`` table.

    :param raw: Raw table value or ``None``.
    :returns: Parsed network config.
    :raises ConfigurationError: If the table is malformed.
    """
    if raw is None:
        return NetworkConfig()
    if not isinstance(raw, dict):
        raise ConfigurationError("msb.network must be a table")
    mode = raw.get("mode", DEFAULT_NETWORK_MODE)
    if not isinstance(mode, str) or not mode:
        raise ConfigurationError("msb.network.mode must be a non-empty string")
    return NetworkConfig(mode=mode)


def _parse_agent(raw: Any) -> AgentConfig:
    """Parse the ``[msb.agent]`` table.

    :param raw: Raw table value or ``None``.
    :returns: Parsed agent config.
    :raises ConfigurationError: If the table is malformed.
    """
    if raw is None:
        return AgentConfig()
    if not isinstance(raw, dict):
        raise ConfigurationError("msb.agent must be a table")
    command = raw.get("command", DEFAULT_AGENT_COMMAND)
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise ConfigurationError(
            "msb.agent.command must be a non-empty list of strings"
        )
    workdir = raw.get("workdir", DEFAULT_WORKDIR)
    if not isinstance(workdir, str) or not workdir:
        raise ConfigurationError("msb.agent.workdir must be a non-empty string")
    return AgentConfig(command=tuple(command), workdir=workdir)


def _parse_string_dict(raw: Any, label: str) -> dict[str, str]:
    """Parse a ``KEY = "value"`` table into a plain string dict.

    :param raw: Raw table value or ``None``.
    :param label: Config key label used in error messages.
    :returns: Parsed string mapping.
    :raises ConfigurationError: If the table is malformed.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{label} must be a table")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            raise ConfigurationError(f"{label}.{key} must be a string")
        result[key] = value
    return result


def _parse_env_passthrough(raw: Any) -> tuple[str, ...]:
    """Parse the ``env_passthrough`` list.

    :param raw: Raw value or ``None``.
    :returns: Tuple of variable names.
    :raises ConfigurationError: If the value is malformed.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(
        isinstance(name, str) and name for name in raw
    ):
        raise ConfigurationError("env_passthrough must be a list of non-empty strings")
    return tuple(raw)


def _parse_logging(raw: Any) -> tuple[str, str | None]:
    """Parse the ``[logging]`` table.

    :param raw: Raw table value or ``None``.
    :returns: Tuple of ``(level, file)``.
    :raises ConfigurationError: If the table is malformed.
    """
    if raw is None:
        return "INFO", None
    if not isinstance(raw, dict):
        raise ConfigurationError("logging must be a table")
    level = raw.get("level", "INFO")
    if not isinstance(level, str) or not level:
        raise ConfigurationError("logging.level must be a non-empty string")
    log_file = raw.get("file")
    if log_file is not None and not isinstance(log_file, str):
        raise ConfigurationError("logging.file must be a string")
    return level, log_file


def parse_config(config: dict[str, Any]) -> MsbConfig:
    """Parse and validate a merged raw configuration.

    :param config: Merged raw configuration dictionary.
    :returns: Validated :class:`MsbConfig`.
    :raises ConfigurationError: If any supported key is malformed.
    """
    warn_unsupported_keys(config)
    msb_raw = config.get("msb")
    if msb_raw is not None and not isinstance(msb_raw, dict):
        raise ConfigurationError("msb must be a table")
    msb_raw = msb_raw or {}
    binary = msb_raw.get("binary")
    if binary is not None and not isinstance(binary, str):
        raise ConfigurationError("msb.binary must be a string")
    level, log_file = _parse_logging(config.get("logging"))
    return MsbConfig(
        binary=binary,
        image=_parse_image(msb_raw.get("image")),
        sandbox=_parse_sandbox(msb_raw.get("sandbox")),
        network=_parse_network(msb_raw.get("network")),
        agent=_parse_agent(msb_raw.get("agent")),
        mounts=(
            _parse_string_dict(msb_raw.get("mounts"), "msb.mounts") or DEFAULT_MOUNTS
        ),
        env=_parse_string_dict(msb_raw.get("env"), "msb.env"),
        env_passthrough=_parse_env_passthrough(config.get("env_passthrough")),
        logging_level=level,
        logging_file=log_file,
    )


def load_config(workspace: Path, environ: dict[str, str] | None = None) -> MsbConfig:
    """Load, merge, and validate msb-circus configuration.

    :param workspace: Workspace directory.
    :param environ: Environment mapping for the XDG lookup.
    :returns: Resolved configuration.
    :raises ConfigurationError: If a config file is invalid.
    """
    return parse_config(read_config_layers(workspace, environ))
