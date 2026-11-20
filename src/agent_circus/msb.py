"""Locating and invoking the ``msb`` binary.

Resolution order (first match wins):

1. Explicit ``[msb] binary`` from configuration.
2. The binary bundled in the ``microsandbox`` wheel
   (``microsandbox._microsandbox.resolved_cli_msb_path()``).
3. ``msb`` on ``PATH``.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path

from .exceptions import MsbCommandError, MsbNotFoundError

logger = logging.getLogger(__name__)

_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:[+-][\w.]+)?")

#: Known msb failures and actionable host-side remedies, matched against
#: stderr substrings. Order matters: first match wins.
REMEDIATION_HINTS: tuple[tuple[str, str], ...] = (
    (
        "permits path substitution",
        (
            "An ancestor of the msb storage root is group- or world-writable. "
            "Fix the directory named in the error with: "
            "chmod g-w,o-w <directory> (check for a permissive umask, "
            "e.g. 0002, in your shell)."
        ),
    ),
    (
        "is not owned by this account",
        (
            "The msb storage root is owned by another account. Run as its "
            "owner, chown it, or point MSB_HOME at a separate storage root."
        ),
    ),
    (
        "is not private to the user",
        (
            "A msb storage directory is readable by other accounts. Fix "
            "with: chmod go-rwx <directory>."
        ),
    ),
    (
        " is running",
        (
            "The sandbox appears to be running. Re-run with --force to stop "
            "it before removal."
        ),
    ),
)


def expected_msb_version() -> str | None:
    """Return the msb version to expect for drift warnings.

    Uses the installed ``microsandbox`` distribution version, because the
    bundled ``msb`` binary always matches it.

    :returns: Expected version string (e.g. ``"X.Y.Z"``), or ``None`` if
        the distribution metadata is unavailable.
    """
    try:
        return _dist_version("microsandbox")
    except PackageNotFoundError:
        return None


def _bundled_msb_path() -> Path | None:
    """Return the msb binary bundled in the microsandbox wheel, if any.

    :returns: Path to the bundled binary, or ``None`` if unavailable.
    """
    try:
        from microsandbox._microsandbox import resolved_cli_msb_path
    except ImportError:
        return None
    path = Path(resolved_cli_msb_path())
    return path if path.is_file() else None


def resolve_msb_binary(config_binary: str | None = None) -> Path:
    """Locate a usable ``msb`` binary.

    :param config_binary: Explicit binary path from configuration.
    :returns: Path to the resolved binary.
    :raises MsbNotFoundError: If no usable binary can be found.
    """
    if config_binary:
        path = Path(config_binary)
        if not path.is_file():
            raise MsbNotFoundError(
                f"Configured msb binary {config_binary!r} does not exist"
            )
        return path

    bundled = _bundled_msb_path()
    if bundled is not None:
        return bundled

    on_path = shutil.which("msb")
    if on_path:
        return Path(on_path)

    raise MsbNotFoundError(
        "No msb binary found: install the 'microsandbox' Python package "
        "(which bundles msb), put 'msb' on PATH, or set [msb] binary in "
        "the config file"
    )


def probe_version(binary: Path) -> str | None:
    """Return the msb version string reported by the binary.

    :param binary: Path to the ``msb`` binary.
    :returns: Version string (e.g. ``"X.Y.Z"``), or ``None`` if it could
        not be determined.
    """
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("msb --version failed for %s: %s", binary, e)
        return None
    match = _VERSION_PATTERN.search(result.stdout)
    return match.group(0) if match else None


def is_bundled_msb(binary: Path) -> bool:
    """Check whether *binary* is the one bundled in the microsandbox wheel.

    :param binary: Resolved msb binary path.
    :returns: True if the binary is the bundled one.
    """
    bundled = _bundled_msb_path()
    return bundled is not None and binary == bundled


def check_version(binary: Path, expected: str | None = None) -> str | None:
    """Probe the msb version and warn on drift from the expected version.

    The bundled wheel binary always matches its distribution version, so
    the drift check is skipped silently for it; it remains active for
    binaries resolved from ``PATH`` or configuration. Never raises; a
    missing version is only logged.

    :param binary: Path to the ``msb`` binary.
    :param expected: Version to compare against; defaults to
        :func:`expected_msb_version`. The drift check is skipped when no
        expected version is available.
    :returns: The probed version string, or ``None``.
    """
    version = probe_version(binary)
    if version is None:
        logger.warning("Could not determine the msb version of %s", binary)
        return None
    if is_bundled_msb(binary):
        # The bundled binary matches its wheel by construction; there is
        # nothing worth logging on every invocation.
        return version
    if expected is None:
        expected = expected_msb_version()
    if expected is None:
        logger.debug(
            "Skipping msb version drift check for %s: installed microsandbox "
            "version is unknown",
            binary,
        )
        return version
    if version != expected:
        logger.warning(
            "msb version %s differs from the version msb-circus is tested "
            "against (%s); CLI flags may have changed",
            version,
            expected,
        )
    else:
        logger.debug("msb version %s matches the expected version", version)
    return version


def remediation_hint(stderr: str) -> str:
    """Return an actionable hint for a known msb failure, if any.

    :param stderr: Captured stderr of the failed msb invocation.
    :returns: A hint string, or an empty string when the failure is not
        recognized.
    """
    for needle, hint in REMEDIATION_HINTS:
        if needle in stderr:
            return f" Hint: {hint}"
    return ""


class MsbInvoker:
    """Thin subprocess wrapper around the ``msb`` CLI.

    Keeps argv construction (pure builder functions) separate from
    execution so builders stay unit-testable without msb installed.
    """

    def __init__(self, binary: Path) -> None:
        """Store the binary to invoke.

        :param binary: Path to the ``msb`` binary.
        """
        self.binary = binary

    def run(
        self,
        args: list[str],
        *,
        capture_output: bool = True,
        timeout: float | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run an ``msb`` subcommand.

        The child's stdin is connected to :data:`os.devnull`. Piped ``msb
        exec`` invocations read host stdin unconditionally (forwarding it to
        the guest), so an inherited stdin would steal bytes from a concurrent
        ACP stream on the same descriptor — e.g. the editor's initialize
        request would be consumed by a dockerd probe instead of reaching the
        agent. Interactive sessions do not go through this method.

        :param args: Full argv including the subcommand (e.g. ``["run", ...]``).
        :param capture_output: Whether to capture stdout/stderr instead of
            letting them through to the terminal.
        :param timeout: Optional timeout in seconds.
        :param check: Whether to raise on a non-zero exit status. Set to
            ``False`` for probes (e.g. ``image inspect``) where the exit
            status is meaningful data.
        :returns: The completed process.
        :raises MsbCommandError: If *check* is true and the process exits
            non-zero.
        """
        argv = [str(self.binary), *args]
        logger.debug("Running: %s", " ".join(argv))
        try:
            result = subprocess.run(
                argv,
                capture_output=capture_output,
                stdin=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=timeout,
            )
        except OSError as e:
            raise MsbCommandError(
                f"Failed to execute {self.binary}: {e}", returncode=-1
            ) from e
        except subprocess.TimeoutExpired as e:
            raise MsbCommandError(
                f"msb {args[0] if args else ''} timed out", returncode=-1
            ) from e
        if check and result.returncode != 0:
            stderr = result.stderr or ""
            raise MsbCommandError(
                f"msb {' '.join(args[:2])} failed with exit code "
                f"{result.returncode}: {stderr.strip()}"
                f"{remediation_hint(stderr)}",
                returncode=result.returncode,
                stderr=stderr,
            )
        return result
