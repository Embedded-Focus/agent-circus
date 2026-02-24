"""Show status of agent containers."""

import logging
from pathlib import Path
from typing import Annotated

import typer

from agent_circus.compose import compose_ps
from agent_circus.config import AVAILABLE_SERVICES, get_workspace_path, load_config
from agent_circus.exceptions import AgentCircusError
from agent_circus.mcp import SERVICE_PREFIX

logger = logging.getLogger(__name__)


def _mcp_service_names(workspace: Path) -> list[str]:
    """Derive MCP sidecar service names from the workspace config."""
    config = load_config(workspace)
    return [f"{SERVICE_PREFIX}{s['name']}" for s in config.get("mcp_servers", [])]


def ps(
    services: Annotated[
        list[str] | None,
        typer.Argument(
            help=f"Services to show. Available: {', '.join(AVAILABLE_SERVICES)}",
        ),
    ] = None,
    workspace: Annotated[
        Path | None,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory path.",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = None,
    all_containers: Annotated[
        bool,
        typer.Option(
            "--all",
            "-a",
            help="Show all containers (including stopped).",
        ),
    ] = False,
    agents_only: Annotated[
        bool,
        typer.Option(
            "--agents",
            help="Show only agent containers.",
        ),
    ] = False,
    mcp_only: Annotated[
        bool,
        typer.Option(
            "--mcp",
            help="Show only MCP sidecar containers.",
        ),
    ] = False,
) -> None:
    """Show status of agent containers.

    Lists the running containers for the specified services.
    If no services are specified, all services will be shown.

    Examples:
        agent-circus ps                      # Show all services
        agent-circus ps claude-code           # Show only claude-code
        agent-circus ps --agents              # Show only agent containers
        agent-circus ps --mcp                 # Show only MCP sidecar containers
        agent-circus ps --all                 # Include stopped containers
    """
    workspace = workspace or get_workspace_path()

    if agents_only and mcp_only:
        typer.echo("Error: --agents and --mcp are mutually exclusive", err=True)
        raise typer.Exit(code=1)

    if (agents_only or mcp_only) and services:
        typer.echo(
            "Error: --agents/--mcp cannot be combined with explicit services",
            err=True,
        )
        raise typer.Exit(code=1)

    filter_services: list[str] | None = services or None
    if agents_only:
        filter_services = AVAILABLE_SERVICES.copy()
    elif mcp_only:
        filter_services = _mcp_service_names(workspace)
        if not filter_services:
            typer.echo("No MCP servers configured.")
            return

    try:
        output = compose_ps(workspace, filter_services, all_containers=all_containers)
        typer.echo(output, nl=False)
    except AgentCircusError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
