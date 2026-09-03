"""Execute a command in a running agent container.

Note: exec is a reserved keyword; that's why this module is called exec_.
"""

import logging
import sys
from pathlib import Path
from typing import Annotated

import typer

from agent_circus.compose import (
    compose_config,
    compose_exec,
    compose_is_service_running,
    compose_up,
)
from agent_circus.config import (
    AVAILABLE_SERVICES,
    get_companion_services,
    get_workspace_path,
    load_config,
    validate_services,
)
from agent_circus.context import build_compose_context
from agent_circus.exceptions import AgentCircusError, ConfigurationError

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
    host_config: Annotated[
        bool,
        typer.Option(
            "--host-config",
            help="Mount the real host provider config directory writable for this service.",
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
    """Execute a command in an agent container.

    Runs a command inside the specified service container using the selected
    runtime's Compose implementation. If the container is not running, it is
    started automatically. Works in both deploy and instant mode.

    Examples:
        agent-circus exec claude-code                          # Interactive shell
        agent-circus exec claude-code -- claude-agent-acp --acp # Run ACP server
        agent-circus exec -T claude-code -- echo hello         # Non-interactive
    """
    workspace = workspace or get_workspace_path()
    companions = get_companion_services(load_config(workspace))

    try:
        validate_services([service], companions)
        cmd = command or []

        context_kwargs = {"host_config_service": service if host_config else None}
        if runtime is not None:
            context_kwargs["runtime"] = runtime
        with build_compose_context(workspace, **context_kwargs) as ctx:
            service_running = compose_is_service_running(ctx, service)
            if host_config and service_running:
                raise ConfigurationError(
                    f"Cannot use --host-config because {service} is already running. "
                    f"Remove it first with: agent-circus remove {service}"
                )
            effective_no_tty = no_tty or not sys.stdin.isatty()
            logger.debug(
                "exec: service=%s runtime=%s service_running=%s "
                "no_tty=%s (stdin_isatty=%s) command=%s",
                service,
                ctx.runtime,
                service_running,
                effective_no_tty,
                sys.stdin.isatty(),
                cmd,
            )
            if not service_running and logger.isEnabledFor(logging.DEBUG):
                try:
                    rendered = compose_config(ctx, service)
                    logger.debug(
                        "Resolved compose config for %s (verify interpolated "
                        "env vars, esp. any env_passthrough / environment "
                        "entries):\n%s",
                        service,
                        rendered,
                    )
                except AgentCircusError as e:
                    logger.debug("Failed to render compose config: %s", e)
            if not service_running:
                typer.echo(
                    f"Service {service} is not running. Starting it...",
                    err=True,
                )
                compose_up(ctx, [service], capture_output=True)

            compose_exec(ctx, service, cmd, no_tty=effective_no_tty)
    except AgentCircusError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
