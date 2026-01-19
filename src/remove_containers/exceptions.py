"""Custom exceptions for remove_containers operations."""


class RemoveContainersError(Exception):
    """Base exception for remove_containers operations."""


class DockerConnectionError(RemoveContainersError):
    """Raised when Docker daemon connection fails."""


class DevContainerNotFoundError(RemoveContainersError):
    """Raised when dev container cannot be found for workspace."""


class ProjectNameError(RemoveContainersError):
    """Raised when project name cannot be determined from container."""


class ContainerRemovalError(RemoveContainersError):
    """Raised when container removal fails."""
