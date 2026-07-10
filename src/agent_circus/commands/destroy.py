"""Destroy all agent containers, volumes, and deployed configuration."""

import logging
import shutil
from pathlib import Path
from typing import Annotated

import typer

from agent_circus.compose import compose_down
from agent_circus.config import config_exists, get_workspace_path
from agent_circus.context import build_compose_context
from agent_circus.exceptions import AgentCircusError
from agent_circus.templates import TEMPLATE_MAPPINGS

logger = logging.getLogger(__name__)


def destroy_deployed_files(workspace: Path) -> list[Path]:
    """Remove all files/directories deployed by 'init --deploy'.

    Only applicable in deploy mode.  In instant mode there are no
    deployed files to remove.

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


def destroy(
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
    runtime: Annotated[
        str | None,
        typer.Option(
            "--runtime",
            help="Container runtime backend to use: docker or podman.",
        ),
    ] = None,
) -> None:
    """Remove all containers, volumes, and deployed configuration files.

    Stops and removes all containers and named volumes, then deletes
    any configuration files deployed by 'init --deploy'.  In instant
    mode there are no deployed files; only containers and volumes are
    removed.

    Examples:
        agent-circus destroy           # Full teardown with confirmation
        agent-circus destroy --force   # Skip confirmation prompt
        agent-circus destroy --remove-orphans
    """
    workspace = workspace or get_workspace_path()

    if not force:
        typer.echo(
            "This will remove all containers, volumes, and deployed configuration files."
        )
        confirmed = typer.confirm("Are you sure you want to continue?")
        if not confirmed:
            typer.echo("Aborted.")
            raise typer.Exit(code=0)

    try:
        typer.echo("Removing containers...")
        context_kwargs = {"runtime": runtime} if runtime is not None else {}
        with build_compose_context(workspace, **context_kwargs) as ctx:
            compose_down(
                ctx,
                volumes=True,
                remove_orphans=remove_orphans,
                timeout=0 if force else None,
            )
        typer.echo("Containers removed successfully.")

        if config_exists(workspace):
            typer.echo("Removing deployed files...")
            removed = destroy_deployed_files(workspace)
            for path in removed:
                typer.echo(f"  Removed: {path}")
            typer.echo("Deployed files removed successfully.")
        else:
            typer.echo("No deployed files to remove (running in instant mode).")

    except AgentCircusError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
