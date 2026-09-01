"""Container runtime diagnostics for Agent Circus."""

import logging
import subprocess

logger = logging.getLogger(__name__)


def log_runtime_diagnostics() -> None:
    """Log best-effort Docker and Compose version diagnostics.

    Never raises; failures are logged at debug level and otherwise ignored.
    """
    probes = [["docker", "--version"], ["docker", "compose", "version"]]
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
