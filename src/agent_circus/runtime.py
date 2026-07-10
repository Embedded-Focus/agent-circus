"""Container runtime selection for Agent Circus."""

import logging
import os
import subprocess
from typing import Literal

from .exceptions import ConfigurationError

ContainerRuntime = Literal["docker", "podman"]

DEFAULT_RUNTIME: ContainerRuntime = "docker"
RUNTIME_ENV_VAR = "AGENT_CIRCUS_RUNTIME"
SUPPORTED_RUNTIMES: tuple[ContainerRuntime, ...] = ("docker", "podman")

logger = logging.getLogger(__name__)


def validate_runtime(value: str) -> ContainerRuntime:
    """Validate a container runtime name.

    :param value: Raw runtime name.
    :returns: Validated runtime name.
    :raises ConfigurationError: If *value* is not supported.
    """
    if value == "docker":
        return "docker"
    if value == "podman":
        return "podman"
    supported = ", ".join(SUPPORTED_RUNTIMES)
    raise ConfigurationError(
        f"Unsupported runtime {value!r}. Supported runtimes: {supported}"
    )


def resolve_runtime(
    cli_runtime: str | None,
    config: dict,
    env: dict[str, str] | None = None,
) -> ContainerRuntime:
    """Resolve the effective container runtime.

    Resolution order is CLI option, ``AGENT_CIRCUS_RUNTIME``, config file,
    then the Docker default.

    :param cli_runtime: Runtime explicitly supplied on the command line.
    :param config: Merged Agent Circus configuration.
    :param env: Environment mapping, defaults to :data:`os.environ`.
    :returns: Effective container runtime.
    :raises ConfigurationError: If the selected runtime is unsupported.
    """
    environ = os.environ if env is None else env

    if cli_runtime:
        return validate_runtime(cli_runtime)

    env_runtime = environ.get(RUNTIME_ENV_VAR)
    if env_runtime:
        return validate_runtime(env_runtime)

    runtime_config = config.get("runtime")
    if isinstance(runtime_config, dict):
        config_runtime = runtime_config.get("engine")
        if config_runtime:
            if not isinstance(config_runtime, str):
                raise ConfigurationError("runtime.engine must be a string")
            return validate_runtime(config_runtime)

    return DEFAULT_RUNTIME


def compose_command(runtime: ContainerRuntime) -> list[str]:
    """Return the Compose command prefix for *runtime*.

    :param runtime: Effective container runtime.
    :returns: Command prefix used to invoke Compose.
    """
    if runtime == "podman":
        return ["podman", "compose"]
    return ["docker", "compose"]


def warn_if_experimental(runtime: ContainerRuntime) -> None:
    """Log a warning for experimental runtimes.

    :param runtime: Effective container runtime.
    """
    if runtime == "podman":
        logger.warning(
            "Podman runtime support is experimental. "
            "Some Docker Compose features may behave differently."
        )


def log_runtime_diagnostics(runtime: ContainerRuntime) -> None:
    """Log best-effort version and Compose-provider diagnostics for *runtime*.

    Podman's ``compose`` subcommand delegates to an external Compose
    provider it discovers on ``PATH`` (``podman-compose``, ``docker-compose``,
    or the ``docker compose`` CLI plugin), which can behave differently for
    things like interactive ``exec`` sessions. Logging which provider and
    version are actually in play helps diagnose runtime-specific failures.
    Never raises; failures are logged at debug level and otherwise ignored.

    :param runtime: Effective container runtime.
    """
    probes = [[runtime, "--version"], [*compose_command(runtime), "version"]]
    for probe in probes:
        try:
            result = subprocess.run(
                probe, capture_output=True, text=True, check=False, timeout=10
            )
            logger.debug(
                "%s -> exit code %s, stdout=%r, stderr=%r",
                " ".join(probe),
                result.returncode,
                result.stdout.strip(),
                result.stderr.strip(),
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug("%s failed: %s", " ".join(probe), e)
