"""Docker Compose operations for Agent Circus."""

import logging
import subprocess
from pathlib import Path

from .config import AVAILABLE_SERVICES, get_compose_file, get_config_dir
from .exceptions import ComposeError, ConfigurationError

logger = logging.getLogger(__name__)


def _run_compose(
    args: list[str],
    workspace: Path,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a docker compose command.

    :param args: Arguments to pass to docker compose.
    :type args: list[str]
    :param workspace: Workspace path.
    :type workspace: Path
    :param capture_output: Capture stdout/stderr instead of streaming.
    :type capture_output: bool
    :returns: Completed process result.
    :rtype: subprocess.CompletedProcess[str]
    :raises ComposeError: If command fails.
    """
    compose_file = get_compose_file(workspace)
    if not compose_file.is_file():
        raise ConfigurationError(f"Compose file not found: {compose_file}")

    cmd = ["docker", "compose", "-f", str(compose_file), *args]
    logger.debug("Running: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            cwd=str(get_config_dir(workspace)),
            capture_output=capture_output,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            error_msg = result.stderr if capture_output else "Command failed"
            raise ComposeError(f"docker compose failed: {error_msg}")
        return result
    except FileNotFoundError as e:
        raise ComposeError("docker compose not found. Is Docker installed?") from e
    except subprocess.SubprocessError as e:
        raise ComposeError(f"Failed to run docker compose: {e}") from e


def validate_services(services: list[str]) -> list[str]:
    """Validate and return service names.

    :param services: List of service names to validate.
    :type services: list[str]
    :returns: Validated list of services.
    :rtype: list[str]
    :raises ConfigurationError: If invalid service name provided.
    """
    if not services:
        return AVAILABLE_SERVICES.copy()

    invalid = set(services) - set(AVAILABLE_SERVICES)
    if invalid:
        raise ConfigurationError(
            f"Invalid service(s): {', '.join(invalid)}. "
            f"Available: {', '.join(AVAILABLE_SERVICES)}"
        )
    return services


def compose_build(
    workspace: Path,
    services: list[str] | None = None,
    no_cache: bool = False,
) -> None:
    """Build service images using docker compose.

    :param workspace: Workspace path.
    :type workspace: Path
    :param services: Services to build, or None for all.
    :type services: list[str] | None
    :param no_cache: Disable build cache.
    :type no_cache: bool
    :raises ComposeError: If build fails.
    """
    services = validate_services(services or [])
    args = ["build"]
    if no_cache:
        args.append("--no-cache")
    args.extend(services)

    logger.info("Building services: %s", ", ".join(services))
    _run_compose(args, workspace)


def compose_up(
    workspace: Path,
    services: list[str] | None = None,
    detach: bool = True,
    build: bool = False,
) -> None:
    """Start services using docker compose.

    :param workspace: Workspace path.
    :type workspace: Path
    :param services: Services to start, or None for all.
    :type services: list[str] | None
    :param detach: Run in detached mode.
    :type detach: bool
    :param build: Build images before starting.
    :type build: bool
    :raises ComposeError: If up fails.
    """
    services = validate_services(services or [])
    args = ["up"]
    if detach:
        args.append("-d")
    if build:
        args.append("--build")
    args.extend(services)

    logger.info("Starting services: %s", ", ".join(services))
    _run_compose(args, workspace)


def compose_down(
    workspace: Path,
    volumes: bool = False,
    remove_orphans: bool = False,
    timeout: int | None = None,
) -> None:
    """Stop and remove containers using docker compose.

    :param workspace: Workspace path.
    :type workspace: Path
    :param volumes: Remove named volumes.
    :type volumes: bool
    :param remove_orphans: Remove orphan containers.
    :type remove_orphans: bool
    :param timeout: Seconds to wait for containers to stop (``-t``).
        Use ``0`` to skip graceful shutdown and kill immediately.
    :type timeout: int | None
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
    _run_compose(args, workspace)


def compose_ps(
    workspace: Path,
    services: list[str] | None = None,
    all_containers: bool = False,
) -> str:
    """List containers using docker compose.

    :param workspace: Workspace path.
    :type workspace: Path
    :param services: Services to list, or None for all.
    :type services: list[str] | None
    :param all_containers: Show all containers (including stopped).
    :type all_containers: bool
    :returns: Command output.
    :rtype: str
    :raises ComposeError: If ps fails.
    """
    services = validate_services(services or [])
    args = ["ps"]
    if all_containers:
        args.append("-a")
    args.extend(services)

    result = _run_compose(args, workspace, capture_output=True)
    return result.stdout


def compose_logs(
    workspace: Path,
    services: list[str] | None = None,
    follow: bool = False,
    tail: int | None = None,
) -> None:
    """Show logs from containers.

    :param workspace: Workspace path.
    :type workspace: Path
    :param services: Services to show logs for, or None for all.
    :type services: list[str] | None
    :param follow: Follow log output.
    :type follow: bool
    :param tail: Number of lines to show from end.
    :type tail: int | None
    :raises ComposeError: If logs fails.
    """
    services = validate_services(services or [])
    args = ["logs"]
    if follow:
        args.append("-f")
    if tail is not None:
        args.extend(["--tail", str(tail)])
    args.extend(services)

    _run_compose(args, workspace)
