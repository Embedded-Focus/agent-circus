"""Template file access utilities."""

import re
import shutil
from importlib.resources import as_file, files
from importlib.resources.abc import Traversable
from pathlib import Path

TEMPLATES = files("agent_circus.templates")

# Mapping: template name -> target name (to restore dot prefixes)
TEMPLATE_MAPPINGS = [
    ("agent-circus", ".agent-circus"),
]


def substitute_variables_str(value: str, variables: dict[str, str]) -> str:
    # Handle $${VAR} syntax - replace with value or empty string
    def replace_braced(match: re.Match[str]) -> str:
        return variables.get(match.group(1), "")

    value = re.sub(r"\$\$\{([^}]+)\}", replace_braced, value)

    # Handle $$VAR syntax - match variable names (letters, digits, underscores)
    def replace_unbraced(match: re.Match[str]) -> str:
        return variables.get(match.group(1), "")

    return re.sub(r"\$\$([A-Za-z_][A-Za-z0-9_]*)", replace_unbraced, value)


def _handle_others(src_path: Path, dst_path: Path, variables: dict[str, str]) -> None:
    dst_path.write_text(substitute_variables_str(src_path.read_text(), variables))
    shutil.copymode(src_path, dst_path)


def _deploy_dir(
    src_dir: Path,
    dst_dir: Path,
    variables: dict[str, str],
    force: bool,
    deployed: list[Path],
) -> None:
    """Recursively deploy a directory, applying filetype handlers to files.

    :param src_dir: Source directory path.
    :type src_dir: Path
    :param dst_dir: Destination directory path.
    :type dst_dir: Path
    :param variables: Mapping of variable names to values for substitution.
    :type variables: dict[str, str]
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
            _deploy_dir(item, dst_item, variables, force, deployed)
        else:
            _deploy_file(item, dst_item, variables)
            deployed.append(dst_item)


def _deploy_file(
    src_path: Path,
    dst_path: Path,
    variables: dict[str, str],
) -> None:
    """Deploy a single file, applying filetype handler if available.

    :param src_path: Source file path.
    :type src_path: Path
    :param dst_path: Destination file path.
    :type dst_path: Path
    :param variables: Mapping of variable names to values for substitution.
    :type variables: dict[str, str]
    """
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    _handle_others(src_path, dst_path, variables)


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
    variables: dict[str, str] | None = None,
) -> list[Path]:
    """Deploy all template files to target directory.

    :param target_dir: Directory to deploy templates to.
    :type target_dir: Path
    :param force: Overwrite existing files if True.
    :type force: bool
    :param variables: Mapping of variable names to values for substitution.
    :type variables: dict[str, str] | None
    :returns: List of deployed paths.
    :rtype: list[Path]
    """
    if variables is None:
        variables = {}
    deployed = []

    for src_name, dst_name in TEMPLATE_MAPPINGS:
        src = TEMPLATES.joinpath(src_name)
        dst = target_dir / dst_name

        with as_file(src) as src_path:
            if src_path.is_dir():
                _deploy_dir(src_path, dst, variables, force, deployed)
            else:
                if dst.exists() and not force:
                    continue
                _deploy_file(src_path, dst, variables)
                deployed.append(dst)

    return deployed
