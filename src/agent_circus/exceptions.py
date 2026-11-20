"""Exceptions for agent-circus."""

from __future__ import annotations


class AgentCircusError(Exception):
    """Base class for all agent-circus errors."""


class ConfigurationError(AgentCircusError):
    """Raised when configuration is missing, malformed, or contradictory."""


class MsbNotFoundError(AgentCircusError):
    """Raised when no usable ``msb`` binary can be located."""


class MsbCommandError(AgentCircusError):
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
