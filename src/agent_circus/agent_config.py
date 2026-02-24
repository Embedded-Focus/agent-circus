"""Agent configuration templating for Agent Circus.

Reads each agent's original configuration file, merges in additions
(e.g. MCP servers), writes the result to the state directory, and
generates a Docker Compose override that bind-mounts the merged
files into agent containers.

The user's host files are never modified.
"""

import json
import logging
import tomllib
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import tomli_w

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handler protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class AgentConfigHandler(Protocol):
    """Structural interface for agent-specific configuration handlers."""

    agent_name: str
    """Docker Compose service name (e.g. ``"claude-code"``)."""

    host_config_path: Path
    """Path to the user's original config file on the host."""

    container_config_path: str
    """Absolute path where the config is expected inside the container."""

    output_filename: str
    """Filename for the merged config in the state directory."""

    def read(self) -> dict[str, Any]:
        """Read and deserialize the user's original config.

        Returns an empty dict if the file does not exist.
        """
        ...

    def merge(self, base: dict[str, Any], additions: dict[str, Any]) -> dict[str, Any]:
        """Merge *additions* into *base* config.

        :param base: The user's original (deserialized) config.
        :param additions: Values to merge in (e.g. ``{"mcp_servers": [...]}``)
        :returns: Merged config.
        """
        ...

    def write(self, config: dict[str, Any], output_path: Path) -> None:
        """Serialize *config* and write it to *output_path*."""
        ...


def build_handler(
    handler: AgentConfigHandler,
    additions: dict[str, Any],
    output_dir: Path,
) -> Path:
    """Read → merge → write using the given handler.

    :param handler: Agent config handler to use.
    :param additions: Values to merge into the user's config.
    :param output_dir: Directory to write the merged config to.
    :returns: Path to the written file.
    """
    base = handler.read()
    merged = handler.merge(base, additions)
    output_path = output_dir / handler.output_filename
    handler.write(merged, output_path)
    logger.debug("Built merged config for %s: %s", handler.agent_name, output_path)
    return output_path


# ---------------------------------------------------------------------------
# Claude Code (JSON)
# ---------------------------------------------------------------------------


class ClaudeCodeConfigHandler:
    """Handler for Claude Code configuration (JSON)."""

    agent_name = "claude-code"
    container_config_path = "/home/node/.claude/.claude.json"
    output_filename = "claude-code.json"

    def __init__(self) -> None:
        self.host_config_path = Path.home() / ".claude" / ".claude.json"

    def read(self) -> dict[str, Any]:
        if not self.host_config_path.is_file():
            return {}
        with open(self.host_config_path) as f:
            return json.load(f)

    def merge(self, base: dict[str, Any], additions: dict[str, Any]) -> dict[str, Any]:
        merged = base.copy()
        for key, value in additions.items():
            if (
                key in merged
                and isinstance(merged[key], dict)
                and isinstance(value, dict)
            ):
                # Dict-level merge (e.g. mcpServers): additions overwrite conflicts.
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        return merged

    def write(self, config: dict[str, Any], output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")


# ---------------------------------------------------------------------------
# TOML-based handlers (Codex, Vibe)
# ---------------------------------------------------------------------------


class _TomlConfigHandler:
    """Base handler for TOML-based agent configs (Codex, Vibe)."""

    host_config_path: Path

    def read(self) -> dict[str, Any]:
        if not self.host_config_path.is_file():
            return {}
        with open(self.host_config_path, "rb") as f:
            return tomllib.load(f)

    def merge(self, base: dict[str, Any], additions: dict[str, Any]) -> dict[str, Any]:
        merged = base.copy()
        for key, value in additions.items():
            if (
                key in merged
                and isinstance(merged[key], list)
                and isinstance(value, list)
            ):
                # Array merge by "name" field: additions overwrite entries
                # with the same name, new entries are appended.
                merged[key] = _merge_named_arrays(merged[key], value)
            elif (
                key in merged
                and isinstance(merged[key], dict)
                and isinstance(value, dict)
            ):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
        return merged

    def write(self, config: dict[str, Any], output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            tomli_w.dump(config, f)


class CodexConfigHandler(_TomlConfigHandler):
    """Handler for Codex configuration (TOML)."""

    agent_name = "codex"
    container_config_path = "/home/node/.codex/config.toml"
    output_filename = "codex.toml"

    def __init__(self) -> None:
        self.host_config_path = Path.home() / ".codex" / "config.toml"


class VibeConfigHandler(_TomlConfigHandler):
    """Handler for Mistral Vibe configuration (TOML)."""

    agent_name = "mistral-vibe"
    container_config_path = "/home/node/.vibe/config.toml"
    output_filename = "mistral-vibe.toml"

    def __init__(self) -> None:
        self.host_config_path = Path.home() / ".vibe" / "config.toml"


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------


def _merge_named_arrays(
    base: list[dict[str, Any]], additions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge two lists of dicts by the ``name`` field.

    Entries in *additions* overwrite base entries with the same name.
    New entries are appended.  Order: base entries first (updated
    in-place), then new additions.

    Falls back to simple concatenation if entries lack ``name`` fields.

    :param base: Original list of entries.
    :param additions: Entries to merge in.
    :returns: Merged list.
    """
    # Check if entries have "name" fields for keyed merging.
    if not all(isinstance(e, dict) and "name" in e for e in [*base, *additions]):
        return [*base, *additions]

    by_name: dict[str, dict[str, Any]] = {e["name"]: e for e in base}
    order: list[str] = [e["name"] for e in base]

    for entry in additions:
        name = entry["name"]
        if name not in by_name:
            order.append(name)
        by_name[name] = entry

    return [by_name[name] for name in order]


# ---------------------------------------------------------------------------
# All handlers
# ---------------------------------------------------------------------------


HANDLERS: list[type[AgentConfigHandler]] = [
    ClaudeCodeConfigHandler,
    CodexConfigHandler,
    VibeConfigHandler,
]


# ---------------------------------------------------------------------------
# Compose override generation
# ---------------------------------------------------------------------------


def build_agent_configs_override(
    additions: dict[str, dict[str, Any]],
    output_dir: Path,
) -> str:
    """Build merged agent configs and return a Compose override JSON string.

    For each agent, reads the user's original config, merges in
    *additions*, writes the result to *output_dir*, and produces a
    Compose override that bind-mounts the merged files into the
    corresponding containers.

    :param additions: Per-agent additions dict, keyed by agent name.
        Example::

            {
                "claude-code": {"mcpServers": {...}},
                "codex": {"mcp_servers": [...]},
                "mistral-vibe": {"mcp_servers": [...]},
            }

    :param output_dir: Directory to write merged config files to.
    :type output_dir: Path
    :returns: Compose override as a JSON string.
    :rtype: str
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    services: dict[str, Any] = {}

    for handler_cls in HANDLERS:
        handler = handler_cls()
        agent_additions = additions.get(handler.agent_name, {})
        if not agent_additions:
            continue

        output_path = build_handler(handler, agent_additions, output_dir)
        services[handler.agent_name] = {
            "volumes": [
                f"{output_path}:{handler.container_config_path}:ro",
            ],
        }

    return json.dumps({"services": services})
