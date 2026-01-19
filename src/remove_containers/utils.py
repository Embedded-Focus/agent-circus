"""Utility functions for remove_containers."""

import logging
import sys
from pathlib import Path


def get_workspace_path() -> Path:
    """Get absolute path to current workspace.

    :returns: Absolute Path to current working directory.
    :rtype: Path
    """
    return Path.cwd().resolve()


def setup_logging() -> None:
    """Configure logging for the application.

    :returns: None
    :rtype: None
    """
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
