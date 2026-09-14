"""Show the installed Agent Circus version."""

import json
import re
import subprocess
from importlib.metadata import distribution
from pathlib import Path
from urllib.parse import unquote, urlparse

import typer

from agent_circus import __version__
from agent_circus._build_info import GIT_COMMIT

_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40,64}")


def checkout_path() -> Path | None:
    """Return the source checkout used by a local directory installation.

    :returns: Checkout path, or ``None`` for an installed distribution artifact.
    """
    direct_url = distribution("agent-circus").read_text("direct_url.json")
    if direct_url is None:
        return None

    try:
        metadata = json.loads(direct_url)
        parsed_url = urlparse(metadata["url"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None

    if "dir_info" not in metadata or parsed_url.scheme != "file":
        return None
    return Path(unquote(parsed_url.path))


def checkout_commit(path: Path) -> str | None:
    """Return the current commit of a source checkout.

    :param path: Local installation source directory.
    :returns: Full Git commit hash, or ``None`` when it cannot be determined.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    commit = result.stdout.strip().lower()
    return (
        commit if result.returncode == 0 and _COMMIT_PATTERN.fullmatch(commit) else None
    )


def git_commit() -> str | None:
    """Return the checkout commit or the hash embedded during packaging.

    :returns: Full Git commit hash, or ``None`` if no hash is available.
    """
    path = checkout_path()
    if path is not None:
        commit = checkout_commit(path)
        if commit is not None:
            return commit
    return GIT_COMMIT


def version() -> None:
    """Show the installed version and exit."""
    commit = git_commit() or "unknown"
    typer.echo(f"agent-circus {__version__} (git {commit})")
