"""Smoke tests run against an installed distribution artifact."""

import subprocess
from importlib.metadata import version
from importlib.resources import files

from agent_circus import __version__


def main() -> None:
    """Verify the installed package's metadata, CLI, and bundled templates."""
    assert __version__ == version("agent-circus")
    assert (files("agent_circus") / "templates" / "Dockerfile.msb-agents").is_file()
    subprocess.run(["agent-circus", "--help"], check=True)
    subprocess.run(["agent-circus", "version"], check=True)


if __name__ == "__main__":
    main()
