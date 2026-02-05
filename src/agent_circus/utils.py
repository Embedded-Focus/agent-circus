"""Utility functions for remove_containers."""

import logging
import sys


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
