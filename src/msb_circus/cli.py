"""Main CLI entry point for msb-circus."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import (
    MsbConfig,
    effective_mounts,
    load_config,
    sandbox_name_for_workspace,
)
from .envs import parse_env_assignments, resolve_passthrough
from .exceptions import ConfigurationError, MsbCircusError
from .exec_ import build_exec_args, run_acp_stream
from .images import (
    build_image_inspect_args,
    build_load_args,
    build_podman_build_args,
    build_podman_save_args,
    bundled_dockerfile_path,
)
from .msb import MsbInvoker, check_version, resolve_msb_binary
from .sandbox import (
    SandboxInfo,
    build_list_args,
    build_remove_args,
    build_run_args,
    build_start_args,
    build_stop_args,
    parse_sandbox_list_output,
)

app = typer.Typer(
    name="msb-circus",
    help="Manage AI coding-agent sandboxes with Microsandbox.",
    no_args_is_help=True,
)

logger = logging.getLogger(__name__)

#: Budget for the post-boot/start dockerd readiness probe.
DOCKERD_PROBE_ATTEMPTS = 20
DOCKERD_PROBE_DELAY = 1.5


@app.callback()
def main(
    ctx: typer.Context,
    log_level: Annotated[
        str | None,
        typer.Option(
            "--log-level",
            envvar="LOGLEVEL",
            help="Set the log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
        ),
    ] = None,
) -> None:
    """Manage AI coding-agent sandboxes with Microsandbox."""
    logging.basicConfig(
        level=log_level or "INFO", format="%(levelname)s %(name)s: %(message)s"
    )


@app.command()
def version(
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory path.",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = Path("."),
) -> None:
    """Show msb-circus and msb versions, and verify they match."""
    print(f"msb-circus {__version__}")
    config = load_config(workspace)
    try:
        binary = resolve_msb_binary(config.binary)
    except MsbCircusError as e:
        print(f"msb binary: not found ({e})")
        raise typer.Exit(code=1) from e
    print(f"msb binary: {binary}")
    actual = check_version(binary)
    print(f"msb version: {actual or 'unknown'}")


def _list_sandboxes(invoker: MsbInvoker, *, running_only: bool) -> list[SandboxInfo]:
    """Query and parse ``msb list`` output.

    :param invoker: Invoker bound to the resolved msb binary.
    :param running_only: Restrict the query to running sandboxes.
    :returns: Parsed sandbox entries.
    :raises MsbCircusError: If the msb invocation or parsing fails.
    """
    result = invoker.run(build_list_args(running_only=running_only))
    return parse_sandbox_list_output(result.stdout)


def _format_sandbox_row(sandbox: SandboxInfo) -> str:
    """Format one sandbox entry for display.

    :param sandbox: Entry to format.
    :returns: Human-readable single-line summary.
    """
    parts = [sandbox.name, sandbox.status or "unknown"]
    if sandbox.id:
        parts.append(sandbox.id[:12])
    if sandbox.created_at is not None:
        created = datetime.fromtimestamp(sandbox.created_at, tz=UTC)
        parts.append(created.strftime("%Y-%m-%d %H:%M UTC"))
    return "  ".join(parts)


@app.command(name="ps")
def ps(
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory path.",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = Path("."),
    show_all: Annotated[
        bool,
        typer.Option(
            "--all",
            "-a",
            help="Show every msb sandbox, not only this workspace's.",
        ),
    ] = False,
    running_only: Annotated[
        bool,
        typer.Option(
            "--running",
            help="Only list running sandboxes (default: all states).",
        ),
    ] = False,
) -> None:
    """Show sandbox status for this workspace (or all sandboxes).

    By default only this workspace's sandbox (``agents-<project>``) is
    shown, in any state. ``--all`` lists every sandbox msb knows about.
    """
    config = load_config(workspace)
    binary = resolve_msb_binary(config.binary)
    check_version(binary)
    invoker = MsbInvoker(binary)
    try:
        sandboxes = _list_sandboxes(invoker, running_only=running_only)
    except MsbCircusError as e:
        print(f"Error: {e}")
        raise typer.Exit(code=1) from e

    if show_all:
        selected = sandboxes
    else:
        name = sandbox_name_for_workspace(workspace)
        selected = [s for s in sandboxes if s.name == name]
        if not selected:
            print(f"No sandbox {name!r} for this workspace yet.")
            return

    if not selected:
        print("No sandboxes found.")
        return
    for sandbox in selected:
        print(_format_sandbox_row(sandbox))


def _resolve_invoker(workspace: Path) -> tuple[MsbConfig, MsbInvoker]:
    """Load config for *workspace* and build an msb invoker.

    :param workspace: Workspace directory.
    :returns: Tuple of the parsed config and a bound invoker.
    :raises MsbCircusError: If configuration or binary resolution fails.
    """
    config = load_config(workspace)
    binary = resolve_msb_binary(config.binary)
    check_version(binary)
    return config, MsbInvoker(binary)


@app.command()
def remove(
    names: Annotated[
        list[str] | None,
        typer.Argument(
            help=(
                "Sandboxes to remove. Defaults to this workspace's sandbox "
                "(agents-<project>)."
            ),
        ),
    ] = None,
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory path.",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = Path("."),
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Skip confirmation and stop running sandboxes before removal.",
        ),
    ] = False,
) -> None:
    """Remove agent sandboxes and their guest state.

    Host-mounted files (e.g. the workspace bind mount) are not affected.
    Without names, this workspace's sandbox is removed. Without --force,
    a confirmation prompt is shown and a running sandbox is not stopped.
    """
    _config, invoker = _resolve_invoker(workspace)
    targets = names or [sandbox_name_for_workspace(workspace)]

    if not force:
        typer.echo(f"This will remove sandbox(es): {', '.join(targets)}.")
        if not typer.confirm("Are you sure you want to continue?"):
            typer.echo("Aborted.")
            raise typer.Exit(code=0)

    for target in targets:
        try:
            invoker.run(build_remove_args(target, force=force))
        except MsbCircusError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e
        typer.echo(f"✓ Removed {target}")


app.command(name="rm", hidden=True)(remove)


def _run_host(args: list[str]) -> None:
    """Run a host command (e.g. podman) inheriting stdout/stderr.

    :param args: Full argv of the host command.
    :raises typer.Exit: If the command exits non-zero.
    """
    logger.debug("Running host command: %s", " ".join(args))
    try:
        result = subprocess.run(args, check=False)
    except FileNotFoundError as e:
        typer.echo(
            f"Error: {args[0]!r} not found on PATH; install it or pick a "
            "different --engine.",
            err=True,
        )
        raise typer.Exit(code=1) from e
    if result.returncode != 0:
        typer.echo(
            f"Error: {' '.join(args[:2])} failed with exit code {result.returncode}",
            err=True,
        )
        raise typer.Exit(code=1)


@app.command()
def build(
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory used as build context.",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = Path("."),
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Rebuild and re-import even if the image is already cached.",
        ),
    ] = False,
    engine: Annotated[
        str,
        typer.Option(
            "--engine",
            help="Host container engine used to build the image: podman or docker.",
        ),
    ] = "podman",
    dockerfile: Annotated[
        Path | None,
        typer.Option(
            "--dockerfile",
            help=(
                "Dockerfile to build. Defaults to [msb.image] dockerfile from "
                "the config, then the bundled msb-agents template."
            ),
        ),
    ] = None,
    platform: Annotated[
        str | None,
        typer.Option(
            "--platform",
            help=(
                "Target platform (e.g. linux/amd64). Defaults to [msb.image] "
                "platform from the config, then the host architecture."
            ),
        ),
    ] = None,
    tag: Annotated[
        str | None,
        typer.Option(
            "--tag",
            help="Image tag. Defaults to [msb.image] tag from the config.",
        ),
    ] = None,
    context: Annotated[
        Path | None,
        typer.Option(
            "--context",
            help="Build context directory. Defaults to the workspace.",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = None,
    keep_tar: Annotated[
        Path | None,
        typer.Option(
            "--keep-tar",
            help=(
                "Write the docker-archive tar to this path and keep it "
                "after import. By default a temporary file is removed on "
                "success."
            ),
        ),
    ] = None,
) -> None:
    """Build the agent image and import it into the msb image cache.

    Runs a host-side podman/docker build of the msb-agents Dockerfile,
    saves the image as a docker-archive tar, and imports it via
    ``msb load``. Skipped entirely when the tag is already cached (unless
    ``--force``).
    """
    config, invoker = _resolve_invoker(workspace)
    image_tag = tag or config.image.tag
    build_platform = platform or config.image.platform
    dockerfile_path = Path(
        dockerfile or config.image.dockerfile or bundled_dockerfile_path()
    )
    build_context = str(context or workspace)

    if not force:
        probe = invoker.run(build_image_inspect_args(image_tag), check=False)
        if probe.returncode == 0:
            typer.echo(
                f"Image {image_tag} is already in the msb cache; "
                "skipping build (use --force to rebuild)."
            )
            return

    typer.echo(
        f"Building {image_tag} ({engine}; dockerfile: {dockerfile_path}; "
        f"context: {build_context})..."
    )
    _build_and_import_image(
        invoker,
        engine=engine,
        dockerfile_path=dockerfile_path,
        platform=build_platform,
        image_tag=image_tag,
        build_context=build_context,
        keep_tar=keep_tar,
    )


def _build_and_import_image(
    invoker: MsbInvoker,
    *,
    engine: str,
    dockerfile_path: Path,
    platform: str | None,
    image_tag: str,
    build_context: str,
    keep_tar: Path | None,
) -> None:
    """Build an image on the host and import it into the msb cache.

    :param invoker: Invoker bound to the resolved msb binary.
    :param engine: Host container engine (``podman`` or ``docker``).
    :param dockerfile_path: Dockerfile to build.
    :param platform: Optional target platform.
    :param image_tag: Tag to build and import.
    :param build_context: Build context directory.
    :param keep_tar: Optional tar output path to keep; a temporary file
        (removed on success) is used when ``None``.
    :raises MsbCircusError: If the msb import fails.
    :raises typer.Exit: If a host build/save step fails.
    """
    _run_host(
        build_podman_build_args(
            dockerfile=str(dockerfile_path),
            tag=image_tag,
            platform=platform,
            context=build_context,
            engine=engine,
        )
    )
    typer.echo("Image built.")

    if keep_tar is not None:
        tar_path = keep_tar
        tar_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        fd, tar_name = tempfile.mkstemp(prefix="msb-circus-", suffix=".tar")
        os.close(fd)
        tar_path = Path(tar_name)

    typer.echo(f"Saving image archive to {tar_path}...")
    _run_host(build_podman_save_args(image_tag, str(tar_path), engine=engine))

    typer.echo("Importing into the msb image cache (this can take a while)...")
    try:
        invoker.run(build_load_args(str(tar_path)), capture_output=False)
    except MsbCircusError as e:
        typer.echo(f"Error: {e}", err=True)
        typer.echo(f"Image archive kept at {tar_path}", err=True)
        raise typer.Exit(code=1) from e

    if keep_tar is None:
        tar_path.unlink(missing_ok=True)
        typer.echo(f"✓ Imported {image_tag} (archive removed)")
    else:
        typer.echo(f"✓ Imported {image_tag} (archive kept at {tar_path})")


@app.command(name="import")
def import_(
    tar: Annotated[
        Path,
        typer.Argument(
            help="Docker-archive tar to import (e.g. from 'podman save').",
            exists=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ],
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory path.",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = Path("."),
) -> None:
    """Import an existing docker-archive tar into the msb image cache."""
    _config, invoker = _resolve_invoker(workspace)
    typer.echo(f"Importing {tar} into the msb image cache...")
    try:
        invoker.run(build_load_args(str(tar)), capture_output=False)
    except MsbCircusError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"✓ Imported {tar}")


def _probe_dockerd(
    invoker: MsbInvoker,
    name: str,
    *,
    attempts: int | None = None,
    delay: float | None = None,
) -> tuple[bool, str]:
    """Wait until dockerd answers ``docker info`` inside the guest.

    The image ENTRYPOINT starts dockerd asynchronously; ``msb start`` and
    freshly booted sandboxes may not have it ready yet (see the spike item
    in the plan). This probe retries until the guest answers or the
    attempt budget is exhausted.

    The probe deliberately uses plain ``msb exec <name> -- docker info``
    (no ``--no-tty``): that is the invocation verified manually against
    the pinned msb release; ``--no-tty`` was observed to fail the probe
    consistently even with a healthy dockerd.

    :param invoker: Invoker bound to the resolved msb binary.
    :param name: Sandbox name.
    :param attempts: Maximum number of probes; defaults to
        :data:`DOCKERD_PROBE_ATTEMPTS`.
    :param delay: Seconds between probes; defaults to
        :data:`DOCKERD_PROBE_DELAY`.
    :returns: Tuple of ``(ready, last_error)`` where *last_error* is the
        captured output of the final failed probe (empty when ready).
    """
    attempts = DOCKERD_PROBE_ATTEMPTS if attempts is None else attempts
    delay = DOCKERD_PROBE_DELAY if delay is None else delay
    last_error = ""
    for attempt in range(attempts):
        result = invoker.run(
            build_exec_args(name, ["docker", "info"], tty=None), check=False
        )
        if result.returncode == 0:
            return True, ""
        last_error = (result.stderr or result.stdout or "").strip()
        if attempt < attempts - 1:
            time.sleep(delay)
    return False, last_error


def _dockerd_warning(name: str, last_error: str) -> str:
    """Build the dockerd-not-ready warning including probe diagnostics.

    :param name: Sandbox name (for the manual diagnostic command).
    :param last_error: Captured output of the final failed probe.
    :returns: Multi-line warning message.
    """
    lines = [
        (
            f"Warning: dockerd did not become ready in the guest; check "
            f"/tmp/dockerd.log via: msb exec {name} -- cat /tmp/dockerd.log"
        ),
        (f"Diagnose with: msb exec {name} -- docker info && msb exec {name} -- whoami"),
    ]
    if last_error:
        truncated = last_error if len(last_error) <= 500 else last_error[:500] + "…"
        lines.append(f"Last probe error: {truncated}")
    return "\n".join(lines)


@app.command()
def up(
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory path.",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = Path("."),
    build: Annotated[
        bool,
        typer.Option(
            "--build",
            "-b",
            help="Build and import the image if it is not cached yet.",
        ),
    ] = False,
    engine: Annotated[
        str,
        typer.Option(
            "--engine",
            help="Host container engine used with --build: podman or docker.",
        ),
    ] = "podman",
) -> None:
    """Ensure the workspace sandbox exists and is running (boot-only).

    Boots the sandbox via ``msb run --detach`` (so the image ENTRYPOINT
    starts dockerd), reuses an already-running sandbox, or starts a
    stopped one. Harnesses are then started manually via ``msb-circus
    exec``. Verifies dockerd readiness after booting or starting.
    """
    config, invoker = _resolve_invoker(workspace)
    _ensure_sandbox_running(
        invoker, config, workspace, allow_build=build, engine=engine
    )


def _ensure_sandbox_running(
    invoker: MsbInvoker,
    config: MsbConfig,
    workspace: Path,
    *,
    allow_build: bool = False,
    engine: str = "podman",
) -> None:
    """Ensure the workspace sandbox exists and is running.

    Status messages go to stderr so this is safe to call before an ACP
    stream that owns stdout.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param workspace: Workspace directory.
    :param allow_build: Build and import the image when it is missing
        instead of failing.
    :param engine: Host engine used when building.
    :raises typer.Exit: If the image is missing and building is not allowed.
    """
    name = sandbox_name_for_workspace(workspace)
    image_tag = config.image.tag
    try:
        effective_mounts(config, workspace)
    except ConfigurationError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    for entry in config.additional_dirs:
        if os.path.isdir(entry.path):
            continue
        if os.path.exists(entry.path):
            detail = "exists but is not a directory"
        else:
            detail = "does not exist on the host"
        typer.echo(
            f"Error: additional_dirs entry {entry.name!r}: host path "
            f"{entry.path!r} {detail}.",
            err=True,
        )
        raise typer.Exit(code=1)

    probe = invoker.run(build_image_inspect_args(image_tag), check=False)
    if probe.returncode != 0:
        if not allow_build:
            typer.echo(
                f"Error: image {image_tag} is not in the msb cache. Run "
                "'msb-circus build' first, or re-run with --build.",
                err=True,
            )
            raise typer.Exit(code=1)
        _build_and_import_image(
            invoker,
            engine=engine,
            dockerfile_path=Path(config.image.dockerfile or bundled_dockerfile_path()),
            platform=config.image.platform,
            image_tag=image_tag,
            build_context=str(workspace),
            keep_tar=None,
        )

    sandboxes = _list_sandboxes(invoker, running_only=False)
    existing = {s.name: s for s in sandboxes}.get(name)
    if existing is not None and (existing.status or "").lower() == "running":
        typer.echo(f"Sandbox {name} is already running; reusing it.", err=True)
        return

    if existing is not None:
        typer.echo(f"Starting existing sandbox {name}...", err=True)
        invoker.run(build_start_args(name))
    else:
        typer.echo(f"Booting sandbox {name} from {image_tag}...", err=True)
        invoker.run(
            build_run_args(
                name=name,
                image=image_tag,
                memory=config.sandbox.memory,
                cpus=config.sandbox.cpus,
                mounts=effective_mounts(config, workspace),
                net=config.network.mode,
                env=config.env,
                workdir=config.agent.workdir,
            )
        )

    ready, last_error = _probe_dockerd(invoker, name)
    if ready:
        typer.echo("dockerd is ready in the guest.", err=True)
    else:
        typer.echo(_dockerd_warning(name, last_error), err=True)


@app.command()
def stop(
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory path.",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = Path("."),
) -> None:
    """Stop the workspace sandbox."""
    _config, invoker = _resolve_invoker(workspace)
    name = sandbox_name_for_workspace(workspace)
    invoker.run(build_stop_args(name))
    typer.echo(f"✓ Stopped {name}")


@app.command()
def start(
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory path.",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = Path("."),
) -> None:
    """Start the workspace sandbox and verify dockerd readiness."""
    _config, invoker = _resolve_invoker(workspace)
    name = sandbox_name_for_workspace(workspace)
    invoker.run(build_start_args(name))
    ready, last_error = _probe_dockerd(invoker, name)
    if ready:
        typer.echo(f"✓ Started {name}; dockerd is ready.")
    else:
        typer.echo(_dockerd_warning(name, last_error), err=True)


@app.command()
def destroy(
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory path.",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = Path("."),
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Skip confirmation prompt.",
        ),
    ] = False,
) -> None:
    """Remove the workspace sandbox including all guest state.

    This stops the sandbox if it is running and deletes guest-only state
    (image layers, caches). Workspace files mounted from the host are not
    affected.
    """
    _config, invoker = _resolve_invoker(workspace)
    name = sandbox_name_for_workspace(workspace)
    if not force:
        typer.echo(f"This will remove sandbox {name} and all its guest state.")
        if not typer.confirm("Are you sure you want to continue?"):
            typer.echo("Aborted.")
            raise typer.Exit(code=0)
    invoker.run(build_remove_args(name, force=True))
    typer.echo(f"✓ Destroyed {name}")


@app.command(name="exec")
def exec_(
    command: Annotated[
        list[str] | None,
        typer.Argument(
            help=(
                "Command to run in the sandbox (pass after '--'). Defaults "
                "to [msb] agent.command from the config."
            ),
        ),
    ] = None,
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory path.",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = Path("."),
    acp: Annotated[
        bool,
        typer.Option(
            "--acp",
            help=(
                "Run in ACP streaming mode (--stream --quiet, piped stdin, "
                "no PTY); stdout carries only ACP messages."
            ),
        ),
    ] = False,
    env: Annotated[
        list[str] | None,
        typer.Option(
            "--env",
            "-e",
            help=(
                "Per-exec environment override KEY=VALUE (repeatable). "
                "Configured env_passthrough variables are always included."
            ),
        ),
    ] = None,
    user: Annotated[
        str | None,
        typer.Option("--user", "-u", help="Guest user to run the command as."),
    ] = None,
    workdir: Annotated[
        str | None,
        typer.Option(
            "--workdir",
            help="Guest working directory. Defaults to [msb] agent.workdir.",
        ),
    ] = None,
) -> None:
    """Run a command (or an ACP session) in the workspace sandbox.

    Boot the sandbox first with 'msb-circus up'; this command boots it
    automatically when it is not running. In ACP mode, all diagnostics go
    to stderr and stdout carries only the ACP byte stream.
    """
    config, invoker = _resolve_invoker(workspace)
    name = sandbox_name_for_workspace(workspace)
    cmd = list(command) if command else list(config.agent.command)

    try:
        env_map = {
            **resolve_passthrough(config.env_passthrough),
            **parse_env_assignments(env or []),
        }
    except ConfigurationError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    _ensure_sandbox_running(invoker, config, workspace)

    argv = [
        str(invoker.binary),
        *build_exec_args(
            name,
            cmd,
            acp=acp,
            env=env_map or None,
            workdir=workdir or config.agent.workdir,
            user=user,
        ),
    ]
    try:
        if acp:
            returncode = run_acp_stream(argv)
        else:
            returncode = subprocess.run(argv, check=False).returncode
    except FileNotFoundError as e:
        typer.echo(f"Error: msb binary not found: {e}", err=True)
        raise typer.Exit(code=1) from e
    if returncode != 0:
        raise typer.Exit(code=returncode)


def run_cli() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    run_cli()
