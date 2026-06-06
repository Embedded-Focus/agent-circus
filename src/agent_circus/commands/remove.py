"""Remove agent containers."""

import logging
from pathlib import Path
from typing import Annotated

import typer

from agent_circus.compose import compose_down
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


def remove(
    services: Annotated[
        list[str] | None,
        typer.Argument(
            help=f"Services to remove. Defaults to all. Available: {', '.join(AVAILABLE_SERVICES)}",
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
    volumes: Annotated[
        bool,
        typer.Option(
            "--volumes",
            "-v",
            help="Also remove named volumes declared in the compose file.",
        ),
    ] = False,
    remove_orphans: Annotated[
        bool,
        typer.Option(
            "--remove-orphans",
            help="Remove containers for services not defined in the compose file.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Skip confirmation prompt.",
        ),
    ] = False,
) -> None:
    """Remove agent containers and associated resources.

    Stops and removes the specified containers, or all containers when
    no service names are given.  Use --volumes to also remove named
    volumes (e.g., bash history).

    Examples:
        agent-circus remove                        # Remove all containers
        agent-circus remove claude-code            # Remove one service
        agent-circus remove claude-code codex      # Remove multiple services
        agent-circus remove --volumes              # Remove containers and volumes
        agent-circus remove --force claude-code    # Skip confirmation
        agent-circus remove --remove-orphans       # Also remove orphan containers
    """
    workspace = workspace or get_workspace_path()
    companions = get_companion_services(load_config(workspace))

    try:
        services_to_remove = validate_services(services or [], companions)
    except AgentCircusError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    if not force:
        target = (
            f"containers for: {', '.join(services_to_remove)}"
            if services
            else "all agent containers"
        )
        if volumes:
            message = f"This will remove {target} and their volumes."
        else:
            message = f"This will remove {target}."

        typer.echo(message)
        confirmed = typer.confirm("Are you sure you want to continue?")
        if not confirmed:
            typer.echo("Aborted.")
            raise typer.Exit(code=0)

    try:
        typer.echo("Removing containers...")
        with build_compose_context(workspace) as ctx:
            compose_down(
                ctx,
                services=services_to_remove if services else None,
                volumes=volumes,
                remove_orphans=remove_orphans,
                timeout=0 if force else None,
            )
        typer.echo("Containers removed successfully.")

    except AgentCircusError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
