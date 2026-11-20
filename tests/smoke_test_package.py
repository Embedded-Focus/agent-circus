"""Smoke tests run against an installed distribution artifact."""

import subprocess
from importlib.resources import files

from msb_circus import __version__


def main() -> None:
    """Verify the installed package's CLI and bundled templates."""
    assert __version__.count(".") == 2
    assert (files("msb_circus") / "templates" / "Dockerfile.msb-agents").is_file()
    subprocess.run(["msb-circus", "--help"], check=True)
    subprocess.run(["msb-circus", "version"], check=True)


if __name__ == "__main__":
    main()
