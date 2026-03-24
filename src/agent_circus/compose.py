"""Docker Compose operations for Agent Circus.

This is a low-level module that executes ``docker compose`` commands.
All configuration assembly (loading config, building overrides,
resolving deploy vs instant mode) happens in higher-level modules
and is passed in via :class:`ComposeContext`.
"""

import dataclasses
import logging
import subprocess
from pathlib import Path

from .exceptions import ComposeError
from .state import (
    get_additional_dirs_override_path,
    get_agent_configs_override_path,
    get_mcp_override_path,
    get_shadow_override_path,
)

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ComposeContext:
    """Pre-assembled context for running docker compose commands.

    Built by :func:`agent_circus.context.build_compose_context` from
    higher-level configuration.  Contains everything ``compose``
    functions need without reaching into config, MCP, or template
    modules.

    :param workspace: Workspace path.
    :param project_name: Sanitized Docker Compose project name.
    :param compose_file: Absolute path to the base compose file.
    :param cwd: Working directory for the subprocess.
    :param env: Extra environment variables, or ``None`` to inherit.
    :param shadow_override: JSON string for shadow bind mounts, or ``None``.
    :param agent_configs_override: JSON string for agent config mounts, or ``None``.
    :param mcp_override: JSON string for MCP sidecar services, or ``None``.
    :param additional_dirs_override: JSON string for extra directory mounts, or ``None``.
    """

    workspace: Path
    project_name: str
    compose_file: Path
    cwd: Path
    env: dict[str, str] | None = None
    shadow_override: str | None = None
    agent_configs_override: str | None = None
    mcp_override: str | None = None
    additional_dirs_override: str | None = None


def _exec_compose(
    args: list[str],
    ctx: ComposeContext,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Execute a docker compose command.

    Writes any override strings from *ctx* to the runtime state
    directory and passes them as additional ``-f`` flags.

    :param args: Arguments to pass after ``docker compose -p ... -f ...``.
    :param ctx: Pre-assembled compose context.
    :param capture_output: Capture stdout/stderr instead of streaming.
    :returns: Completed process result.
    :raises ComposeError: If command fails.
    """
    cmd = [
        "docker",
        "compose",
        "-p",
        ctx.project_name,
        "-f",
        str(ctx.compose_file),
    ]

    shadow_path = get_shadow_override_path(ctx.workspace)
    if ctx.shadow_override:
        shadow_path.write_text(ctx.shadow_override)
        cmd.extend(["-f", str(shadow_path)])
        logger.debug("Shadow override: %s", shadow_path)
    else:
        shadow_path.unlink(missing_ok=True)

    agent_configs_path = get_agent_configs_override_path(ctx.workspace)
    if ctx.agent_configs_override:
        agent_configs_path.write_text(ctx.agent_configs_override)
        cmd.extend(["-f", str(agent_configs_path)])
        logger.debug("Agent configs override: %s", agent_configs_path)
    else:
        agent_configs_path.unlink(missing_ok=True)

    mcp_path = get_mcp_override_path(ctx.workspace)
    if ctx.mcp_override:
        mcp_path.write_text(ctx.mcp_override)
        cmd.extend(["-f", str(mcp_path)])
        logger.debug("MCP sidecar override: %s", mcp_path)
    else:
        mcp_path.unlink(missing_ok=True)

    additional_dirs_path = get_additional_dirs_override_path(ctx.workspace)
    if ctx.additional_dirs_override:
        additional_dirs_path.write_text(ctx.additional_dirs_override)
        cmd.extend(["-f", str(additional_dirs_path)])
        logger.debug("Additional dirs override: %s", additional_dirs_path)
    else:
        additional_dirs_path.unlink(missing_ok=True)

    cmd.extend(args)
    logger.debug("Running: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            cwd=str(ctx.cwd),
            capture_output=capture_output,
            text=True,
            check=False,
            env=ctx.env,
        )
        if result.returncode != 0:
            error_msg = result.stderr if capture_output else "Command failed"
            raise ComposeError(f"docker compose failed: {error_msg}")
        return result
    except FileNotFoundError as e:
        raise ComposeError("docker compose not found. Is Docker installed?") from e
    except subprocess.SubprocessError as e:
        raise ComposeError(f"Failed to run docker compose: {e}") from e


def compose_build(
    ctx: ComposeContext,
    services: list[str] | None = None,
    no_cache: bool = False,
) -> None:
    """Build service images using docker compose.

    :param ctx: Pre-assembled compose context.
    :param services: Services to build, or ``None`` for all.
    :param no_cache: Disable build cache.
    :raises ComposeError: If build fails.
    """
    args = ["build"]
    if no_cache:
        args.append("--no-cache")
    if services:
        args.extend(services)

    logger.info("Building services: %s", ", ".join(services or ["all"]))
    _exec_compose(args, ctx)


def compose_up(
    ctx: ComposeContext,
    services: list[str] | None = None,
    detach: bool = True,
    build: bool = False,
) -> None:
    """Start services using docker compose.

    :param ctx: Pre-assembled compose context.
    :param services: Services to start, or ``None`` for all.
    :param detach: Run in detached mode.
    :param build: Build images before starting.
    :raises ComposeError: If up fails.
    """
    args = ["up"]
    if detach:
        args.append("-d")
    if build:
        args.append("--build")
    if services:
        args.extend(services)

    logger.info("Starting services: %s", ", ".join(services or ["all"]))
    _exec_compose(args, ctx)


def compose_down(
    ctx: ComposeContext,
    volumes: bool = False,
    remove_orphans: bool = False,
    timeout: int | None = None,
) -> None:
    """Stop and remove containers using docker compose.

    :param ctx: Pre-assembled compose context.
    :param volumes: Remove named volumes.
    :param remove_orphans: Remove orphan containers.
    :param timeout: Seconds to wait for containers to stop (``-t``).
    :raises ComposeError: If down fails.
    """
    args = ["down"]
    if timeout is not None:
        args.extend(["-t", str(timeout)])
    if volumes:
        args.append("-v")
    if remove_orphans:
        args.append("--remove-orphans")

    logger.info("Stopping and removing containers")
    _exec_compose(args, ctx)


def compose_ps(
    ctx: ComposeContext,
    services: list[str] | None = None,
    all_containers: bool = False,
) -> str:
    """List containers using docker compose.

    When *services* is ``None`` or empty, all services in the Compose
    project are shown — including MCP sidecar containers.

    :param ctx: Pre-assembled compose context.
    :param services: Service names to list, or ``None`` for all.
    :param all_containers: Show all containers (including stopped).
    :returns: Command output.
    :raises ComposeError: If ps fails.
    """
    args = ["ps"]
    if all_containers:
        args.append("-a")
    if services:
        args.extend(services)

    result = _exec_compose(args, ctx, capture_output=True)
    return result.stdout


def compose_is_service_running(ctx: ComposeContext, service: str) -> bool:
    """Check if a service container is currently running.

    :param ctx: Pre-assembled compose context.
    :param service: Service name to check.
    :returns: ``True`` if the service has a running container.
    """
    args = ["ps", "--status", "running", "--format", "json", service]
    try:
        result = _exec_compose(args, ctx, capture_output=True)
    except ComposeError:
        return False
    output = result.stdout.strip()
    return len(output) > 0 and output != "[]"


def compose_exec(
    ctx: ComposeContext,
    service: str,
    command: list[str],
    no_tty: bool = False,
) -> None:
    """Execute a command in a running service container.

    :param ctx: Pre-assembled compose context.
    :param service: Service name to exec into.
    :param command: Command and arguments to run.
    :param no_tty: Disable pseudo-TTY allocation.
    :raises ComposeError: If exec fails.
    """
    args = ["exec"]
    if no_tty:
        args.append("-T")
    args.append(service)
    args.extend(command)

    _exec_compose(args, ctx)
