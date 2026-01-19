"""Docker SDK wrapper with error handling."""

import docker
from docker.errors import DockerException
from docker.models.containers import Container

from .exceptions import ContainerRemovalError, DockerConnectionError


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

    def get_label(self, key: str) -> str | None:
        """Get a specific label value.

        :param key: Label key.
        :type key: str
        :returns: Label value or None if not found.
        :rtype: str | None
        """
        labels = self._container.labels or {}
        return labels.get(key)

    def remove_force(self) -> None:
        """Force remove this container.

        :returns: None
        :rtype: None
        :raises ContainerRemovalError: If removal fails.
        """
        try:
            self._container.remove(force=True)
        except DockerException as e:
            raise ContainerRemovalError(
                f"Failed to remove container {self.id}: {e}"
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
        self, labels: dict[str, str]
    ) -> ContainerWrapper | None:
        """Find a container matching all specified labels.

        :param labels: Dictionary of label key-value pairs to match.
        :type labels: dict[str, str]
        :returns: First matching container wrapper or None if no match found.
        :rtype: ContainerWrapper | None
        """
        label_filters = [f"{key}={value}" for key, value in labels.items()]

        containers = self._client.containers.list(filters={"label": label_filters})
        return ContainerWrapper(containers[0]) if containers else None

    def close(self) -> None:
        """Close the Docker client connection.

        :returns: None
        :rtype: None
        """
        self._client.close()

    def __enter__(self) -> "DockerClient":
        """Context manager entry.

        :returns: Self instance.
        :rtype: DockerClient
        """
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit.

        :param exc_type: Exception type.
        :param exc_val: Exception value.
        :param exc_tb: Exception traceback.
        :returns: None
        :rtype: None
        """
        self.close()
