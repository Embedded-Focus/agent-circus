"""Custom exceptions for Agent Circus CLI."""


class AgentCircusError(Exception):
    """Base exception for all Agent Circus errors."""


class DockerConnectionError(AgentCircusError):
    """Raised when connection to Docker daemon fails."""


class ConfigurationError(AgentCircusError):
    """Raised when configuration is invalid or missing."""


class ContainerNotFoundError(AgentCircusError):
    """Raised when a required container is not found."""


class ContainerOperationError(AgentCircusError):
    """Raised when a container operation fails."""


class BuildError(AgentCircusError):
    """Raised when building container images fails."""


class ComposeError(AgentCircusError):
    """Raised when Docker Compose operations fail."""


class DevContainerError(AgentCircusError):
    """Raised when DevContainer operations fail."""
