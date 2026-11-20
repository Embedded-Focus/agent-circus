"""msb-circus: manage AI coding-agent sandboxes with Microsandbox."""

from importlib.metadata import version as distribution_version

from .exceptions import (
    ConfigurationError,
    MsbCircusError,
    MsbCommandError,
    MsbNotFoundError,
)

__version__ = distribution_version("agent-circus")

__all__ = [
    "ConfigurationError",
    "MsbCircusError",
    "MsbCommandError",
    "MsbNotFoundError",
    "__version__",
]
