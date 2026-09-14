"""Smoke tests run against an installed distribution artifact."""

import subprocess
from importlib.metadata import version

from agent_circus import __version__
from agent_circus.templates import get_template_path


def main() -> None:
    """Verify the installed package's metadata, CLI, and bundled templates."""
    assert __version__ == version("agent-circus")
    assert get_template_path("agent-circus/compose.yaml").is_file()
    subprocess.run(["agent-circus", "--help"], check=True)
    expected_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    version_output = subprocess.run(
        ["agent-circus", "version"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"(git {expected_commit})" in version_output


if __name__ == "__main__":
    main()
