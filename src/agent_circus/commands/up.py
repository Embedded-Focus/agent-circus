"""Start agent containers."""

import logging
from pathlib import Path
from typing import Annotated

import typer

from agent_circus.compose import compose_up
from agent_circus.config import (
    AVAILABLE_SERVICES,
    get_workspace_path,
    validate_services,
)
from agent_circus.context import build_compose_context
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

    All containers run in detached mode.

    Examples:
        agent-circus up                      # Start all services
        agent-circus up claude-code          # Start only claude-code
        agent-circus up --build              # Build and start all
    """
    workspace = workspace or get_workspace_path()

    try:
        services_to_start = validate_services(services or [])

        if services:
            typer.echo(f"Starting services: {', '.join(services_to_start)}")
        else:
            typer.echo("Starting all services...")

        with build_compose_context(workspace) as ctx:
            compose_up(ctx, services_to_start, build=build)

    except AgentCircusError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
