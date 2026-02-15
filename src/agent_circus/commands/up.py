"""Start agent containers."""

import logging
from pathlib import Path
from typing import Annotated

import typer

from agent_circus.compose import compose_up
from agent_circus.config import AVAILABLE_SERVICES, get_workspace_path
from agent_circus.exceptions import AgentCircusError

logger = logging.getLogger(__name__)


def up(
    services: Annotated[
        list[str] | None,
        typer.Argument(
            help=f"Services to start. Available: {', '.join(AVAILABLE_SERVICES)}",
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
    build: Annotated[
        bool,
        typer.Option(
            "--build",
            "-b",
            help="Build images before starting containers.",
        ),
    ] = False,
) -> None:
    """Start agent containers.

    Starts the specified services using docker compose. If no services
    are specified, all services will be started.

    By default, containers run in detached mode. Use --attach to run
    in the foreground and see container output.

    Examples:
        agent-circus up                      # Start all services
        agent-circus up claude-code          # Start only claude-code
        agent-circus up --build              # Build and start all
    """
    workspace = workspace or get_workspace_path()

    services_to_start = services or []

    try:
        if services_to_start:
            typer.echo(f"Starting services: {', '.join(services_to_start)}")
        else:
            typer.echo("Starting all services...")

        compose_up(workspace, services_to_start or None, build=build)

    except AgentCircusError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
