"""Utility functions for msb-circus."""

import logging
import sys
from pathlib import Path


def setup_logging(
    level: str = "INFO",
    log_file: Path | None = None,
    to_stderr: bool = True,
) -> None:
    """Configure logging for the application.

    :param level: Log level name (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    :param log_file: Optional path to a log file. When set, logs are written
        to the file (and to stderr unless *to_stderr* is False).
    :param to_stderr: Also write logs to stderr. ACP mode must set this to
        False: the ACP backend (acp.el) treats every non-empty stderr
        chunk as an API error and shows an error dialog for it, so in ACP
        mode logs may only go to the log file.
    :returns: None
    :rtype: None
    :raises ValueError: If *to_stderr* is False and *log_file* is None;
        without a file the logs would be lost (or leak through the
        last-resort handler to stderr, exactly what ACP mode forbids).
    """
    handlers: list[logging.Handler] = []
    if to_stderr:
        handlers.append(logging.StreamHandler(sys.stderr))
    if log_file is not None:
        handlers.append(logging.FileHandler(log_file))
    if not handlers:
        raise ValueError(
            "setup_logging: to_stderr=False requires a log_file, "
            "otherwise logs would be lost"
        )

    logging.basicConfig(
        level=logging.getLevelNamesMapping()[level.upper()],
        format="[%(levelname)s] %(message)s",
        handlers=handlers,
        # Replace any handlers configured elsewhere (e.g. by an imported
        # library); otherwise basicConfig is a silent no-op and the CLI
        # options would have no effect.
        force=True,
    )
