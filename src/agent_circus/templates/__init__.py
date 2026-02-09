"""Template file access utilities."""

import shutil
from importlib.resources import as_file, files
from importlib.resources.abc import Traversable
from pathlib import Path

TEMPLATES = files("agent_circus.templates")

# Mapping: template name -> target name (to restore dot prefixes)
TEMPLATE_MAPPINGS = [
    ("agent-circus", ".agent-circus"),
    ("envrc", ".envrc"),
    ("flake.nix", "flake.nix"),
    ("flake.lock", "flake.lock"),
]


def get_template_path(name: str) -> Traversable:
    """Get path to a template file or directory.

    :param name: Template name.
    :type name: str
    :returns: Traversable to template resource.
    :rtype: Traversable
    """
    return TEMPLATES.joinpath(name)


def deploy_templates(target_dir: Path, force: bool = False) -> list[Path]:
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

        if dst.exists() and not force:
            continue  # Skip existing files unless force=True

        with as_file(src) as src_path:
            if src_path.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src_path, dst)
            else:
                shutil.copy2(src_path, dst)

        deployed.append(dst)

    return deployed
