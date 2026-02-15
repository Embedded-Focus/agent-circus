"""Template file access utilities."""

import shutil
from contextlib import AbstractContextManager
from importlib.resources import as_file, files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Literal

TEMPLATES = files("agent_circus.templates")

# Mapping: template name -> target name (to restore dot prefixes)
TEMPLATE_MAPPINGS = [
    ("agent-circus", ".agent-circus"),
]


def _deploy_dir(
    src_dir: Path,
    dst_dir: Path,
    force: bool,
    deployed: list[Path],
) -> None:
    """Recursively deploy a directory, copying files as-is.

    :param src_dir: Source directory path.
    :type src_dir: Path
    :param dst_dir: Destination directory path.
    :type dst_dir: Path
    :param force: Overwrite existing files if True.
    :type force: bool
    :param deployed: List to append deployed paths to.
    :type deployed: list[Path]
    """
    dst_dir.mkdir(exist_ok=True)
    for item in src_dir.iterdir():
        dst_item = dst_dir / item.name
        if dst_item.exists() and not force:
            continue
        if item.is_dir():
            _deploy_dir(item, dst_item, force, deployed)
        else:
            shutil.copy2(item, dst_item)
            deployed.append(dst_item)


def template_dir_context() -> AbstractContextManager[Path, Literal[False]]:
    """Return a context manager providing a real filesystem Path to the bundled template directory.

    Used by instant mode to point docker compose at the package's
    embedded templates without copying them to the workspace.

    :returns: Context manager yielding the template directory path.
    :rtype: AbstractContextManager[Path]
    """
    return as_file(TEMPLATES.joinpath("agent-circus"))


def get_template_path(name: str) -> Traversable:
    """Get path to a template file or directory.

    :param name: Template name.
    :type name: str
    :returns: Traversable to template resource.
    :rtype: Traversable
    """
    return TEMPLATES.joinpath(name)


def deploy_templates(
    target_dir: Path,
    force: bool = False,
) -> list[Path]:
    """Deploy all template files to target directory.

    :param target_dir: Directory to deploy templates to.
    :type target_dir: Path
    :param force: Overwrite existing files if True.
    :type force: bool
    :returns: List of deployed paths.
    :rtype: list[Path]
    """
    deployed = []

    for src_name, dst_name in TEMPLATE_MAPPINGS:
        src = TEMPLATES.joinpath(src_name)
        dst = target_dir / dst_name

        with as_file(src) as src_path:
            if src_path.is_dir():
                _deploy_dir(src_path, dst, force, deployed)
            else:
                if dst.exists() and not force:
                    continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dst)
                deployed.append(dst)

    return deployed
