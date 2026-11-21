"""Smoke tests run against an installed distribution artifact."""

import subprocess
from importlib.metadata import version
from importlib.resources import files

from agent_circus import __version__

#: Files below ``agent_circus/templates`` that the CLI reads at run time.
BUNDLED_TEMPLATES = (
    "Dockerfile.msb-agents",
    "circus_router.py",
    "git-wrapper.sh",
    "dashboard/index.html",
    "dashboard/dashboard.js",
    "dashboard/dashboard.css",
    "dashboard/shell.html",
    "dashboard/shell.js",
    "dashboard/shell.css",
    "dashboard/shell-themes.js",
    "dashboard/favicon.png",
    "dashboard/vendor/xterm/xterm.js",
    "dashboard/vendor/xterm/xterm.css",
    "dashboard/vendor/xterm/addon-fit.js",
)


def main() -> None:
    """Verify the installed package's metadata, CLI, and bundled templates."""
    assert __version__ == version("agent-circus")
    templates = files("agent_circus") / "templates"
    for name in BUNDLED_TEMPLATES:
        assert (templates / name).is_file(), f"missing template: {name}"
    subprocess.run(["agent-circus", "--help"], check=True)
    subprocess.run(["agent-circus", "version"], check=True)


if __name__ == "__main__":
    main()
