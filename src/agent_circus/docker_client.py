"""Docker SDK wrapper with error handling for Agent Circus."""

import logging
from pathlib import Path

import docker
from docker.errors import BuildError as DockerBuildError
from docker.errors import DockerException
from docker.models.containers import Container
from docker.models.images import Image

from .exceptions import (
    BuildError,
    ContainerOperationError,
    DockerConnectionError,
)

logger = logging.getLogger(__name__)


class ContainerWrapper:
    """Wrapper around docker.models.containers.Container with convenience methods."""

    def __init__(self, container: Container) -> None:
        """Initialize container wrapper.

        :param container: Docker SDK Container instance.
        :type container: Container
        """
        self._container = container

    @property
    def id(self) -> str:
        """Get container ID.

        :returns: Container ID.
        :rtype: str
        """
        return self._container.id

    @property
    def short_id(self) -> str:
        """Get short container ID.

        :returns: Short container ID.
        :rtype: str
        """
        return self._container.short_id

    @property
    def name(self) -> str:
        """Get container name.

        :returns: Container name.
        :rtype: str
        """
        return self._container.name

    @property
    def status(self) -> str:
        """Get container status.

        :returns: Container status (running, exited, etc.).
        :rtype: str
        """
        return self._container.status

    @property
    def labels(self) -> dict[str, str]:
        """Get container labels.

        :returns: Dictionary of container labels.
        :rtype: dict[str, str]
        """
        return self._container.labels or {}

    def get_label(self, key: str) -> str | None:
        """Get a specific label value.

        :param key: Label key.
        :type key: str
        :returns: Label value or None if not found.
        :rtype: str | None
        """
        return self.labels.get(key)

    def start(self) -> None:
        """Start this container.

        :raises ContainerOperationError: If start fails.
        """
        try:
            self._container.start()
        except DockerException as e:
            raise ContainerOperationError(
                f"Failed to start container {self.short_id}: {e}"
            ) from e

    def stop(self, timeout: int = 10) -> None:
        """Stop this container.

        :param timeout: Seconds to wait before killing.
        :type timeout: int
        :raises ContainerOperationError: If stop fails.
        """
        try:
            self._container.stop(timeout=timeout)
        except DockerException as e:
            raise ContainerOperationError(
                f"Failed to stop container {self.short_id}: {e}"
            ) from e

    def remove(self, force: bool = False, v: bool = False) -> None:
        """Remove this container.

        :param force: Force removal of running container.
        :type force: bool
        :param v: Remove associated volumes.
        :type v: bool
        :raises ContainerOperationError: If removal fails.
        """
        try:
            self._container.remove(force=force, v=v)
        except DockerException as e:
            raise ContainerOperationError(
                f"Failed to remove container {self.short_id}: {e}"
            ) from e

    def reload(self) -> None:
        """Reload container data from server.

        :raises ContainerOperationError: If reload fails.
        """
        try:
            self._container.reload()
        except DockerException as e:
            raise ContainerOperationError(
                f"Failed to reload container {self.short_id}: {e}"
            ) from e


class DockerClient:
    """Wrapper around docker.DockerClient with error handling and convenience methods."""

    def __init__(self) -> None:
        """Initialize Docker client and verify connectivity.

        :raises DockerConnectionError: If Docker daemon is not accessible.
        """
        try:
            self._client = docker.from_env()
            self._client.ping()
        except DockerException as e:
            raise DockerConnectionError(
                f"Failed to connect to Docker daemon: {e}"
            ) from e

    @property
    def client(self) -> docker.DockerClient:
        """Get the underlying Docker client instance.

        :returns: Docker client instance.
        :rtype: docker.DockerClient
        """
        return self._client

    def get_container_by_labels(
        self, labels: dict[str, str], all_containers: bool = False
    ) -> ContainerWrapper | None:
        """Find a container matching all specified labels.

        :param labels: Dictionary of label key-value pairs to match.
        :type labels: dict[str, str]
        :param all_containers: Include stopped containers.
        :type all_containers: bool
        :returns: First matching container wrapper or None if no match found.
        :rtype: ContainerWrapper | None
        """
        label_filters = [f"{key}={value}" for key, value in labels.items()]
        containers = self._client.containers.list(
            filters={"label": label_filters}, all=all_containers
        )
        return ContainerWrapper(containers[0]) if containers else None

    def get_containers_by_labels(
        self, labels: dict[str, str], all_containers: bool = False
    ) -> list[ContainerWrapper]:
        """Find all containers matching specified labels.

        :param labels: Dictionary of label key-value pairs to match.
        :type labels: dict[str, str]
        :param all_containers: Include stopped containers.
        :type all_containers: bool
        :returns: List of matching container wrappers.
        :rtype: list[ContainerWrapper]
        """
        label_filters = [f"{key}={value}" for key, value in labels.items()]
        containers = self._client.containers.list(
            filters={"label": label_filters}, all=all_containers
        )
        return [ContainerWrapper(c) for c in containers]

    def get_project_containers(
        self, project_name: str, all_containers: bool = False
    ) -> list[ContainerWrapper]:
        """Get all containers for a Docker Compose project.

        :param project_name: Docker Compose project name.
        :type project_name: str
        :param all_containers: Include stopped containers.
        :type all_containers: bool
        :returns: List of container wrappers.
        :rtype: list[ContainerWrapper]
        """
        return self.get_containers_by_labels(
            {"com.docker.compose.project": project_name}, all_containers=all_containers
        )

    def get_service_container(
        self, project_name: str, service_name: str, all_containers: bool = False
    ) -> ContainerWrapper | None:
        """Get container for a specific service in a project.

        :param project_name: Docker Compose project name.
        :type project_name: str
        :param service_name: Service name.
        :type service_name: str
        :param all_containers: Include stopped containers.
        :type all_containers: bool
        :returns: Container wrapper or None if not found.
        :rtype: ContainerWrapper | None
        """
        return self.get_container_by_labels(
            {
                "com.docker.compose.project": project_name,
                "com.docker.compose.service": service_name,
            },
            all_containers=all_containers,
        )

    def build_image(
        self,
        path: Path,
        dockerfile: str = "Dockerfile",
        tag: str | None = None,
        target: str | None = None,
        buildargs: dict[str, str] | None = None,
        nocache: bool = False,
    ) -> Image:
        """Build a Docker image.

        :param path: Path to build context.
        :type path: Path
        :param dockerfile: Dockerfile name relative to path.
        :type dockerfile: str
        :param tag: Image tag.
        :type tag: str | None
        :param target: Target build stage.
        :type target: str | None
        :param buildargs: Build arguments.
        :type buildargs: dict[str, str] | None
        :param nocache: Disable build cache.
        :type nocache: bool
        :returns: Built image.
        :rtype: Image
        :raises BuildError: If build fails.
        """
        try:
            image, logs = self._client.images.build(
                path=str(path),
                dockerfile=dockerfile,
                tag=tag,
                target=target,
                buildargs=buildargs,
                nocache=nocache,
                rm=True,
            )
            return image
        except DockerBuildError as e:
            raise BuildError(f"Failed to build image: {e}") from e
        except DockerException as e:
            raise BuildError(f"Docker error during build: {e}") from e

    def close(self) -> None:
        """Close the Docker client connection."""
        self._client.close()

    def __enter__(self) -> "DockerClient":
        """Context manager entry.

        :returns: Self instance.
        :rtype: DockerClient
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()
