"""Build agent container images."""

import logging
from pathlib import Path
from typing import Annotated

import typer

from agent_circus.compose import compose_build
from agent_circus.config import (
    AVAILABLE_SERVICES,
    get_workspace_path,
    validate_services,
)
from agent_circus.context import build_compose_context
from agent_circus.exceptions import AgentCircusError

logger = logging.getLogger(__name__)


def build(
    services: Annotated[
        list[str] | None,
        typer.Argument(
            help=f"Services to build. Available: {', '.join(AVAILABLE_SERVICES)}",
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
    no_cache: Annotated[
        bool,
        typer.Option(
            "--no-cache",
            help="Build without using cache.",
        ),
    ] = False,
) -> None:
    """Build agent container images.

    Builds Docker images for the specified services using docker compose.
    If no services are specified, all services will be built.

    Examples:
        agent-circus build                    # Build all services
        agent-circus build claude-code        # Build only claude-code
        agent-circus build codex mistral-vibe # Build multiple services
        agent-circus build --no-cache         # Build without cache
    """
    workspace = workspace or get_workspace_path()

    try:
        services_to_build = validate_services(services or [])

        if services:
            typer.echo(f"Building services: {', '.join(services_to_build)}")
        else:
            typer.echo("Building all services...")

        with build_compose_context(workspace) as ctx:
            compose_build(ctx, services_to_build, no_cache=no_cache)
        typer.echo("Build completed successfully.")

    except AgentCircusError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
