"""CLI entry point and main execution logic."""

import logging

from .container_manager import (
    find_dev_container,
    get_project_name,
    remove_services,
)
from .docker_client import DockerClient
from .exceptions import (
    DevContainerNotFoundError,
    DockerConnectionError,
    ProjectNameError,
)
from .utils import get_workspace_path, setup_logging

logger = logging.getLogger(__name__)


def run_cli() -> int:
    """Main CLI entry point.

    :returns: Exit code (0 for success, 1 for error).
    :rtype: int
    """
    setup_logging()

    try:
        with DockerClient() as client:
            workspace = get_workspace_path()

            try:
                dev_container = find_dev_container(client, workspace)
            except DevContainerNotFoundError as e:
                logger.info(str(e))
                return 0

            project = get_project_name(dev_container)
            logger.info("Found project: %s", project)

            services = ["claude-code", "codex", "mistral-vibe"]
            remove_services(client, project, services)

            logger.info("Done.")
            return 0

    except DockerConnectionError as e:
        logger.error(str(e))
        return 1
    except ProjectNameError as e:
        logger.error(str(e))
        return 1
    except Exception as e:
        logger.error("Unexpected error: %s", e)
        return 1
