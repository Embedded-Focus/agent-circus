"""Utility functions for remove_containers."""

import logging
import sys
from pathlib import Path


def setup_logging(
    level: str = "INFO",
    log_file: Path | None = None,
) -> None:
    """Configure logging for the application.

    :param level: Log level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    :param log_file: Optional path to a log file. When set, logs are written
        to both stdout and the file.
    :returns: None
    :rtype: None
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=logging.getLevelName(level.upper()),
        format="[%(levelname)s] %(message)s",
        handlers=handlers,
    )
