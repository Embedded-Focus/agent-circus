"""Docker Compose operations for Agent Circus."""

import json
import logging
import os
import subprocess
from pathlib import Path

from .agent_config import build_agent_configs_override
from .config import (
    AVAILABLE_SERVICES,
    COMPOSE_FILE_NAME,
    build_agent_config_additions,
    load_config,
    resolve_config,
    sanitize_project_name,
)
from .exceptions import ComposeError, ConfigurationError
from .state import (
    get_agent_configs_dir,
    get_agent_configs_override_path,
    get_shadow_override_path,
)
from .templates import template_dir_context

logger = logging.getLogger(__name__)


def _build_shadow_override(shadow: list[str]) -> str:
    """Build a Docker Compose override YAML that shadows paths with /dev/null.

    For each path in *shadow*, every service gets a read-only bind mount
    of ``/dev/null`` over ``/workspace/<path>``.  When merged with the
    base compose file via ``-f``, these mounts take precedence over the
    workspace volume, effectively hiding the host files from the
    container.

    :param shadow: Workspace-relative paths to shadow.
    :type shadow: list[str]
    :returns: Compose override YAML string.
    :rtype: str
    """
    volumes = [f"/dev/null:/workspace/{p}:ro" for p in shadow]
    services = {name: {"volumes": volumes} for name in AVAILABLE_SERVICES}
    # Use JSON as a subset of YAML — avoids a PyYAML dependency.
    return json.dumps({"services": services})


def _exec_compose(
    args: list[str],
    workspace: Path,
    compose_file: Path,
    cwd: Path,
    capture_output: bool = False,
    env: dict[str, str] | None = None,
    shadow: list[str] | None = None,
    agent_config_additions: dict[str, dict] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute a docker compose command.

    :param args: Arguments to pass after ``docker compose -p ... -f ...``.
    :type args: list[str]
    :param workspace: Workspace path (used to derive the project name).
    :type workspace: Path
    :param compose_file: Absolute path to the compose file.
    :type compose_file: Path
    :param cwd: Working directory for the subprocess.
    :type cwd: Path
    :param capture_output: Capture stdout/stderr instead of streaming.
    :type capture_output: bool
    :param env: Environment variables for the subprocess, or None to
        inherit the current environment.
    :type env: dict[str, str] | None
    :param shadow: Workspace-relative paths to shadow with ``/dev/null``
        bind mounts.  A compose override file is written to the
        runtime state directory and passed as an additional ``-f``
        flag.  The file uses a deterministic path so that every
        ``docker compose`` invocation for the same workspace sees a
        consistent set of ``-f`` paths.
    :type shadow: list[str] | None
    :param agent_config_additions: Per-agent config additions to merge
        into each agent's original config file.  When present, merged
        configs are written to the state directory and bind-mounted
        into agent containers via a compose override.
    :type agent_config_additions: dict[str, dict] | None
    :returns: Completed process result.
    :rtype: subprocess.CompletedProcess[str]
    :raises ComposeError: If command fails.
    """
    cmd = [
        "docker",
        "compose",
        "-p",
        sanitize_project_name(workspace.name),
        "-f",
        str(compose_file),
    ]

    shadow_path = get_shadow_override_path(workspace)
    if shadow:
        override_content = _build_shadow_override(shadow)
        shadow_path.write_text(override_content)
        cmd.extend(["-f", str(shadow_path)])
        logger.debug("Shadow override: %s", shadow_path)
    else:
        # Remove a stale override from a previous run so Compose
        # does not accidentally pick it up.
        shadow_path.unlink(missing_ok=True)

    agent_configs_path = get_agent_configs_override_path(workspace)
    if agent_config_additions and any(agent_config_additions.values()):
        configs_dir = get_agent_configs_dir(workspace)
        override_content = build_agent_configs_override(
            agent_config_additions, configs_dir
        )
        agent_configs_path.write_text(override_content)
        cmd.extend(["-f", str(agent_configs_path)])
        logger.debug("Agent configs override: %s", agent_configs_path)
    else:
        agent_configs_path.unlink(missing_ok=True)

    cmd.extend(args)
    logger.debug("Running: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=capture_output,
            text=True,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            error_msg = result.stderr if capture_output else "Command failed"
            raise ComposeError(f"docker compose failed: {error_msg}")
        return result
    except FileNotFoundError as e:
        raise ComposeError("docker compose not found. Is Docker installed?") from e
    except subprocess.SubprocessError as e:
        raise ComposeError(f"Failed to run docker compose: {e}") from e


def _run_compose(
    args: list[str],
    workspace: Path,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a docker compose command, auto-detecting deploy or instant mode.

    Loads the merged configuration (user-global + project-local) and
    applies any ``shadow`` paths as ``/dev/null`` bind-mount overrides.

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
    config = load_config(workspace)
    shadow = config.get("shadow", [])
    agent_config_additions = build_agent_config_additions(config)

    config_dir = resolve_config(workspace)

    if config_dir is not None:
        compose_file = config_dir / COMPOSE_FILE_NAME
        return _exec_compose(
            args,
            workspace,
            compose_file,
            config_dir,
            capture_output,
            shadow=shadow,
            agent_config_additions=agent_config_additions,
        )

    with template_dir_context() as template_dir:
        compose_file = template_dir / COMPOSE_FILE_NAME
        env = os.environ.copy()
        env["AGENT_CIRCUS_WORKSPACE"] = str(workspace)
        return _exec_compose(
            args,
            workspace,
            compose_file,
            template_dir,
            capture_output,
            env=env,
            shadow=shadow,
            agent_config_additions=agent_config_additions,
        )


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


def compose_is_service_running(workspace: Path, service: str) -> bool:
    """Check if a service container is currently running.

    :param workspace: Workspace path.
    :type workspace: Path
    :param service: Service name to check.
    :type service: str
    :returns: True if the service has a running container.
    :rtype: bool
    """
    validate_services([service])
    args = ["ps", "--status", "running", "--format", "json", service]
    try:
        result = _run_compose(args, workspace, capture_output=True)
    except ComposeError:
        return False
    output = result.stdout.strip()
    return len(output) > 0 and output != "[]"


def compose_exec(
    workspace: Path,
    service: str,
    command: list[str],
    no_tty: bool = False,
) -> None:
    """Execute a command in a running service container.

    :param workspace: Workspace path.
    :type workspace: Path
    :param service: Service name to exec into.
    :type service: str
    :param command: Command and arguments to run.
    :type command: list[str]
    :param no_tty: Disable pseudo-TTY allocation.
    :type no_tty: bool
    :raises ComposeError: If exec fails.
    """
    validate_services([service])
    args = ["exec"]
    if no_tty:
        args.append("-T")
    args.append(service)
    args.extend(command)

    _run_compose(args, workspace)
