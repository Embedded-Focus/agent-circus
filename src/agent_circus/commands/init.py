"""Initialize agent container configuration."""

import logging
from pathlib import Path
from typing import Annotated

import typer
from agent_circus.config import (
    CONFIG_DIR_NAME,
    config_exists,
    get_compose_file,
    get_config_dir,
    get_dockerfile,
    get_workspace_path,
)
from agent_circus.templates import deploy_templates

logger = logging.getLogger(__name__)


def init(
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
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            "-c",
            help="Only check if configuration exists, don't create.",
        ),
    ] = False,
    deploy: Annotated[
        bool,
        typer.Option(
            "--deploy",
            "-d",
            help="Deploy template files to workspace.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Overwrite existing files when deploying.",
        ),
    ] = False,
) -> None:
    """Initialize or verify agent container configuration.

    Checks that the .agent-circus directory exists with the required
    configuration files (compose.yaml, Dockerfile).

    Use --check to verify configuration without making changes.
    Use --deploy to deploy template files to the workspace.
    """
    workspace = workspace or get_workspace_path()

    if deploy:
        _deploy_templates(workspace, force)
    elif check:
        _check_config(workspace)
    else:
        _init_config(workspace)


def _deploy_templates(workspace: Path, force: bool) -> None:
    """Deploy template files to workspace.

    :param workspace: Workspace path.
    :type workspace: Path
    :param force: Overwrite existing files if True.
    :type force: bool
    """
    deployed = deploy_templates(workspace, force=force)

    if not deployed:
        typer.echo("No files deployed (all already exist). Use --force to overwrite.")
        return

    typer.echo(f"Deployed {len(deployed)} file(s) to {workspace}:")
    for path in deployed:
        typer.echo(f"  {path.relative_to(workspace)}")


def _check_config(workspace: Path) -> None:
    """Check if configuration exists and is valid.

    :param workspace: Workspace path.
    :type workspace: Path
    :raises typer.Exit: If configuration is missing or invalid.
    """
    config_dir = get_config_dir(workspace)
    compose_file = get_compose_file(workspace)
    dockerfile = get_dockerfile(workspace)

    errors = []

    if not config_dir.is_dir():
        errors.append(f"Configuration directory not found: {config_dir}")

    if not compose_file.is_file():
        errors.append(f"Compose file not found: {compose_file}")

    if not dockerfile.is_file():
        errors.append(f"Dockerfile not found: {dockerfile}")

    if errors:
        for error in errors:
            typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Configuration valid: {config_dir}")
    typer.echo(f"  Compose file: {compose_file.name}")
    typer.echo(f"  Dockerfile: {dockerfile.name}")


def _init_config(workspace: Path) -> None:
    """Initialize configuration if not present.

    :param workspace: Workspace path.
    :type workspace: Path
    """
    config_dir = get_config_dir(workspace)

    if config_exists(workspace):
        typer.echo(f"Configuration already exists: {config_dir}")
        typer.echo("Use 'agent-circus init --check' to verify configuration.")
        return

    if not config_dir.exists():
        typer.echo(
            f"Configuration directory '{CONFIG_DIR_NAME}' not found in {workspace}",
            err=True,
        )
        typer.echo(
            "\nTo set up agent-circus, create the configuration directory with:",
            err=True,
        )
        typer.echo(f"  mkdir {config_dir}", err=True)
        typer.echo(
            "\nThen add compose.yaml and Dockerfile. See documentation for examples.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Config dir exists but files are missing
    compose_file = get_compose_file(workspace)
    dockerfile = get_dockerfile(workspace)

    missing = []
    if not compose_file.is_file():
        missing.append("compose.yaml")
    if not dockerfile.is_file():
        missing.append("Dockerfile")

    if missing:
        typer.echo(
            f"Configuration directory exists but missing files: {', '.join(missing)}",
            err=True,
        )
        raise typer.Exit(code=1)
