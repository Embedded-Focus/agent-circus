"""Installer sandbox flow for system skills.

Runs the ``skills`` CLI (npm, vercel-labs/skills) inside a short-lived
installer sandbox (``circus-skills``) that mounts the system skill store
read-write; the installer is the only sandbox with write access to the
store. This module is the orchestration layer: the typer command in
:mod:`msb_circus.cli` forwards its arguments here and maps
:class:`InstallerError` to CLI exit codes.
"""

from __future__ import annotations

import fcntl
import logging
import subprocess
import sys
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path

from .config import INIT_NONE, MsbConfig
from .exceptions import MsbCircusError
from .exec_ import build_exec_args
from .images import build_image_inspect_args
from .msb import MsbInvoker
from .sandbox import (
    build_list_args,
    build_remove_args,
    build_run_args,
    parse_sandbox_list_output,
)
from .system_skills import (
    INSTALLER_ENV,
    INSTALLER_SANDBOX_NAME,
    build_skills_command,
    ensure_store,
    installer_lock_path,
    installer_mounts,
    system_skills_store,
)

logger = logging.getLogger(__name__)


class InstallerError(MsbCircusError):
    """A skills installer step failed; carries the CLI exit code.

    :ivar exit_code: Exit code for the CLI (1 or 2).
    """

    def __init__(self, message: str, exit_code: int = 1) -> None:
        """Initialize the error.

        :param message: Human-readable error description.
        :param exit_code: Exit code for the CLI (1 or 2).
        """
        super().__init__(message)
        self.exit_code = exit_code


@contextmanager
def installer_lock(path: Path) -> Generator[None, None, None]:
    """Hold an exclusive host lock for the whole installer run.

    Only works between msb-circus processes on this host; the OS releases
    the lock when the process dies, so a crash leaves no stale lock. The
    lock file itself is kept (only its lock is released).

    :param path: Lock file path (see :func:`system_skills.installer_lock_path`).
    :raises InstallerError: When another process holds the lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(path, "a")  # noqa: SIM115 - closed in the finally below
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise InstallerError(
                f"another msb-circus skills command is running (lock: {path})"
            ) from e
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        fd.close()


def run_skills_installer(
    invoker: MsbInvoker, config: MsbConfig, args: Sequence[str]
) -> int:
    """Run the ``skills`` CLI in the short-lived installer sandbox.

    Boot a minimal sandbox (``circus-skills``) with only the system
    skill store mounted read-write, run the command interactively in it,
    and always remove the sandbox afterwards (also on Ctrl-C or a failed
    boot). The interactive exec deliberately bypasses
    :meth:`MsbInvoker.run`, which captures output and closes stdin.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param args: Forwarded user arguments (subcommand first).
    :returns: Exit code of the ``skills`` CLI.
    :raises InstallerError: On a disallowed subcommand (code 2), a
        missing image, an msb failure (list, leftover removal, boot)
        (code 1), or when another msb-circus skills command holds the
        installer lock (code 1).
    """
    try:
        command = build_skills_command(args)
    except ValueError as e:
        raise InstallerError(str(e), exit_code=2) from e
    store = system_skills_store(config.skills.store)
    try:
        ensure_store(store)
    except OSError as e:
        raise InstallerError(
            f"cannot create the system skill store {store}: {e}"
        ) from e
    probe = invoker.run(build_image_inspect_args(config.image.tag), check=False)
    if probe.returncode != 0:
        raise InstallerError(
            f"image {config.image.tag} is not in the msb cache. Run "
            "msb-circus build first."
        )
    with installer_lock(installer_lock_path()):
        try:
            result = invoker.run(build_list_args(running_only=False))
            sandboxes = parse_sandbox_list_output(result.stdout)
        except MsbCircusError as e:
            raise InstallerError(str(e)) from e
        # The lock is held, so any leftover installer sandbox is from a
        # dead run: remove it whatever its status.
        if any(s.name == INSTALLER_SANDBOX_NAME for s in sandboxes):
            try:
                invoker.run(build_remove_args(INSTALLER_SANDBOX_NAME, force=True))
            except MsbCircusError as e:
                raise InstallerError(
                    "cannot remove the leftover installer sandbox "
                    f"{INSTALLER_SANDBOX_NAME}: {e}"
                ) from e
        logger.info(
            "Running the skills CLI in sandbox %s (store: %s)...",
            INSTALLER_SANDBOX_NAME,
            store,
        )
        # The boot is inside the try: a failed or interrupted boot can leave
        # the sandbox behind, and the finally block removes it.
        try:
            try:
                invoker.run(
                    build_run_args(
                        name=INSTALLER_SANDBOX_NAME,
                        image=config.image.tag,
                        memory=config.sandbox.memory,
                        cpus=config.sandbox.cpus,
                        mounts=installer_mounts(store),
                        net=config.network.mode,
                        init=(
                            None
                            if config.sandbox.init == INIT_NONE
                            else config.sandbox.init
                        ),
                        trust_host_cas=config.network.trust_host_cas,
                        env=INSTALLER_ENV,
                        workdir="/root",
                    )
                )
            except MsbCircusError as e:
                raise InstallerError(
                    f"cannot boot the installer sandbox {INSTALLER_SANDBOX_NAME}: {e}"
                ) from e
            tty = True if sys.stdin.isatty() and sys.stdout.isatty() else None
            argv = [
                str(invoker.binary),
                *build_exec_args(
                    INSTALLER_SANDBOX_NAME, command, workdir="/root", tty=tty
                ),
            ]
            returncode = subprocess.run(argv, check=False).returncode
        finally:
            try:
                invoker.run(
                    build_remove_args(INSTALLER_SANDBOX_NAME, force=True), check=False
                )
            except MsbCircusError as e:
                logger.debug(
                    "Removing the installer sandbox %s failed: %s",
                    INSTALLER_SANDBOX_NAME,
                    e,
                )
    return returncode
