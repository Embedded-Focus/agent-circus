"""DevContainer CLI operations for Agent Circus."""

import logging
import subprocess
from pathlib import Path

from .config import get_config_dir, get_devcontainer_file
from .exceptions import ConfigurationError, DevContainerError

logger = logging.getLogger(__name__)


def _run_devcontainer(
    commands: list[str],
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
    devcontainer_json = get_devcontainer_file(workspace)
    if not devcontainer_json.is_file():
        raise ConfigurationError(f"Compose file not found: {devcontainer_json}")

    cmd = [
        "devcontainer",
        *commands,
        "--workspace-folder",
        str(workspace),
        "--override-config",
        str(devcontainer_json),
    ]
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
            raise DevContainerError(f"devcontainer failed: {error_msg}")
        return result
    except FileNotFoundError as e:
        raise DevContainerError(
            "DevContainer not found. Is the DevContainer CLI installed?"
        ) from e
    except subprocess.SubprocessError as e:
        raise DevContainerError(f"Failed to run devcontainer: {e}") from e


def devcontainer_up(workspace: Path) -> None:
    """Start services using docker compose.

    :param workspace: Workspace path.
    :type workspace: Path
    :raises DevContainerError: If up fails.
    """
    _run_devcontainer(["up"], workspace)
