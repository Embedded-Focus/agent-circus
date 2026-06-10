"""Manage project-local writable agent configuration stores."""

import shutil
from pathlib import Path
from typing import Annotated

import typer

from agent_circus.compose import compose_is_service_running
from agent_circus.config import (
    AVAILABLE_SERVICES,
    get_agent_config_data_stores,
    get_workspace_path,
    load_config,
    validate_services,
)
from agent_circus.context import build_compose_context
from agent_circus.exceptions import AgentCircusError, ConfigurationError
from agent_circus.state import get_agent_config_store_dir, get_data_store_dir

app = typer.Typer(help="Manage writable agent configuration stores.")


def _config_store_path(workspace: Path, agent_name: str) -> Path:
    """Return the active writable config store path for an agent.

    :param workspace: Workspace directory.
    :param agent_name: Agent service name.
    :returns: Built-in or explicitly configured store path.
    """
    owners = get_agent_config_data_stores(load_config(workspace).get("data_stores", []))
    explicit_store = owners.get(agent_name)
    if explicit_store:
        return get_data_store_dir(workspace, explicit_store)
    return get_agent_config_store_dir(workspace, agent_name)


@app.command()
def reset(
    agent: Annotated[
        str | None,
        typer.Argument(
            help=f"Agent to reset. Available: {', '.join(AVAILABLE_SERVICES)}"
        ),
    ] = None,
    all_agents: Annotated[
        bool,
        typer.Option("--all", help="Reset configuration stores for every agent."),
    ] = False,
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
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Skip the confirmation prompt."),
    ] = False,
) -> None:
    """Delete writable config stores so the next start seeds them again."""
    workspace = workspace or get_workspace_path()

    try:
        if all_agents == (agent is not None):
            raise ConfigurationError("Specify exactly one agent or --all")
        agents = AVAILABLE_SERVICES if all_agents else validate_services([agent or ""])

        with build_compose_context(workspace) as ctx:
            running = [name for name in agents if compose_is_service_running(ctx, name)]
        if running:
            raise ConfigurationError(
                "Cannot reset config for running services: " + ", ".join(running)
            )

        if not force:
            typer.echo(
                "This will delete writable config for: " + ", ".join(agents) + "."
            )
            if not typer.confirm("Are you sure you want to continue?"):
                typer.echo("Aborted.")
                raise typer.Exit(code=0)

        for name in agents:
            store_path = _config_store_path(workspace, name)
            if store_path.exists():
                shutil.rmtree(store_path)
            typer.echo(f"Reset {name} config store: {store_path}")
    except AgentCircusError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
