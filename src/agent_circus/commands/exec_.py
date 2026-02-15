"""Execute a command in a running agent container.

Note: exec is a reserved keyword; that's why this module is called exec_.
"""

import logging
from pathlib import Path
from typing import Annotated

import typer

from agent_circus.compose import compose_exec
from agent_circus.config import AVAILABLE_SERVICES, get_workspace_path
from agent_circus.exceptions import AgentCircusError

logger = logging.getLogger(__name__)


def exec_cmd(
    service: Annotated[
        str,
        typer.Argument(
            help=f"Service to exec into. Available: {', '.join(AVAILABLE_SERVICES)}",
        ),
    ],
    command: Annotated[
        list[str] | None,
        typer.Argument(
            help="Command to run in the container.",
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
    no_tty: Annotated[
        bool,
        typer.Option(
            "-T",
            "--no-tty",
            help="Disable pseudo-TTY allocation.",
        ),
    ] = False,
) -> None:
    """Execute a command in a running agent container.

    Runs a command inside the specified service container using
    docker compose exec. Works in both deploy and instant mode.

    Examples:
        agent-circus exec claude-code                          # Interactive shell
        agent-circus exec claude-code -- claude-code-acp --acp # Run ACP server
        agent-circus exec -T claude-code -- echo hello         # Non-interactive
    """
    workspace = workspace or get_workspace_path()

    cmd = command or []

    try:
        compose_exec(workspace, service, cmd, no_tty=no_tty)
    except AgentCircusError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
