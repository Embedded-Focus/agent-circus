"""MCP server integration for Agent Circus.

Handles parsing MCP server configuration from ``config.toml`` and
generating Docker Compose override files for MCP sidecar containers.

Agent-specific MCP configuration (merging MCP servers into each agent's
native config format) is handled by :mod:`agent_circus.agent_config`.
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Default MCP endpoint path used by most servers.
DEFAULT_MCP_PATH = "/mcp"

# Default port for MCP HTTP servers.
DEFAULT_MCP_PORT = 8080

# Default transport type.
DEFAULT_MCP_TRANSPORT = "streamable-http"

# Prefix for MCP sidecar service names in Docker Compose.
SERVICE_PREFIX = "mcp-"


def _service_name(name: str) -> str:
    """Return the Docker Compose service name for an MCP server.

    :param name: MCP server name from config.
    :type name: str
    :returns: Prefixed service name.
    :rtype: str
    """
    return f"{SERVICE_PREFIX}{name}"


def _server_url(name: str, port: int, path: str) -> str:
    """Build the internal Docker-network URL for an MCP server.

    :param name: MCP server name from config.
    :type name: str
    :param port: Port the server listens on.
    :type port: int
    :param path: HTTP path for the MCP endpoint.
    :type path: str
    :returns: Full URL reachable from agent containers.
    :rtype: str
    """
    return f"http://{_service_name(name)}:{port}{path}"


def build_compose_override(
    mcp_servers: list[dict[str, Any]],
    agent_services: list[str],
) -> str:
    """Build a Docker Compose override that defines MCP sidecar services.

    Returns a JSON string (valid YAML subset) suitable for passing as
    an additional ``-f`` flag to ``docker compose``.

    Each agent service gets a ``depends_on`` entry for every MCP sidecar
    so that ``docker compose up <agent>`` automatically starts the
    required MCP containers.

    :param mcp_servers: List of MCP server definitions from config.
    :type mcp_servers: list[dict[str, Any]]
    :param agent_services: Agent service names that should depend on the
        MCP sidecars (e.g. ``["claude-code", "codex", "mistral-vibe"]``).
    :type agent_services: list[str]
    :returns: Compose override as a JSON string.
    :rtype: str
    """
    services: dict[str, Any] = {}
    sidecar_names: list[str] = []

    for server in mcp_servers:
        name = server["name"]
        svc_name = _service_name(name)
        sidecar_names.append(svc_name)
        port = server.get("port", DEFAULT_MCP_PORT)

        svc: dict[str, Any] = {
            "image": server["image"],
            "expose": [port],
        }

        env = server.get("env")
        if env:
            svc["environment"] = env

        command = server.get("command")
        if command:
            svc["command"] = command

        volumes = server.get("volumes")
        if volumes:
            svc["volumes"] = volumes

        services[svc_name] = svc

    if sidecar_names:
        depends_on = {name: {"condition": "service_started"} for name in sidecar_names}
        for agent in agent_services:
            services.setdefault(agent, {})
            services[agent]["depends_on"] = depends_on

    return json.dumps({"services": services})
