"""Smoke tests run against an installed distribution artifact."""

import subprocess
from importlib.metadata import version

from agent_circus.templates import get_template_path


def main() -> None:
    """Verify the installed package's metadata, CLI, and bundled templates."""
    assert version("agent-circus") == "0.1.0"
    assert get_template_path("agent-circus/compose.yaml").is_file()
    subprocess.run(["agent-circus", "--help"], check=True)


if __name__ == "__main__":
    main()
