"""Exceptions for msb-circus."""

from __future__ import annotations


class MsbCircusError(Exception):
    """Base class for all msb-circus errors."""


class ConfigurationError(MsbCircusError):
    """Raised when configuration is missing, malformed, or contradictory."""


class MsbNotFoundError(MsbCircusError):
    """Raised when no usable ``msb`` binary can be located."""


class MsbCommandError(MsbCircusError):
    """Raised when an ``msb`` invocation fails.

    :ivar returncode: Exit status of the failed process.
    :ivar stderr: Captured stderr, if any.
    """

    def __init__(self, message: str, *, returncode: int, stderr: str = "") -> None:
        """Initialize the error.

        :param message: Human-readable error description.
        :param returncode: Exit status of the failed process.
        :param stderr: Captured stderr, if any.
        """
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr
