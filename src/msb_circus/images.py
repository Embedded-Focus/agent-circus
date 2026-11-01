"""Pure argv builders for image build (host) and import (msb).

Images are built on the host with podman (preferred) or docker, saved as a
docker-archive tar, and imported into msb with ``msb load``.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_BUILD_TARGET = "agents"
DEFAULT_SAVE_FORMAT = "docker-archive"
DEFAULT_TAR_NAME = "msb-agents.tar"
DEFAULT_CONTEXT = "."
DEFAULT_ENGINE = "podman"


def bundled_dockerfile_path() -> Path:
    """Return the path of the bundled ``Dockerfile.msb-agents`` template.

    The template ships inside the package (``msb_circus/templates/``), so
    this resolves both in source trees and installed wheels.

    :returns: Path to the bundled Dockerfile.
    """
    return Path(__file__).parent / "templates" / "Dockerfile.msb-agents"


def build_image_inspect_args(tag: str) -> list[str]:
    """Build argv for probing the msb image cache.

    ``msb image inspect`` exits 0 when the image is cached and 1 with
    ``image not found`` on stderr otherwise.

    :param tag: Image reference to probe.
    :returns: Argv list.
    """
    return ["image", "inspect", tag]


def build_podman_build_args(
    *,
    dockerfile: str,
    tag: str,
    platform: str | None = None,
    target: str = DEFAULT_BUILD_TARGET,
    context: str = DEFAULT_CONTEXT,
    engine: str = DEFAULT_ENGINE,
) -> list[str]:
    """Build argv for a host-side image build.

    :param dockerfile: Path to the Dockerfile.
    :param tag: Image tag to apply.
    :param platform: Optional ``--platform`` value (e.g. ``linux/amd64``).
    :param target: Multi-stage build target (the msb-agents template uses
        ``agents``).
    :param context: Build context directory.
    :param engine: Host container engine (``podman`` or ``docker``).
    :returns: Argv list.
    :raises ValueError: If *engine* is not ``podman`` or ``docker``.
    """
    if engine not in ("podman", "docker"):
        raise ValueError(f"Unsupported engine {engine!r}")
    args = [
        engine,
        "build",
        "--file",
        dockerfile,
        "--target",
        target,
    ]
    if platform:
        args += ["--platform", platform]
    args += ["--tag", tag, context]
    return args


def build_podman_save_args(
    tag: str, tar_path: str = DEFAULT_TAR_NAME, *, engine: str = "podman"
) -> list[str]:
    """Build argv for saving an image as a docker-archive tar.

    :param tag: Image tag to save.
    :param tar_path: Output tar path.
    :param engine: Host container engine (``podman`` or ``docker``).
    :returns: Argv list.
    :raises ValueError: If *engine* is not ``podman`` or ``docker``.
    """
    if engine not in ("podman", "docker"):
        raise ValueError(f"Unsupported engine {engine!r}")
    return [
        engine,
        "save",
        "--format",
        DEFAULT_SAVE_FORMAT,
        "--output",
        tar_path,
        tag,
    ]


def build_load_args(tar_path: str = DEFAULT_TAR_NAME) -> list[str]:
    """Build argv for ``msb load``.

    :param tar_path: Docker-archive tar to import.
    :returns: Argv list.
    """
    return ["load", "--input", tar_path]


def build_image_list_args() -> list[str]:
    """Build argv for listing locally cached images.

    :returns: Argv list producing machine-readable output.
    """
    return ["image", "list", "--format", "json"]
