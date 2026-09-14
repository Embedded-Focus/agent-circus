"""Hatch build hook that embeds the source commit in distributions."""

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40,64}")


def source_commit(root: str) -> str | None:
    """Return the commit represented by the source tree.

    :param root: Project root passed to Hatch.
    :returns: Full Git commit hash, or ``None`` when it cannot be determined.
    """
    environment_commit = os.environ.get("GITHUB_SHA", "").lower()
    if _COMMIT_PATTERN.fullmatch(environment_commit):
        return environment_commit

    try:
        result = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
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


class CustomBuildHook(BuildHookInterface):
    """Embed Git build information in wheel and source distributions."""

    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        """Add a generated build information module to the artifact.

        :param version: Version of the artifact being built.
        :param build_data: Mutable Hatch build configuration.
        :returns: None.
        """
        del version
        commit = source_commit(self.root)
        if commit is None:
            return

        with tempfile.NamedTemporaryFile(
            mode="w", prefix="agent-circus-build-info-", suffix=".py", delete=False
        ) as generated:
            generated.write(
                '"""Build information generated when packaging Agent Circus."""\n\n'
                f'GIT_COMMIT: str | None = "{commit}"\n'
            )

        build_data["force_include"][generated.name] = "src/agent_circus/_build_info.py"

    def finalize(
        self, version: str, build_data: dict[str, Any], artifact_path: str
    ) -> None:
        """Remove the temporary generated module after packaging.

        :param version: Version of the built artifact.
        :param build_data: Hatch build configuration used for the artifact.
        :param artifact_path: Path to the completed artifact.
        :returns: None.
        """
        del version, artifact_path
        for source, destination in build_data["force_include"].items():
            if destination == "src/agent_circus/_build_info.py":
                Path(source).unlink(missing_ok=True)
