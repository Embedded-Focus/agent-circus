"""Show status of agent containers."""

import logging
from pathlib import Path
from typing import Annotated

import typer

from agent_circus.compose import compose_ps
from agent_circus.config import AVAILABLE_SERVICES, config_exists, get_workspace_path
from agent_circus.exceptions import AgentCircusError

logger = logging.getLogger(__name__)


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
) -> None:
    """Show status of agent containers.

    Lists the running containers for the specified services.
    If no services are specified, all services will be shown.

    Examples:
        agent-circus ps                     # Show all services
        agent-circus ps claude-code          # Show only claude-code
        agent-circus ps --all                # Include stopped containers
    """
    workspace = workspace or get_workspace_path()

    if not config_exists(workspace):
        typer.echo(
            "Error: Configuration not found. Run 'agent-circus init' first.",
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        output = compose_ps(workspace, services or None, all_containers=all_containers)
        typer.echo(output, nl=False)
    except AgentCircusError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
