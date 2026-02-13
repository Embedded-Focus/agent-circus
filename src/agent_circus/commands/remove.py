"""Remove agent containers."""

import logging
import shutil
from pathlib import Path
from typing import Annotated

import typer

from agent_circus.compose import compose_down
from agent_circus.config import config_exists, get_workspace_path
from agent_circus.exceptions import AgentCircusError
from agent_circus.templates import TEMPLATE_MAPPINGS

logger = logging.getLogger(__name__)


def destroy_deployed_files(workspace: Path) -> list[Path]:
    """Remove all files/directories deployed by 'init --deploy'.

    :param workspace: Workspace directory.
    :type workspace: Path
    :returns: List of removed paths.
    :rtype: list[Path]
    """
    removed = []
    for _, dst_name in TEMPLATE_MAPPINGS:
        path = workspace / dst_name
        if path.is_dir():
            shutil.rmtree(path)
            removed.append(path)
        elif path.exists():
            path.unlink()
            removed.append(path)
    return removed


def remove(
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
    destroy: Annotated[
        bool,
        typer.Option(
            "--destroy",
            "-d",
            help="Remove all files deployed by 'init --deploy' (implies --volumes).",
        ),
    ] = False,
) -> None:
    """Remove agent containers and associated resources.

    Stops and removes all containers defined in the compose file.
    Use --volumes to also remove named volumes (e.g., bash history).

    Examples:
        agent-circus remove                  # Remove all containers
        agent-circus remove --volumes        # Remove containers and volumes
        agent-circus remove --force          # Skip confirmation
        agent-circus remove --remove-orphans # Also remove orphan containers
        agent-circus remove --destroy        # Remove containers, volumes, and config
    """
    workspace = workspace or get_workspace_path()

    # --destroy implies --volumes
    if destroy:
        volumes = True

    if not config_exists(workspace):
        typer.echo(
            "Error: Configuration not found. Run 'agent-circus init' first.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Confirm removal unless --force is specified
    if not force:
        if destroy:
            message = (
                "This will remove all agent containers, volumes, and deployed files."
            )
        elif volumes:
            message = "This will remove all agent containers and their volumes."
        else:
            message = "This will remove all agent containers."

        typer.echo(message)
        confirmed = typer.confirm("Are you sure you want to continue?")
        if not confirmed:
            typer.echo("Aborted.")
            raise typer.Exit(code=0)

    try:
        typer.echo("Removing containers...")
        compose_down(workspace, volumes=volumes, remove_orphans=remove_orphans)
        typer.echo("Containers removed successfully.")

        if destroy:
            typer.echo("Removing deployed files...")
            removed = destroy_deployed_files(workspace)
            for path in removed:
                typer.echo(f"  Removed: {path}")
            typer.echo("Deployed files removed successfully.")

    except AgentCircusError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
