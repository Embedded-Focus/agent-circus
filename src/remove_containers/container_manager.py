"""Container management business logic."""

import logging
from pathlib import Path

from .docker_client import ContainerWrapper, DockerClient
from .exceptions import (
    ContainerRemovalError,
    DevContainerNotFoundError,
    ProjectNameError,
)

logger = logging.getLogger(__name__)


def find_dev_container(client: DockerClient, workspace: Path) -> ContainerWrapper:
    """Find dev container for the specified workspace.

    :param client: Docker client instance.
    :type client: DockerClient
    :param workspace: Absolute path to workspace directory.
    :type workspace: Path
    :returns: Container wrapper for the dev container.
    :rtype: ContainerWrapper
    :raises DevContainerNotFoundError: If no dev container found for workspace.
    """
    labels = {"devcontainer.local_folder": str(workspace)}
    container = client.get_container_by_labels(labels)

    if container is None:
        raise DevContainerNotFoundError(
            f"No dev container found for workspace: {workspace}"
        )

    return container


def get_project_name(container: ContainerWrapper) -> str:
    """Extract Docker Compose project name from container.

    :param container: Container wrapper instance.
    :type container: ContainerWrapper
    :returns: Project name string.
    :rtype: str
    :raises ProjectNameError: If project name label is missing.
    """
    project = container.get_label("com.docker.compose.project")

    if not project:
        raise ProjectNameError(
            f"Could not determine project name from container {container.id}"
        )

    return project


def find_service_container(
    client: DockerClient, project: str, service: str
) -> ContainerWrapper | None:
    """Find container for a specific project and service.

    :param client: Docker client instance.
    :type client: DockerClient
    :param project: Docker Compose project name.
    :type project: str
    :param service: Service name.
    :type service: str
    :returns: Container wrapper or None if not found.
    :rtype: ContainerWrapper | None
    """
    labels = {
        "com.docker.compose.project": project,
        "com.docker.compose.service": service,
    }
    return client.get_container_by_labels(labels)


def remove_services(
    client: DockerClient, project: str, services: list[str]
) -> dict[str, bool]:
    """Remove containers for specified services.

    :param client: Docker client instance.
    :type client: DockerClient
    :param project: Docker Compose project name.
    :type project: str
    :param services: List of service names to remove.
    :type services: list[str]
    :returns: Dictionary mapping service name to removal success status.
    :rtype: dict[str, bool]
    """
    logger.info("Removing containers for services: %s.", ", ".join(services))
    results = {}

    for service in services:
        container = find_service_container(client, project, service)

        if container is None:
            logger.warning("No container found for service: %s", service)
            results[service] = False
            continue

        try:
            logger.info("Removing container %s (%s) ...", container.short_id, service)
            container.remove_force()
            results[service] = True
        except ContainerRemovalError as e:
            logger.error("Failed to remove container for %s: %s", service, e)
            results[service] = False

    return results
