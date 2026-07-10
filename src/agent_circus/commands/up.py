"""Start agent containers."""

import logging
from pathlib import Path
from typing import Annotated

import typer

from agent_circus.compose import compose_up
from agent_circus.config import (
    AVAILABLE_SERVICES,
    get_companion_services,
    get_workspace_path,
    load_config,
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
    runtime: Annotated[
        str | None,
        typer.Option(
            "--runtime",
            help="Container runtime backend to use: docker or podman.",
        ),
    ] = None,
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
    companions = get_companion_services(load_config(workspace))

    try:
        services_to_start = validate_services(services or [], companions)

        if services:
            typer.echo(f"Starting services: {', '.join(services_to_start)}")
        else:
            typer.echo("Starting all services...")

        context_kwargs = {"runtime": runtime} if runtime is not None else {}
        with build_compose_context(workspace, **context_kwargs) as ctx:
            compose_up(ctx, services_to_start, build=build)

    except AgentCircusError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
