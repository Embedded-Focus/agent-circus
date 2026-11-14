"""Main CLI entry point for msb-circus."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import (
    ADDITIONAL_DIRS_GUEST_BASE,
    AGENT_CONFIG_FILE_COPIES,
    DEFAULT_WORKDIR,
    DOCKER_VOLUME_SUFFIX,
    DOCKERD_SHUTDOWN_TIMEOUT_ENV_VAR,
    HOOK_NAMES,
    INIT_NONE,
    SSH_AGENT_GUEST_SOCKET,
    SSH_AGENT_VSOCK_PORT,
    DaemonConfig,
    HookConfig,
    MsbConfig,
    docker_enabled,
    docker_volume_name_for_workspace,
    effective_mounts,
    load_config,
    load_hook_script,
    resolve_agent_config_mounts,
    resolve_agent_skill_mounts,
    resolve_agent_workdir,
    resolve_mounts,
    sandbox_name_for_workspace,
)
from .daemons import (
    EXIT_SVSCAN_NOT_READY,
    SVSCAN_WAIT_INTERVAL_S,
    SVSCAN_WAIT_TRIES,
    build_install_script,
    build_logs_command,
    build_port_wait_script,
    build_restart_script,
    build_start_script,
    build_status_script,
    build_stop_all_script,
    build_stop_script,
    encode_env_files,
    format_duration,
    parse_status_output,
)
from .envs import parse_env_assignments, resolve_passthrough
from .exceptions import ConfigurationError, MsbCircusError, MsbCommandError
from .exec_ import (
    S6_PID1_COMM,
    build_exec_args,
    build_host_cas_probe_args,
    build_init_shutdown_args,
    build_install_executable_args,
    build_pid1_probe_args,
    build_prepare_ssh_dir_args,
    build_remove_guest_symlink_args,
    build_repair_codex_daemon_args,
    build_ssh_agent_probe_args,
    build_write_claude_trust_args,
    build_write_gitconfig_args,
    build_write_hosts_args,
    build_write_trust_args,
    run_acp_stream,
)
from .gitconfig import (
    GIT_GUEST_HOST_CONFIG,
    GIT_GUEST_SIGNING_KEY,
    check_ssh_public_key,
    render_managed_gitconfig,
    resolve_host_gitconfig,
)
from .gitworktree import (
    GIT_WRAPPER_GUEST_PATH,
    SANDBOX_WORKTREES_DIR,
    ensure_not_linked_worktree,
    ensure_sandbox_worktrees_dir,
    find_missing_worktrees,
    git_wrapper_script,
    has_sandbox_worktrees,
    repair_guest_path_worktrees,
)
from .hooks import (
    EXIT_RUNNER_FAILED,
    EXIT_TIMEOUT,
    build_check_env,
    build_check_script,
    build_run_env,
    hook_key_hash,
    log_path,
    parse_check_output,
)
from .hooks import build_logs_command as build_hook_logs_command
from .hooks import build_run_args as build_hook_run_args
from .hooks import build_status_script as build_hook_status_script
from .hooks import parse_status_output as parse_hook_status_output
from .hostports import find_occupied_ports
from .hosts import (
    loopback_names,
    read_hosts_file,
    render_host_block,
    select_host_lines,
    validate_host_lines,
)
from .images import (
    build_image_inspect_args,
    build_load_args,
    build_podman_build_args,
    build_podman_save_args,
    bundled_dockerfile_path,
)
from .msb import MsbInvoker, check_version, probe_version, resolve_msb_binary
from .ps import format_sandbox_json, format_sandbox_table
from .sandbox import (
    SandboxInfo,
    VolumeInfo,
    build_copy_args,
    build_inspect_args,
    build_list_args,
    build_remove_args,
    build_run_args,
    build_start_args,
    build_stop_args,
    build_volume_list_args,
    build_volume_remove_args,
    configured_ports,
    parse_inspect_bind_mounts,
    parse_inspect_disk_config,
    parse_inspect_ports,
    parse_inspect_ports_ordered,
    parse_root_disk_spec,
    parse_sandbox_list_output,
    parse_size_mib,
    parse_volume_list_output,
)
from .skills import (
    CONFIG_MOUNT_SKILL_DIRS,
    PROJECT_SKILL_DIRS,
    find_broken_skill_links,
)
from .ssh_agent import (
    refresh_agent_link,
    ssh_file_copies,
    ssh_sandbox_env,
)
from .tracing import TraceSession
from .utils import setup_logging

app = typer.Typer(
    name="msb-circus",
    help="Manage AI coding-agent sandboxes with Microsandbox.",
    no_args_is_help=True,
)

daemon_app = typer.Typer(
    help="Start, stop, restart and read the logs of a configured daemon.",
    no_args_is_help=True,
)
app.add_typer(daemon_app, name="daemon")

hook_app = typer.Typer(
    help="Run a project hook or read its log.",
    no_args_is_help=True,
)
app.add_typer(hook_app, name="hook")

logger = logging.getLogger(__name__)

#: Budget for the post-boot/start dockerd readiness probe.
DOCKERD_PROBE_ATTEMPTS = 20
DOCKERD_PROBE_DELAY = 1.5

#: Retry budget for the ``ssh-add -l`` probe while the guest bridge starts.
SSH_AGENT_PROBE_ATTEMPTS = 10
SSH_AGENT_PROBE_DELAY = 0.5

#: Seconds between ``msb list`` polls while waiting for an s6 shutdown.
STOP_POLL_INTERVAL = 0.5

#: Seconds msb-circus waits for an autostarted daemon's port to open.
DAEMON_READY_TIMEOUT = 30


def _monotonic() -> float:
    """Return :func:`time.monotonic`; indirection eases test patching."""
    return time.monotonic()


#: Log file used by ``exec --acp`` when neither ``--log-file`` nor the
#: ``[logging]`` config selects one. ACP mode never writes logs to stderr
#: (the ACP backend treats any stderr output as an API error), so a file
#: is mandatory; the default keeps the logs in a predictable place.
DEFAULT_ACP_LOG_FILE = Path(tempfile.gettempdir()) / "msb-circus-acp.log"


def _configure_acp_logging(settings: object) -> None:
    """Reconfigure logging for ACP mode: log file only, never stderr.

    Must run before any config load or sandbox probe so that their
    warnings stay off stderr. Fatal errors that immediately precede a
    ``typer.Exit`` keep using ``typer.echo(..., err=True)``: the process
    is about to die anyway, and the backend should surface the reason.

    :param settings: Value of ``ctx.obj`` as set by the ``main`` callback
        (a dict with ``log_level`` and ``log_file`` keys).
    """
    options = settings if isinstance(settings, dict) else {}
    level = options.get("log_level") or "INFO"
    log_file = options.get("log_file") or DEFAULT_ACP_LOG_FILE
    setup_logging(level=str(level), log_file=Path(log_file), to_stderr=False)


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
    log_file: Annotated[
        Path | None,
        typer.Option(
            "--log-file",
            envvar="LOGFILE",
            help=(
                "Path to a log file. Logs are written to stderr and this "
                "file; in ACP mode (exec --acp) only to this file."
            ),
        ),
    ] = None,
) -> None:
    """Manage AI coding-agent sandboxes with Microsandbox."""
    # The workspace option is per-command, so fall back to the current
    # directory for the [logging] defaults. A malformed config must not
    # break unrelated commands; the invoked command surfaces the error.
    if log_level is None or log_file is None:
        config_logger = logging.getLogger("msb_circus.config")
        # Silence config warnings from this probe load; the command's own
        # config load re-emits them to the fully configured handlers
        # (otherwise they would run through the last-resort handler to
        # stderr before setup_logging has been called).
        config_logger.disabled = True
        try:
            config = load_config(Path("."))
        except MsbCircusError:
            config = None
        finally:
            config_logger.disabled = False
        if config is not None:
            log_level = log_level or config.logging_level
            log_file = log_file or (
                Path(config.logging_file) if config.logging_file else None
            )
    setup_logging(level=log_level or "INFO", log_file=log_file)
    # Resolved settings for commands that must reconfigure logging
    # (exec --acp switches to file-only); obj propagates to subcommands.
    ctx.obj = {"log_level": log_level or "INFO", "log_file": log_file}


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
    format_: Annotated[
        str,
        typer.Option(
            "--format",
            help="Output format: table (docker compose ps style) or json.",
        ),
    ] = "table",
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            "-q",
            help="Only print sandbox names, one per line.",
        ),
    ] = False,
) -> None:
    """Show sandbox status for this workspace (or all sandboxes).

    By default only this workspace's sandbox (``agents-<project>``) is
    shown, in any state. ``--all`` lists every sandbox msb knows about.

    The table output mirrors ``docker compose ps`` (and therefore
    ``agent-circus ps``). For scripts use ``--format json`` or
    ``--quiet``; both stay silent when no sandbox matches.

    Examples:
        msb-circus ps                       # Table for this workspace
        msb-circus ps --all                 # Every sandbox msb knows
        msb-circus ps --running             # Only running sandboxes
        msb-circus ps --format json         # Machine-readable output
        msb-circus ps --quiet | xargs msb-circus rm -f
    """
    if format_ not in ("table", "json"):
        print(f"Error: unsupported --format {format_!r} (expected table or json)")
        raise typer.Exit(code=1)

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

    # Machine-readable formats stay parseable: no prose on stdout.
    if quiet:
        for sandbox in selected:
            print(sandbox.name)
        return

    # Both readable formats carry the ports (table column / JSON key).
    # One msb inspect per sandbox; failures degrade to no ports.
    selected = [
        replace(sandbox, ports=_fetch_sandbox_ports(invoker, sandbox.name))
        for sandbox in selected
    ]

    if format_ == "json":
        print(format_sandbox_json(selected))
        return

    # Mirror docker compose ps: an empty selection still prints the
    # header row.
    print(format_sandbox_table(selected))


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


def _docker_volumes(invoker: MsbInvoker) -> dict[str, VolumeInfo] | None:
    """Return existing msb volumes by name.

    :param invoker: Invoker bound to the resolved msb binary.
    :returns: Volumes keyed by name, or ``None`` when the list could not
        be read (the failure is logged at debug level).
    """
    try:
        result = invoker.run(build_volume_list_args())
        volumes = parse_volume_list_output(result.stdout)
    except (MsbCommandError, ConfigurationError) as e:
        logger.debug("Could not list msb volumes: %s", e)
        return None
    return {volume.name: volume for volume in volumes}


def _finish_docker_volume(
    invoker: MsbInvoker,
    volumes: dict[str, VolumeInfo] | None,
    volume: str,
    *,
    delete: bool,
) -> None:
    """Delete or report the persistent docker volume of a removed sandbox.

    :param invoker: Invoker bound to the resolved msb binary.
    :param volumes: Result of :func:`_docker_volumes` taken before removal.
    :param volume: Docker volume name of the removed sandbox.
    :param delete: Delete the volume instead of keeping it.
    :raises typer.Exit: If deleting the volume fails.
    """
    if volumes is None or volume not in volumes:
        return
    if delete:
        try:
            invoker.run(build_volume_remove_args(volume))
        except MsbCircusError as e:
            typer.echo(f"Error: could not delete docker volume {volume}: {e}", err=True)
            raise typer.Exit(code=1) from e
        typer.echo(f"✓ Deleted docker volume {volume}")
        return
    info = volumes[volume]
    if info.capacity_bytes is None:
        size = "unknown size"
    else:
        size = _format_capacity(info.capacity_bytes)
    typer.echo(
        f"Kept docker volume {volume} ({size}) with its docker images. "
        f"Delete it with: msb volume rm {volume}"
    )


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
    volumes: Annotated[
        bool,
        typer.Option(
            "--volumes",
            help="Also delete the persistent docker volume (all docker images and data in it).",
        ),
    ] = False,
) -> None:
    """Remove agent sandboxes and their guest state.

    Host-mounted files (e.g. the workspace bind mount) are not affected.
    Without names, this workspace's sandbox is removed. Without --force,
    a confirmation prompt is shown and a running sandbox is not stopped.
    A persistent docker volume is kept unless --volumes is given.
    """
    _config, invoker = _resolve_invoker(workspace)
    targets = names or [sandbox_name_for_workspace(workspace)]
    existing = _docker_volumes(invoker)

    if not force:
        if volumes:
            typer.echo(
                f"This will remove sandbox(es): {', '.join(targets)}, "
                "and delete their persistent docker volumes."
            )
        else:
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
        _finish_docker_volume(
            invoker, existing, f"{target}{DOCKER_VOLUME_SUFFIX}", delete=volumes
        )


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

    The image's s6 services start dockerd asynchronously; ``msb start``
    and freshly booted sandboxes may not have it ready yet (see the spike
    item in the plan). This probe retries until the guest answers or the
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


def _check_dockerd(invoker: MsbInvoker, config: MsbConfig, name: str) -> None:
    """Probe dockerd readiness or explain why docker is unavailable.

    When the sandbox has no usable docker storage backend (no docker disk
    and no flat root), dockerd is never started by the image's s6 services
    and ``docker`` commands in the guest fail with a socket error. That
    state
    is reported at debug level — including for already-running
    sandboxes, which skip the boot/start paths — together with the fix.
    Otherwise dockerd readiness is probed and a failure is logged at
    debug level with diagnostics.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    """
    if not docker_enabled(config):
        logger.debug(
            "docker is not enabled for sandbox %s: the guest has no usable "
            "docker storage backend, so docker commands will fail. Enable "
            'it with docker_disk = "10G" (or root_disk = "flat:32G") under '
            "[msb.sandbox] in .agent-circus/config.toml, then recreate the "
            "sandbox: msb-circus rm -f && msb-circus up.",
            name,
        )
        return
    ready, last_error = _probe_dockerd(invoker, name)
    if ready:
        logger.debug("dockerd is ready in the guest.")
    else:
        logger.debug("%s", _dockerd_warning(name, last_error))


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

    Boots the sandbox via ``msb run --detach --init ...`` (so s6-overlay
    becomes PID 1 and starts the services), reuses an already-running
    sandbox, or starts a stopped one. Harnesses are then started manually
    via ``msb-circus exec``. Verifies dockerd readiness after booting or
    starting.
    """
    config, invoker = _resolve_invoker(workspace)
    _ensure_sandbox_running(
        invoker,
        config,
        workspace,
        allow_build=build,
        engine=engine,
        sync_daemons=True,
    )


def _warn_on_port_drift(invoker: MsbInvoker, config: MsbConfig, name: str) -> None:
    """Warn when configured port forwards differ from the sandbox's ports.

    msb fixes port mappings at sandbox creation and ``msb modify`` cannot
    change them, so a config change only applies to a newly created
    sandbox. Failures here are non-fatal (probes, never raises).

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name to inspect.
    """
    configured = configured_ports(config.port_forwards)
    if not configured:
        return
    try:
        result = invoker.run(build_inspect_args(name))
        published = parse_inspect_ports(result.stdout)
    except (MsbCommandError, ConfigurationError) as e:
        logger.debug("Port drift check for %s failed: %s", name, e)
        return
    if published == configured:
        return
    logger.warning(
        "Port forwards for %s differ from the configuration. "
        "msb fixes ports at sandbox creation, so the change is not applied "
        "to this sandbox. Remove and re-create it to apply: "
        "msb-circus rm -f && msb-circus up (ports: %s).",
        name,
        sorted(published),
    )


def _fetch_sandbox_ports(
    invoker: MsbInvoker, name: str
) -> tuple[tuple[str, int, int, str], ...]:
    """Fetch a sandbox's published ports via ``msb inspect``.

    Failures are non-fatal: an unreadable inspect output is logged at
    debug level and reported as no ports.

    :param invoker: Invoker bound to the resolved msb binary.
    :param name: Sandbox name to inspect.
    :returns: ``(host, host_port, guest_port, protocol)`` tuples in
        msb's order; empty when the sandbox publishes nothing or the
        inspect output could not be read.
    """
    try:
        result = invoker.run(build_inspect_args(name))
        return tuple(parse_inspect_ports_ordered(result.stdout))
    except (MsbCommandError, ConfigurationError) as e:
        logger.debug("Port lookup for %s failed: %s", name, e)
        return ()


def _warn_on_disk_drift(invoker: MsbInvoker, config: MsbConfig, name: str) -> None:
    """Warn when configured disk settings differ from the sandbox's disks.

    msb fixes disks at sandbox creation and ``msb modify`` cannot add
    mounts, so a config change only applies to a newly created sandbox.
    Failures here are non-fatal: an unreadable or unrecognized inspect
    output skips the check instead of reporting a false mismatch.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name to inspect.
    """
    try:
        result = invoker.run(build_inspect_args(name))
        guest = parse_inspect_disk_config(result.stdout)
    except (MsbCommandError, ConfigurationError) as e:
        logger.debug("Disk drift check for %s failed: %s", name, e)
        return

    problems: list[str] = []

    # Docker data disk: compare presence and size. A smaller persisted
    # disk is a drift; a larger one is fine (the user raised it via
    # `msb modify`-style growth or manually). Named volume sizes are not
    # in the inspect output; they are checked at creation instead.
    requested_docker_disk = config.sandbox.docker_disk
    expected_volume = (
        f"{name}{DOCKER_VOLUME_SUFFIX}" if config.sandbox.docker_disk_persist else None
    )
    if expected_volume is not None:
        if guest.docker_volume_name == expected_volume:
            logger.debug(
                "Persistent docker volume %s is mounted in sandbox %s.",
                expected_volume,
                name,
            )
        elif guest.docker_volume_name is not None:
            problems.append(
                f"sandbox uses docker volume {guest.docker_volume_name}, but the "
                f"config expects {expected_volume}"
            )
        elif guest.docker_disk_size_mib is not None:
            problems.append(
                "docker_disk_persist is set, but the sandbox keeps docker data "
                "on a private disk that is deleted with the sandbox"
            )
        elif guest.mounts_seen:
            problems.append(
                f"persistent docker volume {expected_volume} is not mounted "
                "in the sandbox"
            )
    elif guest.docker_volume_name is not None:
        problems.append(
            f"sandbox uses persistent docker volume {guest.docker_volume_name}, "
            "but msb.sandbox.docker_disk_persist is not set"
        )
    elif requested_docker_disk is not None:
        if guest.docker_disk_size_mib is None:
            if guest.mounts_seen:
                problems.append(
                    f"docker_disk={requested_docker_disk} is not present in the sandbox"
                )
        else:
            wanted = parse_size_mib(requested_docker_disk)
            if wanted is not None and guest.docker_disk_size_mib < wanted:
                problems.append(
                    f"docker disk is {guest.docker_disk_size_mib} MiB, smaller than "
                    f"the configured {requested_docker_disk}"
                )
    elif guest.docker_disk_size_mib is not None:
        problems.append(
            "sandbox has a docker data disk but the config does not set "
            "msb.sandbox.docker_disk"
        )

    # Root disk: compare kind always, size only when both are known. With
    # no root_disk configured, the expected kind is msb's managed default.
    requested_kind, requested_size_mib = (
        parse_root_disk_spec(config.sandbox.root_disk)
        if config.sandbox.root_disk is not None
        else ("managed", None)
    )
    if (
        requested_kind is not None
        and guest.root_disk_kind is not None
        and guest.root_disk_kind != requested_kind
    ):
        problems.append(
            f"root disk kind is {guest.root_disk_kind}, but the config asks "
            f"for {requested_kind}"
        )
    elif (
        guest.root_disk_kind == requested_kind
        and requested_size_mib is not None
        and guest.root_disk_size_mib is not None
        and guest.root_disk_size_mib < requested_size_mib
    ):
        problems.append(
            f"root disk is {guest.root_disk_size_mib} MiB, smaller than the "
            f"configured {requested_size_mib} MiB"
        )

    if not problems:
        return
    logger.warning(
        "Disk settings of sandbox %s differ from the configuration. Disks are "
        "fixed at sandbox creation, so the change is not applied to this "
        "sandbox. Remove and re-create it to apply (this deletes a private "
        "docker data disk; a persistent docker volume is kept): "
        "msb-circus rm -f && msb-circus up (%s).",
        name,
        "; ".join(problems),
    )


def _warn_on_mount_drift(
    invoker: MsbInvoker, config: MsbConfig, name: str, workspace: Path
) -> None:
    """Warn when the git worktree mirror is configured but the sandbox lacks it.

    msb fixes mounts at sandbox creation, so enabling
    ``[git] worktree_mirror`` only applies to a newly created sandbox.
    Failures here are non-fatal: an unreadable or unrecognized inspect
    output skips the check instead of reporting a false mismatch.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name to inspect.
    :param workspace: Workspace directory whose host path should be mirrored.
    """
    if not config.git_worktree_mirror:
        return
    try:
        result = invoker.run(build_inspect_args(name))
        binds = parse_inspect_bind_mounts(result.stdout)
    except (MsbCommandError, ConfigurationError) as e:
        logger.debug("Mount drift check for %s failed: %s", name, e)
        return
    host_path = str(workspace)
    if (host_path, host_path) in binds:
        return
    if config.git_worktree_mirror_explicit:
        logger.warning(
            "git.worktree_mirror is enabled in the configuration, but sandbox %s "
            "was created without the workspace mirror mount. Mounts are fixed at "
            "sandbox creation, so the change is not applied to this sandbox. "
            "Remove and re-create it to apply: msb-circus rm -f && msb-circus up.",
            name,
        )
    else:
        logger.warning(
            "Sandbox %s was created without the git worktree mirror mount, which "
            "msb-circus now adds by default ([git] worktree_mirror defaults to true). "
            "Mounts are fixed at sandbox creation. Re-create the sandbox to apply "
            "it (msb-circus rm -f && msb-circus up), or set [git] worktree_mirror = "
            "false in .agent-circus/config.toml to keep the old behavior.",
            name,
        )


def _warn_on_trust_host_cas_drift(
    invoker: MsbInvoker, config: MsbConfig, name: str
) -> None:
    """Warn when ``trust_host_cas`` differs from the sandbox's setting.

    msb applies ``--trust-host-cas`` at sandbox creation only, and
    ``msb inspect`` does not report it. The guest bundle
    ``/.msb/tls/host-cas.pem`` exists exactly when the flag was set.
    Failures here are non-fatal (probes, never raises).

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name to probe.
    """
    try:
        result = invoker.run(build_host_cas_probe_args(name), check=False)
    except MsbCommandError as e:
        logger.debug("Host CA drift check for %s failed: %s", name, e)
        return
    answer = (result.stdout or "").strip()
    if answer not in ("present", "absent"):
        logger.debug(
            "Host CA drift check for %s gave no answer (rc=%s)",
            name,
            result.returncode,
        )
        return
    if (answer == "present") == config.network.trust_host_cas:
        return
    state = "enabled" if config.network.trust_host_cas else "disabled"
    logger.warning(
        "msb.network.trust_host_cas is %s in the configuration, but sandbox "
        "%s was created with the opposite setting. msb applies it at "
        "sandbox creation only. Remove and re-create the sandbox to apply: "
        "msb-circus rm -f && msb-circus up.",
        state,
        name,
    )


def _fail_on_occupied_ports(config: MsbConfig) -> None:
    """Hard-fail when a configured host port is already bound on the host.

    msb does not reliably fail at boot when the host port is held by
    another process (e.g. a second VM); the forward then only answers
    with empty replies. The probe is best-effort: a port grabbed between
    probe and boot is still reported by msb itself.

    :param config: Resolved msb-circus configuration.
    :raises typer.Exit: If any configured host port is occupied.
    """
    occupied = find_occupied_ports(config.port_forwards)
    if not occupied:
        return
    detail = ", ".join(
        f"{f.host}:{f.host_port} -> {f.guest_port}/{f.protocol}" for f in occupied
    )
    typer.echo(
        f"Error: host port(s) already bound by another process: {detail}. "
        "Stop the conflicting process (another VM or sandbox?) or change "
        "host_port in the [[port_forwards]] entry.",
        err=True,
    )
    raise typer.Exit(code=1)


def _workspace_guest_path(config: MsbConfig, workspace: Path) -> str:
    """Return the guest path the workspace itself is mounted at.

    ACP clients open sessions with the workspace's guest path as the
    ``session/new`` cwd, independent of the agent workdir. The path is
    taken from the first effective mount whose host side is the
    workspace (the ``.`` mount, ``/workspace`` by default); the
    worktree mirror mount, which has the same host side, comes later
    and is never matched. Without a workspace mount the documented
    default is assumed.

    :param config: Resolved msb-circus configuration.
    :param workspace: Workspace directory (absolute host path).
    :returns: Guest directory the workspace is mounted at.
    """
    for host_path, guest in effective_mounts(config, workspace):
        if host_path == str(workspace):
            return guest.split(":", 1)[0]
    return DEFAULT_WORKDIR


def _trusted_guest_dirs(config: MsbConfig, workspace: Path) -> list[str]:
    """Return the guest directories to pre-trust for the agent harnesses.

    The guest workspace path, the exec workdir, the worktree mirror
    path (when enabled), and every ``additional_dirs`` guest target, in
    first-occurrence order and without duplicates. The workspace path
    is always included because ACP clients pass it as the session cwd
    even when the agent workdir differs.

    :param config: Resolved msb-circus configuration.
    :param workspace: Workspace directory (absolute host path).
    :returns: Guest directory paths.
    """
    dirs = [
        _workspace_guest_path(config, workspace),
        resolve_agent_workdir(config, workspace),
    ]
    if config.git_worktree_mirror:
        dirs.append(str(workspace))
    dirs += [
        f"{ADDITIONAL_DIRS_GUEST_BASE}/{entry.name}" for entry in config.additional_dirs
    ]
    return list(dict.fromkeys(dirs))


def _ensure_pi_trust(
    invoker: MsbInvoker, config: MsbConfig, name: str, workspace: Path
) -> None:
    """Pre-trust the mounted work directories for pi and pi-acp.

    Writes ``$HOME/.pi/agent/trust.json`` in the guest so the first harness
    start in a fresh sandbox does not prompt for directory trust. The write
    is idempotent and overwrites an existing trust file. Failures are
    non-fatal: pi then simply asks on first start, as before.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    :param workspace: Workspace directory (absolute host path).
    """
    try:
        result = invoker.run(
            build_write_trust_args(name, _trusted_guest_dirs(config, workspace)),
            check=False,
        )
        if result.returncode == 0:
            return
        raise MsbCommandError(
            (result.stderr or result.stdout or "").strip(),
            returncode=result.returncode,
            stderr=result.stderr,
        )
    except MsbCommandError as e:
        logger.warning(
            "Could not pre-trust the work directories for pi in %s; pi may ask "
            "to trust the directory on first start (%s)",
            name,
            e,
        )


def _ensure_claude_trust(
    invoker: MsbInvoker, config: MsbConfig, name: str, workspace: Path
) -> None:
    """Pre-trust the mounted work directories for Claude Code.

    Merges ``projects[<dir>].hasTrustDialogAccepted`` into the guest's
    ``~/.claude.json`` so Claude Code skips its folder-trust dialog. Runs
    after :func:`_ensure_agent_config_copies` because the host copy
    overwrites the guest file; merging in the guest also covers hosts
    without a ``~/.claude.json``. Failures are non-fatal: Claude Code then
    asks to trust the folder, as before.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    :param workspace: Workspace directory (absolute host path).
    """
    try:
        result = invoker.run(
            build_write_claude_trust_args(name, _trusted_guest_dirs(config, workspace)),
            check=False,
        )
        if result.returncode == 0:
            return
        raise MsbCommandError(
            (result.stderr or result.stdout or "").strip(),
            returncode=result.returncode,
            stderr=result.stderr,
        )
    except MsbCommandError as e:
        logger.warning(
            "Could not pre-trust the work directories for Claude Code in %s; "
            "Claude Code may ask to trust the folder (%s)",
            name,
            e,
        )


def _repair_codex_daemon(invoker: MsbInvoker, name: str) -> None:
    """Repair the codex app-server daemon install inside the guest.

    Codex records its daemon with an absolute ``current`` symlink, which
    dangles when the host ``~/.codex`` is bind-mounted at ``/root/.codex``
    (host and guest disagree on the absolute path). A stale control-socket
    symlink from another machine aborts daemon startup the same way. The
    repair repoints dangling symlinks so codex starts; it is idempotent and
    best-effort: failures warn and never block the boot.

    :param invoker: Invoker bound to the resolved msb binary.
    :param name: Sandbox name.
    """
    try:
        result = invoker.run(build_repair_codex_daemon_args(name), check=False)
        if result.returncode == 0:
            return
        raise MsbCommandError(
            (result.stderr or result.stdout or "").strip(),
            returncode=result.returncode,
            stderr=result.stderr,
        )
    except MsbCommandError as e:
        logger.warning(
            "Could not repair the codex app-server daemon install in %s; codex "
            "may fail with 'daemon executable not found' (%s)",
            name,
            e,
        )


def _copy_into_guest(
    invoker: MsbInvoker, name: str, host_path: str | Path, guest_path: str
) -> None:
    """Copy one host file into the guest, following host symlinks.

    ``msb copy`` copies a symlink as a symlink, which leaves a dangling
    link in the guest (e.g. home-manager links into ``/nix/store``) and
    makes later copies fail with ``File exists``. The host path is
    therefore resolved first. When the copy fails, a symlink left at the
    guest destination by an earlier copy is removed and the copy is
    retried once; regular guest files are never removed. Shortcomings:
    the guest gets a snapshot of the link target (the intended one-way
    copy semantics anyway), and the repair runs on any copy failure, not
    only ``File exists``.

    :param invoker: Invoker bound to the resolved msb binary.
    :param name: Sandbox name.
    :param host_path: Host file to copy (may be a symlink).
    :param guest_path: Absolute guest destination path.
    :raises MsbCommandError: If the host path cannot be resolved or the
        copy still fails after the repair.
    """
    try:
        source = str(Path(host_path).resolve(strict=True))
    except OSError as e:
        raise MsbCommandError(f"cannot resolve {host_path}: {e}", returncode=1) from e
    try:
        invoker.run(build_copy_args(name, source, guest_path))
        return
    except MsbCommandError as first_error:
        logger.debug(
            "Copy of %s to %s failed (%s); removing a stale guest symlink and retrying",
            source,
            guest_path,
            first_error,
        )
    try:
        invoker.run(build_remove_guest_symlink_args(name, guest_path), check=False)
    except MsbCommandError as e:
        logger.debug("Guest symlink cleanup for %s failed: %s", guest_path, e)
    invoker.run(build_copy_args(name, source, guest_path))


def _ensure_agent_config_copies(invoker: MsbInvoker, name: str) -> None:
    """Copy host agent harness config files into the guest (one-way).

    Refreshes every entry of :data:`AGENT_CONFIG_FILE_COPIES` after boot or
    start so the guest sees the host's current settings. Host files that do
    not exist are skipped silently (the harness starts without them, as
    before); a failed ``msb copy`` warns and leaves the previous guest copy
    in place. The copies are best-effort and never block the boot.

    :param invoker: Invoker bound to the resolved msb binary.
    :param name: Sandbox name.
    """
    home = Path.home()
    for harness, below_home, guest in AGENT_CONFIG_FILE_COPIES:
        host_file = home / below_home
        if not host_file.is_file():
            logger.debug(
                "Skipping %s config copy: %s does not exist on the host",
                harness,
                host_file,
            )
            continue
        try:
            _copy_into_guest(invoker, name, str(host_file), guest)
        except MsbCommandError as e:
            logger.warning(
                "Could not copy the %s config file %s into sandbox %s; the "
                "harness keeps its previous guest copy (%s)",
                harness,
                host_file,
                name,
                e,
            )


def _warn_on_agent_config_mount_drift(invoker: MsbInvoker, name: str) -> None:
    """Warn when an expected agent config-dir mount is missing.

    msb fixes mounts at sandbox creation, so adding a config directory
    mount (or creating ``~/.claude`` / ``~/.codex`` after the sandbox was
    created) only applies to a newly created sandbox. Failures here are
    non-fatal: an unreadable inspect output skips the check.

    :param invoker: Invoker bound to the resolved msb binary.
    :param name: Sandbox name to inspect.
    """
    expected = resolve_agent_config_mounts()
    if not expected:
        return
    try:
        result = invoker.run(build_inspect_args(name))
        binds = parse_inspect_bind_mounts(result.stdout)
    except (MsbCommandError, ConfigurationError) as e:
        logger.debug("Agent config mount drift check for %s failed: %s", name, e)
        return
    missing = [guest for host, guest in expected if (host, guest) not in binds]
    if not missing:
        return
    logger.warning(
        "Agent config directory mounts (%s) are missing from sandbox %s. "
        "Mounts are fixed at sandbox creation, so the harnesses start "
        "without host config and credentials in this sandbox. Remove and "
        "re-create it to apply: msb-circus rm -f && msb-circus up.",
        ", ".join(missing),
        name,
    )


def _warn_on_agent_skill_mount_drift(invoker: MsbInvoker, name: str) -> None:
    """Warn when an existing host user skill directory is not mounted.

    msb fixes mounts at sandbox creation, so a skill directory created on
    the host later (e.g. by the first ``npx skills add -g``) stays
    invisible in the sandbox until it is re-created. The ``:ro`` option is
    stripped before comparing, because ``msb inspect`` may report the
    read-only flag separately from the guest path. Failures here are
    non-fatal: an unreadable inspect output skips the check.

    :param invoker: Invoker bound to the resolved msb binary.
    :param name: Sandbox name to inspect.
    """
    expected = resolve_agent_skill_mounts()
    if not expected:
        return
    try:
        result = invoker.run(build_inspect_args(name))
        binds = parse_inspect_bind_mounts(result.stdout)
    except (MsbCommandError, ConfigurationError) as e:
        logger.debug("Agent skill mount drift check for %s failed: %s", name, e)
        return
    mounted = {(host, guest.split(":", 1)[0]) for host, guest in binds}
    wanted = [(host, guest.split(":", 1)[0]) for host, guest in expected]
    missing = [guest for host, guest in wanted if (host, guest) not in mounted]
    if not missing:
        return
    logger.warning(
        "User skill directory mounts (%s) are missing from sandbox %s, so "
        "agents in it cannot see your user skills. Mounts are fixed at "
        "sandbox creation. Remove and re-create it to apply: "
        "msb-circus rm -f && msb-circus up.",
        ", ".join(missing),
        name,
    )


def _ensure_host_entries(invoker: MsbInvoker, config: MsbConfig, name: str) -> None:
    """Write the selected host entries into the guest ``/etc/hosts``.

    Re-reads the host hosts file on every call so changes apply without
    re-creating the sandbox. Best-effort: failures warn and never block
    the boot.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    """
    if config.hosts is None:
        return
    lines = validate_host_lines(
        select_host_lines(read_hosts_file(config.hosts.file), config.hosts.patterns)
    )
    if config.hosts.patterns and not lines:
        logger.info(
            "No host entries in %s match the [hosts] patterns", config.hosts.file
        )
    loop = loopback_names(lines)
    if loop:
        logger.warning(
            "Host entries resolve to loopback inside the sandbox, not to the "
            "host: %s. Use host.microsandbox.internal (with the 'host' network "
            "profile) to reach host services.",
            ", ".join(loop),
        )
    try:
        result = invoker.run(
            build_write_hosts_args(name, render_host_block(lines)), check=False
        )
        if result.returncode != 0:
            raise MsbCommandError(
                (result.stderr or result.stdout or "").strip(),
                returncode=result.returncode,
                stderr=result.stderr,
            )
    except MsbCommandError as e:
        logger.warning(
            "Could not update /etc/hosts in %s; forwarded host entries may "
            "be missing or stale (%s)",
            name,
            e,
        )
        return
    logger.debug("Wrote %d host entries into %s", len(lines), name)


def _prepare_ssh_agent(config: MsbConfig) -> Path | None:
    """Refresh the host agent link when SSH forwarding is configured.

    :param config: Resolved msb-circus configuration.
    :returns: The link path, or ``None`` when ``[ssh]`` is not configured.
    :raises typer.Exit: If the host agent is unusable (fatal, printed to
        stderr like other configuration errors).
    """
    if config.ssh is None:
        return None
    try:
        return refresh_agent_link()
    except ConfigurationError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _ensure_ssh_files(invoker: MsbInvoker, config: MsbConfig, name: str) -> None:
    """Copy the configured SSH config/known_hosts into the guest (one-way).

    Like :data:`AGENT_CONFIG_FILE_COPIES`: host edits are picked up after
    every boot/start/reuse, guest-side edits are lost on the next refresh.
    Best-effort: missing host files warn and are skipped; failed copies
    warn and keep the previous guest copy. Never blocks the boot.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    """
    if config.ssh is None:
        return
    copies = ssh_file_copies(config.ssh)
    if not copies:
        return
    try:
        result = invoker.run(build_prepare_ssh_dir_args(name), check=False)
        if result.returncode != 0:
            raise MsbCommandError(
                (result.stderr or result.stdout or "").strip(),
                returncode=result.returncode,
                stderr=result.stderr,
            )
    except MsbCommandError as e:
        logger.warning(
            "Could not prepare the SSH directory in sandbox %s; SSH config "
            "files are not copied (%s)",
            name,
            e,
        )
        return
    for host, guest in copies:
        if not Path(host).is_file():
            logger.warning(
                "SSH file %s not found on the host; skipping its sandbox copy",
                host,
            )
            continue
        try:
            _copy_into_guest(invoker, name, host, guest)
            invoker.run(build_exec_args(name, ["chmod", "600", guest], tty=None))
        except MsbCommandError as e:
            logger.warning(
                "Could not copy the SSH file %s into sandbox %s; the guest "
                "keeps its previous copy (%s)",
                host,
                name,
                e,
            )


def _ensure_git_config(invoker: MsbInvoker, config: MsbConfig, name: str) -> None:
    """Forward the host git config and signing key into the guest.

    Writes a managed gitconfig under ``/root/.config/msb-circus/git``
    (include of the host gitconfig copy plus ``user.signingkey``) and
    inserts one managed include block at the top of the guest's
    ``/root/.gitconfig``; all other content there is kept, and agents'
    own ``git config --global`` settings win over host settings. The
    refresh runs on every boot, start, and reuse; guest edits of the
    managed file are overwritten.

    Best-effort and never raises: missing or invalid host files produce
    log messages and are skipped; failed copies keep the guest's
    previous copies.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    """
    if config.git is None:
        return
    host_config, level, message = resolve_host_gitconfig(config.git)
    if message is not None and level is not None:
        logger.log(level, message)
    reason = None
    if config.git.signing_key_path is not None:
        key_path = Path(config.git.signing_key_path)
        reason = check_ssh_public_key(key_path)
        if reason is not None:
            logger.warning(
                "git.signing_key_path %s: %s; the sandbox gets no signing key",
                config.git.signing_key_path,
                reason,
            )
        if config.ssh is None:
            logger.warning(
                "git.signing_key_path is set but [ssh] is not configured; "
                "commit signing in the sandbox needs the forwarded SSH agent"
            )
    if host_config is None and config.git.signing_key_path is None:
        return
    include_signing_key = reason is None and config.git.signing_key_path is not None
    if host_config is None and not include_signing_key:
        return
    content = render_managed_gitconfig(
        include_host_config=host_config is not None,
        include_signing_key=include_signing_key,
    )
    try:
        result = invoker.run(build_write_gitconfig_args(name, content), check=False)
        if result.returncode != 0:
            raise MsbCommandError(
                (result.stderr or result.stdout or "").strip(),
                returncode=result.returncode,
                stderr=result.stderr,
            )
    except MsbCommandError as e:
        logger.warning(
            "Could not write the git config in sandbox %s; host git config "
            "is not forwarded (%s)",
            name,
            e,
        )
        return
    if host_config is not None:
        try:
            _copy_into_guest(invoker, name, str(host_config), GIT_GUEST_HOST_CONFIG)
        except MsbCommandError as e:
            logger.warning(
                "Could not copy %s into sandbox %s; the guest keeps its "
                "previous copy (%s)",
                host_config,
                name,
                e,
            )
    if reason is None and config.git.signing_key_path is not None:
        try:
            _copy_into_guest(
                invoker, name, config.git.signing_key_path, GIT_GUEST_SIGNING_KEY
            )
        except MsbCommandError as e:
            logger.warning(
                "Could not copy %s into sandbox %s; the guest keeps its "
                "previous copy (%s)",
                config.git.signing_key_path,
                name,
                e,
            )


def _check_ssh_agent(invoker: MsbInvoker, config: MsbConfig, name: str) -> None:
    """Probe the forwarded SSH agent inside the guest; warn on problems.

    The probe is ``ssh-add -l``: exit code 0 lists keys, 1 means the agent
    has no identities, and 2 means it cannot connect (missing route or
    bridge). Exit code 2 is retried briefly, because the image's s6
    services start the guest bridge asynchronously after boot. Never raises.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    """
    if config.ssh is None:
        return
    for attempt in range(SSH_AGENT_PROBE_ATTEMPTS):
        try:
            result = invoker.run(build_ssh_agent_probe_args(name), check=False)
        except MsbCommandError as e:
            logger.debug("SSH agent probe in %s failed: %s", name, e)
            return
        if result.returncode != 2 or attempt == SSH_AGENT_PROBE_ATTEMPTS - 1:
            break
        time.sleep(SSH_AGENT_PROBE_DELAY)
    if result.returncode == 0:
        logger.debug("SSH agent reachable in %s", name)
    elif result.returncode == 1:
        logger.debug(
            "The forwarded SSH agent in %s has no keys; add one on the host "
            "with: ssh-add",
            name,
        )
    elif result.returncode == 2:
        logger.warning(
            "Cannot reach the forwarded SSH agent in sandbox %s. If [ssh] "
            "was added after the sandbox was created, re-create it to "
            "apply: msb-circus rm -f && msb-circus up. Otherwise check the "
            "host agent (ssh-add -l) and the guest bridge log "
            "/tmp/ssh-agent-bridge.log.",
            name,
        )
    else:
        logger.debug(
            "ssh-add probe in %s exited with %s: %s (if the image predates "
            "openssh-client, rebuild the image with msb-circus build)",
            name,
            result.returncode,
            (result.stderr or result.stdout or "").strip(),
        )


def _fail_on_linked_worktree(workspace: Path) -> None:
    """Exit with an error when the workspace is in a linked Git worktree.

    :param workspace: Workspace directory.
    :raises typer.Exit: If the workspace is in a linked Git worktree.
    """
    try:
        ensure_not_linked_worktree(workspace)
    except ConfigurationError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e


def _repair_guest_path_worktrees(config: MsbConfig, workspace: Path) -> None:
    """Repair worktrees that Git recorded through the guest mount.

    Only with ``[git] worktree_mirror``: without the mirror, guest paths
    are the only valid paths inside the sandbox. Never raises.

    :param config: Resolved msb-circus configuration.
    :param workspace: Workspace directory (absolute host path).
    """
    if not config.git_worktree_mirror:
        return
    try:
        guest_root = _workspace_guest_path(config, workspace)
    except ConfigurationError as e:
        logger.debug("Worktree repair skipped: %s", e)
        return
    try:
        repaired = repair_guest_path_worktrees(workspace, guest_root)
    except subprocess.CalledProcessError as e:
        logger.warning(
            "Could not repair Git worktrees that record the sandbox path %s: %s",
            guest_root,
            (e.stderr or "").strip() or e,
        )
        return
    except OSError as e:
        logger.warning(
            "Could not repair Git worktrees that record the sandbox path %s: %s",
            guest_root,
            e,
        )
        return
    for path in repaired:
        logger.info(
            "Repaired Git worktree %s: Git had recorded its sandbox path below "
            "%s; it now records the host path.",
            path,
            guest_root,
        )


def _warn_on_missing_worktrees(workspace: Path) -> None:
    """Warn about registered Git worktrees that are missing on the host.

    Non-fatal; reads only host files.

    :param workspace: Workspace directory.
    """
    missing = find_missing_worktrees(workspace)
    if not missing:
        return
    logger.warning(
        "These Git worktrees of %s do not exist on the host: %s. A worktree "
        "created in the sandbox outside the workspace exists only in the "
        "sandbox, and its uncommitted changes are lost when the sandbox is "
        "removed. Create worktrees below .agent-circus/worktrees/ instead. "
        "Run 'git worktree prune' on the host to drop entries whose "
        "worktree is gone.",
        workspace,
        ", ".join(str(p) for p in missing),
    )


def _warn_on_broken_skill_links(config: MsbConfig, workspace: Path) -> None:
    """Warn about project and user skill links that do not resolve.

    Checks the links against the mounts a new sandbox would get. For a
    sandbox created with other mounts, the result can differ; the mount
    drift warnings cover that case. Non-fatal; reads only host files.

    :param config: Resolved msb-circus configuration.
    :param workspace: Workspace directory.
    """
    home = Path(os.environ.get("HOME", str(Path.home())))
    try:
        mounts = (
            effective_mounts(config, workspace)
            + resolve_agent_config_mounts(warn_missing=False)
            + resolve_agent_skill_mounts()
        )
    except ConfigurationError as e:
        logger.debug("Skill link check skipped: %s", e)
        return
    skill_dirs = (
        [workspace / d for d in PROJECT_SKILL_DIRS]
        + [Path(host) for host, _ in resolve_agent_skill_mounts()]
        + [home / d for d in CONFIG_MOUNT_SKILL_DIRS]
    )
    broken = find_broken_skill_links(skill_dirs, mounts)
    if not broken:
        return
    logger.warning(
        "These skill links do not work in the sandbox: %s. Agents in the "
        "sandbox cannot load these skills. Use relative links between "
        "mounted directories (installers such as 'npx skills' create them), "
        "and reinstall skills whose target is missing.",
        "; ".join(f"{b.path} -> {b.target} ({b.problem})" for b in broken),
    )


def _prepare_sandbox_worktrees(config: MsbConfig, workspace: Path) -> None:
    """Create the sandbox worktree directory and warn without the mirror.

    Non-fatal: a failure to create the directory only logs a warning.

    :param config: Resolved msb-circus configuration.
    :param workspace: Workspace directory.
    """
    try:
        ensure_sandbox_worktrees_dir(workspace)
    except OSError as e:
        logger.warning(
            "Could not create %s in %s: %s", SANDBOX_WORKTREES_DIR, workspace, e
        )
    if not config.git_worktree_mirror and has_sandbox_worktrees(workspace):
        logger.warning(
            "%s contains worktrees, but git.worktree_mirror is off. Git "
            "records their paths below /workspace, which do not exist on the "
            "host. Set [git] worktree_mirror = true and re-create the "
            "sandbox: msb-circus rm -f && msb-circus up.",
            workspace / SANDBOX_WORKTREES_DIR,
        )


def _ensure_git_wrapper(
    invoker: MsbInvoker, config: MsbConfig, name: str, workspace: Path
) -> None:
    """Install the guest ``git`` wrapper that guides worktree locations.

    Rewritten on every boot, start, and reuse. With the git worktree
    mirror on, the wrapper sends worktrees from the workspace mount (e.g.
    ``/workspace``) to the mirror at the host path. Failures are
    non-fatal: the guest then uses the plain git.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    :param workspace: Workspace directory (absolute host path).
    """
    guest_spec = resolve_mounts(config.mounts, workspace).get(str(workspace), "")
    guest_root = guest_spec.split(":", 1)[0]
    mirror_root = str(workspace) if config.git_worktree_mirror else ""
    script = git_wrapper_script(guest_root, mirror_root)
    try:
        result = invoker.run(
            build_install_executable_args(name, script, GIT_WRAPPER_GUEST_PATH),
            check=False,
        )
        if result.returncode == 0:
            return
        raise MsbCommandError(
            (result.stderr or result.stdout or "").strip(),
            returncode=result.returncode,
            stderr=result.stderr,
        )
    except MsbCommandError as e:
        logger.warning(
            "Could not install the git wrapper in sandbox %s; worktree "
            "locations are not checked (%s)",
            name,
            e,
        )


def _format_capacity(capacity_bytes: int) -> str:
    """Format a byte count as an msb size string.

    :param capacity_bytes: Size in bytes.
    :returns: ``"<n>G"`` when the size is a whole number of GiB,
        otherwise ``"<n>M"`` (MiB, rounded down).
    """
    gib = 1024 * 1024 * 1024
    if capacity_bytes % gib == 0:
        return f"{capacity_bytes // gib}G"
    return f"{capacity_bytes // (1024 * 1024)}M"


def _check_docker_volume(invoker: MsbInvoker, config: MsbConfig, volume: str) -> None:
    """Check that an existing persistent docker volume fits the config.

    Reads ``msb volume list`` and compares the volume's kind and size
    with ``msb.sandbox.docker_disk``. When msb itself cannot be queried
    the check is skipped at debug level: ``msb run`` still validates the
    volume when it attaches it. When the volume does not exist yet,
    ``--mount-named`` creates it with the configured size.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param volume: Name of the persistent docker volume.
    :raises typer.Exit: If the volume exists with another kind or size.
    """
    try:
        result = invoker.run(build_volume_list_args())
        volumes = parse_volume_list_output(result.stdout)
    except (MsbCommandError, ConfigurationError) as e:
        logger.debug(
            "Could not list msb volumes to check %s; msb validates the "
            "volume at run time (%s)",
            volume,
            e,
        )
        return
    entry = next((v for v in volumes if v.name == volume), None)
    if entry is None:
        logger.info(
            "Creating persistent docker volume %s (%s).",
            volume,
            config.sandbox.docker_disk,
        )
        return
    if entry.kind is not None and entry.kind != "disk":
        typer.echo(
            f"Error: docker volume {volume} exists but is a {entry.kind} "
            f"volume, not a disk volume. Remove it with: msb volume rm {volume}",
            err=True,
        )
        raise typer.Exit(code=1)
    wanted_mib = (
        parse_size_mib(config.sandbox.docker_disk)
        if config.sandbox.docker_disk is not None
        else None
    )
    if (
        entry.capacity_bytes is not None
        and wanted_mib is not None
        and entry.capacity_bytes != wanted_mib * 1024 * 1024
    ):
        typer.echo(
            f"Error: docker volume {volume} has "
            f"{_format_capacity(entry.capacity_bytes)}, but "
            f"msb.sandbox.docker_disk is {config.sandbox.docker_disk}. The "
            "size of an existing volume cannot change. Either set "
            f'docker_disk = "{_format_capacity(entry.capacity_bytes)}", or '
            "delete the volume and its docker images with: "
            f"msb volume rm {volume}",
            err=True,
        )
        raise typer.Exit(code=1)
    logger.info(
        "Reusing persistent docker volume %s (%s).",
        volume,
        (
            _format_capacity(entry.capacity_bytes)
            if entry.capacity_bytes is not None
            else config.sandbox.docker_disk
        ),
    )


def _merged_env(
    config: MsbConfig,
    env_passthrough: tuple[str, ...],
    env: Mapping[str, str],
) -> dict[str, str]:
    """Return the environment of a daemon or hook.

    In increasing precedence: the ``[env]``/``[msb.env]`` variables, the
    top-level ``env_passthrough`` variables, ``SSH_AUTH_SOCK`` (when
    ``[ssh]`` is configured), *env_passthrough*, and *env*.

    :param config: Resolved msb-circus configuration.
    :param env_passthrough: Extra passthrough patterns of the entry.
    :param env: Extra variables of the entry.
    :returns: Merged environment.
    :raises ConfigurationError: If a passthrough variable is unset on the host.
    """
    base = {**config.env, **resolve_passthrough(config.env_passthrough)}
    if config.ssh:
        base = {**base, "SSH_AUTH_SOCK": SSH_AGENT_GUEST_SOCKET}
    return {
        **base,
        **resolve_passthrough(env_passthrough),
        **env,
    }


def _daemon_envs(config: MsbConfig) -> dict[str, dict[str, str]]:
    """Return the merged environment of every configured daemon.

    The environment of a daemon is, in increasing precedence: the
    ``[env]``/``[msb.env]`` variables, the top-level ``env_passthrough``
    variables, ``SSH_AUTH_SOCK`` (when ``[ssh]`` is configured), the
    daemon's own ``env_passthrough`` variables, and its ``env`` entries.

    :param config: Resolved msb-circus configuration.
    :returns: Mapping of daemon name to its merged environment.
    :raises ConfigurationError: If a passthrough variable is unset on the
        host.
    """
    return {
        daemon.name: _merged_env(config, daemon.env_passthrough, daemon.env)
        for daemon in config.daemons
    }


def _daemon_url(config: MsbConfig, port: int) -> str | None:
    """Return the host URL that reaches a guest TCP port, if forwarded.

    :param config: Resolved msb-circus configuration.
    :param port: Guest TCP port the daemon listens on.
    :returns: ``http://`` URL of the first matching TCP port forward, or
        ``None`` when no TCP forward publishes the port.
    """
    forward = next(
        (
            f
            for f in config.port_forwards
            if f.protocol == "tcp" and f.guest_port == port
        ),
        None,
    )
    if forward is None:
        return None
    if forward.host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    elif ":" in forward.host:
        host = f"[{forward.host}]"
    else:
        host = forward.host
    return f"http://{host}:{forward.host_port}"


def _wait_for_daemon_port(
    invoker: MsbInvoker, config: MsbConfig, name: str, daemon: DaemonConfig
) -> bool:
    """Wait until an autostarted daemon's guest port accepts connections.

    The daemon must have a ``port``; callers filter daemons without one.
    The wait probes the guest port for :data:`DAEMON_READY_TIMEOUT`
    seconds and never raises.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    :param daemon: Daemon to wait for; ``daemon.port`` must not be ``None``.
    :returns: Whether the port opened in time.
    """
    port = daemon.port
    if port is None:
        return True
    try:
        result = invoker.run(
            build_exec_args(
                name,
                ["sh", "-c", build_port_wait_script(port, DAEMON_READY_TIMEOUT)],
                tty=None,
            ),
            check=False,
            timeout=DAEMON_READY_TIMEOUT + 15,
        )
    except MsbCommandError as e:
        logger.debug("Port wait for daemon %s in %s failed: %s", daemon.name, name, e)
        result = None
    url = _daemon_url(config, port)
    if result is not None and result.returncode == 0:
        logger.info(
            "Daemon %s is ready on guest port %s%s",
            daemon.name,
            port,
            f" ({url})" if url else "",
        )
        return True
    logger.warning(
        "Daemon %s did not open guest port %s within %s s. "
        "See: msb-circus daemon logs %s",
        daemon.name,
        port,
        DAEMON_READY_TIMEOUT,
        daemon.name,
    )
    return False


def _ensure_daemons(
    invoker: MsbInvoker, config: MsbConfig, name: str, workspace: Path
) -> None:
    """Register the configured daemons with s6 inside the sandbox.

    Runs the install script from :mod:`msb_circus.daemons` in the guest
    and waits for the ports of autostarted daemons. Never raises: config
    or msb failures warn and leave the sandbox running without daemons.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    :param workspace: Workspace directory (for the daemon workdir).
    """
    if not config.daemons:
        return
    try:
        envs = _daemon_envs(config)
        script = build_install_script(
            config.daemons, resolve_agent_workdir(config, workspace)
        )
        result = invoker.run(
            build_exec_args(
                name,
                ["sh", "-c", script],
                env=encode_env_files(envs),
                tty=None,
            ),
            check=False,
        )
    except (ConfigurationError, MsbCommandError, ValueError) as e:
        logger.warning("Daemons are not registered in %s: %s", name, e)
        return
    if result.returncode == 3:
        logger.warning(
            "Daemons are not registered in %s: the sandbox image has no "
            "s6-overlay as PID 1",
            name,
        )
        return
    if result.returncode == EXIT_SVSCAN_NOT_READY:
        logger.warning(
            "Daemons are not registered in %s: s6-svscan did not start within %.0f s",
            name,
            SVSCAN_WAIT_TRIES * SVSCAN_WAIT_INTERVAL_S,
        )
        return
    if result.returncode != 0:
        logger.warning(
            "Daemons are not registered in %s (exit code %s): %s",
            name,
            result.returncode,
            (result.stderr or "").strip(),
        )
        return
    logger.info("Registered %d daemon(s) in %s", len(config.daemons), name)
    for daemon in config.daemons:
        if daemon.autostart and daemon.port is not None:
            _wait_for_daemon_port(invoker, config, name, daemon)


def _hook_failed(hook: HookConfig, reason: str) -> bool:
    """Log a failed hook; return ``False`` only for a required hook.

    :param hook: The failed hook.
    :param reason: Short reason for the log message.
    :returns: ``True`` when the command may continue.
    """
    hint = f"see 'msb-circus hook logs {hook.name}'"
    if hook.required:
        logger.error("The required %s hook failed (%s); %s.", hook.name, reason, hint)
        return False
    logger.warning("The %s hook failed (%s); %s.", hook.name, reason, hint)
    return True


def _run_hook(
    invoker: MsbInvoker,
    config: MsbConfig,
    name: str,
    workspace: Path,
    hook: HookConfig,
    content: str,
    *,
    force: bool,
    quiet: bool,
    strict: bool = False,
) -> bool:
    """Run one hook in the sandbox.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    :param workspace: Workspace directory (for the default workdir).
    :param hook: Hook to run.
    :param content: Script content (from :func:`load_hook_script`).
    :param force: Run even when the hook is up to date.
    :param quiet: Capture the output instead of showing it (ACP mode).
    :param strict: Treat every failure as fatal, even for an optional
        hook (interactive ``hook run`` mode).
    :returns: ``True`` when the command may continue (success, or failure
        of an optional hook); always ``False`` on failure in strict mode.
    """

    def fail(reason: str) -> bool:
        """Log the failure; honor strict mode."""
        ok = _hook_failed(hook, reason)
        return ok and not strict

    key = hook_key_hash(content) if hook.name == "provision" else None
    try:
        env = {
            **_merged_env(config, hook.env_passthrough, hook.env),
            **build_run_env(content, key, hook.timeout),
        }
    except ConfigurationError as e:
        return fail(str(e))
    workdir = hook.workdir or resolve_agent_workdir(config, workspace)
    logger.info(
        "Running the %s hook in %s (log in the sandbox: %s)...",
        hook.name,
        name,
        log_path(hook.name),
    )
    try:
        result = invoker.run(
            build_exec_args(
                name,
                build_hook_run_args(hook.name, force),
                env=env,
                workdir=workdir,
                tty=None,
            ),
            capture_output=quiet,
            check=False,
        )
    except MsbCommandError as e:
        return fail(str(e))
    rc = result.returncode
    if rc == 0:
        logger.info("The %s hook finished in %s.", hook.name, name)
        return True
    if rc == EXIT_TIMEOUT and hook.timeout:
        reason = f"timed out after {hook.timeout} s"
    elif rc == EXIT_RUNNER_FAILED:
        reason = "the hook runner failed in the sandbox"
    else:
        reason = f"exit code {rc}"
    return fail(reason)


def _ensure_hooks(
    invoker: MsbInvoker,
    config: MsbConfig,
    name: str,
    workspace: Path,
    *,
    run_provision: bool,
    quiet: bool,
) -> bool:
    """Run the due project hooks (``[msb.hooks]``) in the sandbox.

    ``provision`` runs first, then ``startup``. With *run_provision*
    false, a due ``provision`` hook is only reported. A failing check
    never blocks the sandbox.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    :param workspace: Workspace directory.
    :param run_provision: Run a due ``provision`` hook (else warn only).
    :param quiet: Capture hook output instead of showing it (ACP mode).
    :returns: ``False`` if a required hook failed, else ``True``.
    """
    hooks = [h for h in (config.hooks.provision, config.hooks.startup) if h is not None]
    if not hooks:
        return True
    contents: dict[str, str] = {}
    for hook in hooks:
        try:
            contents[hook.name] = load_hook_script(hook, workspace)
        except ConfigurationError as e:
            if not _hook_failed(hook, str(e)):
                return False
    if not contents:
        return True
    provision_hash = (
        hook_key_hash(contents["provision"]) if "provision" in contents else None
    )
    try:
        check = invoker.run(
            build_exec_args(
                name,
                ["sh", "-c", build_check_script()],
                env=build_check_env(provision_hash, "startup" in contents),
                tty=None,
            ),
            check=False,
        )
    except MsbCommandError as e:
        logger.warning("Could not check the hooks in %s: %s", name, e)
        return True
    if check.returncode != 0:
        logger.warning(
            "Could not check the hooks in %s (exit code %s).",
            name,
            check.returncode,
        )
        return True
    due = parse_check_output(check.stdout or "")
    provision = config.hooks.provision
    if "provision" in due and provision is not None and "provision" in contents:
        if not run_provision:
            logger.warning(
                "The provision hook in %s is new or changed; run 'msb-circus up' "
                "to run it.",
                name,
            )
        else:
            if due["provision"] == "changed":
                logger.info(
                    "The provision script changed since the last run in %s.", name
                )
            if not _run_hook(
                invoker,
                config,
                name,
                workspace,
                provision,
                contents["provision"],
                force=False,
                quiet=quiet,
            ):
                return False
    startup = config.hooks.startup
    if "startup" in due and startup is not None and "startup" in contents:
        ran = _run_hook(
            invoker,
            config,
            name,
            workspace,
            startup,
            contents["startup"],
            force=False,
            quiet=quiet,
        )
        if not ran:
            return False
    return True


def _exit_on_hook_failure() -> None:
    """Exit after a required hook failed; the sandbox keeps running.

    :raises typer.Exit: Always, with exit code 1.
    """
    typer.echo(
        "Error: a required hook failed. The sandbox keeps running. Fix the "
        "hook and run 'msb-circus up' again.",
        err=True,
    )
    raise typer.Exit(code=1)


def _ensure_sandbox_running(
    invoker: MsbInvoker,
    config: MsbConfig,
    workspace: Path,
    *,
    allow_build: bool = False,
    engine: str = "podman",
    sync_daemons: bool = False,
    quiet: bool = False,
) -> None:
    """Ensure the workspace sandbox exists and is running.

    Status messages go through logging (stderr by default, file-only in
    ACP mode), so this is safe to call before an ACP stream that owns
    stdout and must not receive stderr output. Fatal errors print to
    stderr and exit.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param workspace: Workspace directory.
    :param allow_build: Build and import the image when it is missing
        instead of failing.
    :param engine: Host engine used when building.
    :param sync_daemons: Also register daemons in an already-running
        sandbox, to apply config changes.
    :param quiet: Capture hook output (ACP mode).
    :raises typer.Exit: If the image is missing and building is not allowed,
        or the workspace is in a linked Git worktree, or a required hook
        fails. Hooks run after the dockerd check and before daemons.
    """
    _fail_on_linked_worktree(workspace)
    _repair_guest_path_worktrees(config, workspace)
    _warn_on_missing_worktrees(workspace)
    _warn_on_broken_skill_links(config, workspace)
    _prepare_sandbox_worktrees(config, workspace)
    name = sandbox_name_for_workspace(workspace)
    image_tag = config.image.tag
    try:
        effective_mounts(config, workspace)
    except ConfigurationError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    link = _prepare_ssh_agent(config)

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
        logger.info("Sandbox %s is already running; reusing it.", name)
        _ensure_pi_trust(invoker, config, name, workspace)
        _ensure_agent_config_copies(invoker, name)
        _ensure_host_entries(invoker, config, name)
        _ensure_claude_trust(invoker, config, name, workspace)
        _repair_codex_daemon(invoker, name)
        _ensure_ssh_files(invoker, config, name)
        _ensure_git_config(invoker, config, name)
        _ensure_git_wrapper(invoker, config, name, workspace)
        _check_ssh_agent(invoker, config, name)
        _warn_on_agent_config_mount_drift(invoker, name)
        _warn_on_agent_skill_mount_drift(invoker, name)
        _warn_on_port_drift(invoker, config, name)
        _warn_on_disk_drift(invoker, config, name)
        _warn_on_mount_drift(invoker, config, name, workspace)
        _warn_on_trust_host_cas_drift(invoker, config, name)
        _check_dockerd(invoker, config, name)
        if not _ensure_hooks(
            invoker, config, name, workspace, run_provision=sync_daemons, quiet=quiet
        ):
            _exit_on_hook_failure()
        if sync_daemons:
            _ensure_daemons(invoker, config, name, workspace)
        return

    if existing is not None:
        logger.info("Starting existing sandbox %s...", name)
        _fail_on_occupied_ports(config)
        invoker.run(build_start_args(name))
        _ensure_pi_trust(invoker, config, name, workspace)
        _ensure_agent_config_copies(invoker, name)
        _ensure_host_entries(invoker, config, name)
        _ensure_claude_trust(invoker, config, name, workspace)
        _repair_codex_daemon(invoker, name)
        _ensure_ssh_files(invoker, config, name)
        _ensure_git_config(invoker, config, name)
        _ensure_git_wrapper(invoker, config, name, workspace)
        _check_ssh_agent(invoker, config, name)
        _warn_on_agent_config_mount_drift(invoker, name)
        _warn_on_agent_skill_mount_drift(invoker, name)
        _warn_on_port_drift(invoker, config, name)
        _warn_on_disk_drift(invoker, config, name)
        _warn_on_mount_drift(invoker, config, name, workspace)
        _warn_on_trust_host_cas_drift(invoker, config, name)
    else:
        logger.info("Booting sandbox %s from %s...", name, image_tag)
        docker_volume = (
            docker_volume_name_for_workspace(workspace)
            if config.sandbox.docker_disk_persist
            else None
        )
        if docker_volume is not None:
            _check_docker_volume(invoker, config, docker_volume)
        _fail_on_occupied_ports(config)
        try:
            invoker.run(
                build_run_args(
                    name=name,
                    image=image_tag,
                    memory=config.sandbox.memory,
                    cpus=config.sandbox.cpus,
                    mounts=(
                        effective_mounts(config, workspace)
                        + resolve_agent_config_mounts()
                        + resolve_agent_skill_mounts()
                    ),
                    net=config.network.mode,
                    port_forwards=config.port_forwards,
                    workdir=resolve_agent_workdir(config, workspace),
                    docker_disk=config.sandbox.docker_disk,
                    root_disk=config.sandbox.root_disk,
                    init=(
                        None
                        if config.sandbox.init == INIT_NONE
                        else config.sandbox.init
                    ),
                    trust_host_cas=config.network.trust_host_cas,
                    env={
                        **config.env,
                        DOCKERD_SHUTDOWN_TIMEOUT_ENV_VAR: str(
                            config.sandbox.docker_stop_timeout
                        ),
                        **(ssh_sandbox_env() if link else {}),
                    },
                    vsock=[(str(link), SSH_AGENT_VSOCK_PORT)] if link else None,
                    docker_volume=docker_volume,
                )
            )
        except MsbCommandError as e:
            if docker_volume is not None and "already attached" in f"{e} {e.stderr}":
                invoker.run(build_remove_args(name, force=True), check=False)
                typer.echo(
                    f"Error: docker volume {docker_volume} is in use by another "
                    "running sandbox. A disk volume can only be attached to one "
                    "sandbox at a time. Stop the other sandbox and try again.",
                    err=True,
                )
                raise typer.Exit(code=1) from e
            raise
        _ensure_pi_trust(invoker, config, name, workspace)
        _ensure_agent_config_copies(invoker, name)
        _ensure_host_entries(invoker, config, name)
        _ensure_claude_trust(invoker, config, name, workspace)
        _repair_codex_daemon(invoker, name)
        _ensure_ssh_files(invoker, config, name)
        _ensure_git_config(invoker, config, name)
        _ensure_git_wrapper(invoker, config, name, workspace)
        _check_ssh_agent(invoker, config, name)

    _check_dockerd(invoker, config, name)
    if not _ensure_hooks(
        invoker, config, name, workspace, run_provision=True, quiet=quiet
    ):
        _exit_on_hook_failure()
    _ensure_daemons(invoker, config, name, workspace)


def _stop_sandbox(invoker: MsbInvoker, config: MsbConfig, name: str) -> None:
    """Stop the sandbox, working around msb's stop with an s6 init.

    With s6-overlay as PID 1, ``msb stop`` never completes (the guest
    agent no longer is PID 1). The shutdown is then triggered in the
    guest (``kill -TERM 1``) and msb-circus waits until msb reports the
    sandbox as stopped, at most ``stop_timeout`` seconds, before it
    forces the stop. Other sandboxes use plain ``msb stop``.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    :raises MsbCommandError: If the plain or forced ``msb stop`` fails.
    """
    if config.sandbox.init == INIT_NONE:
        invoker.run(build_stop_args(name))
        return
    try:
        result = invoker.run(build_pid1_probe_args(name), check=False)
    except MsbCommandError:
        result = None
    if (
        result is None
        or result.returncode != 0
        or ((result.stdout or "").strip() != S6_PID1_COMM)
    ):
        invoker.run(build_stop_args(name))
        return
    logger.info("Shutting down the services in %s...", name)
    if config.daemons:
        logger.info("Stopping the daemons in %s...", name)
        try:
            invoker.run(
                build_exec_args(name, ["sh", "-c", build_stop_all_script()], tty=None),
                check=False,
            )
        except MsbCommandError as e:
            logger.debug("Stopping the daemons in %s failed: %s", name, e)
    try:
        invoker.run(build_init_shutdown_args(name), check=False)
    except MsbCommandError as e:
        logger.debug("Init shutdown exec in %s failed: %s", name, e)
    deadline = _monotonic() + config.sandbox.stop_timeout
    while _monotonic() < deadline:
        try:
            sandboxes = _list_sandboxes(invoker, running_only=False)
        except (MsbCommandError, ConfigurationError) as e:
            logger.debug("Sandbox list during stop of %s failed: %s", name, e)
            sandboxes = []
        entry = next((s for s in sandboxes if s.name == name), None)
        if entry is None or (entry.status or "").lower() == "stopped":
            return
        time.sleep(STOP_POLL_INTERVAL)
    logger.warning(
        "Sandbox %s did not stop within %s s; forcing the stop "
        "(unsynced writes may be lost)",
        name,
        config.sandbox.stop_timeout,
    )
    invoker.run(build_stop_args(name, force=True))


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
    config, invoker = _resolve_invoker(workspace)
    name = sandbox_name_for_workspace(workspace)
    _stop_sandbox(invoker, config, name)
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
    _fail_on_linked_worktree(workspace)
    config, invoker = _resolve_invoker(workspace)
    _repair_guest_path_worktrees(config, workspace)
    _warn_on_missing_worktrees(workspace)
    _warn_on_broken_skill_links(config, workspace)
    _prepare_sandbox_worktrees(config, workspace)
    name = sandbox_name_for_workspace(workspace)
    # A running sandbox holds its own forwarded ports; only probe before a
    # real start. If the status probe fails, still run the check.
    try:
        running = {s.name for s in _list_sandboxes(invoker, running_only=True)}
    except (MsbCommandError, ConfigurationError):
        running = set()
    if name not in running:
        _fail_on_occupied_ports(config)
    _prepare_ssh_agent(config)
    invoker.run(build_start_args(name))
    _ensure_host_entries(invoker, config, name)
    _ensure_ssh_files(invoker, config, name)
    _ensure_git_config(invoker, config, name)
    _ensure_git_wrapper(invoker, config, name, workspace)
    _check_ssh_agent(invoker, config, name)
    _warn_on_disk_drift(invoker, config, name)
    _warn_on_mount_drift(invoker, config, name, workspace)
    _warn_on_trust_host_cas_drift(invoker, config, name)
    if docker_enabled(config):
        ready, last_error = _probe_dockerd(invoker, name)
        if ready:
            typer.echo(f"✓ Started {name}; dockerd is ready.")
        else:
            typer.echo(_dockerd_warning(name, last_error), err=True)
    else:
        typer.echo(f"✓ Started {name} (docker is not enabled for this sandbox)")
    if not _ensure_hooks(
        invoker, config, name, workspace, run_provision=True, quiet=False
    ):
        _exit_on_hook_failure()
    _ensure_daemons(invoker, config, name, workspace)


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
    volumes: Annotated[
        bool,
        typer.Option(
            "--volumes",
            help="Also delete the persistent docker volume (all docker images and data in it).",
        ),
    ] = False,
) -> None:
    """Remove the workspace sandbox including all guest state.

    This stops the sandbox if it is running and deletes guest-only state
    (image layers, caches). Workspace files mounted from the host are not
    affected. A persistent docker volume is kept unless --volumes is given.
    """
    _config, invoker = _resolve_invoker(workspace)
    name = sandbox_name_for_workspace(workspace)
    volume = docker_volume_name_for_workspace(workspace)
    existing = _docker_volumes(invoker)
    missing = find_missing_worktrees(workspace)
    if missing:
        typer.echo(
            "Warning: these Git worktrees do not exist on the host and are "
            "lost with the sandbox: "
            + ", ".join(str(p) for p in missing)
            + ". Commit and push their changes first.",
            err=True,
        )
    if not force:
        if volumes and existing is not None and volume in existing:
            typer.echo(
                f"This will remove sandbox {name} and all its guest state, "
                f"and delete docker volume {volume} with all docker images in it."
            )
        else:
            typer.echo(f"This will remove sandbox {name} and all its guest state.")
        if not typer.confirm("Are you sure you want to continue?"):
            typer.echo("Aborted.")
            raise typer.Exit(code=0)
    invoker.run(build_remove_args(name, force=True))
    typer.echo(f"✓ Destroyed {name}")
    _finish_docker_volume(invoker, existing, volume, delete=volumes)


@app.command(name="exec")
def exec_(
    ctx: typer.Context,
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
            help="Guest working directory. Defaults to [msb] agent.workdir, or the "
            "workspace host path when git.worktree_mirror is on.",
        ),
    ] = None,
    trace: Annotated[
        bool | None,
        typer.Option(
            "--trace/--no-trace",
            help="Record resource and transport traces for this execution.",
        ),
    ] = None,
    trace_dir: Annotated[
        Path | None,
        typer.Option(
            "--trace-dir",
            help=(
                "Trace root directory. Relative paths use the project "
                "workspace as their base."
            ),
        ),
    ] = None,
    trace_payloads: Annotated[
        bool | None,
        typer.Option(
            "--trace-payloads/--no-trace-payloads",
            help="Save raw ACP streams. These files can contain sensitive data.",
        ),
    ] = None,
    trace_sample_interval: Annotated[
        float | None,
        typer.Option(
            "--trace-sample-interval",
            min=0.1,
            help="Seconds between resource samples.",
        ),
    ] = None,
) -> None:
    """Run a command (or an ACP session) in the workspace sandbox.

    Boot the sandbox first with 'msb-circus up'; this command boots it
    automatically when it is not running. In ACP mode, warnings and status
    messages go to the log file only (never stderr, which the ACP backend
    treats as an error channel) and stdout carries only the ACP byte
    stream. Fatal errors still print to stderr right before exiting.
    """
    if acp:
        _configure_acp_logging(ctx.obj)
    config, invoker = _resolve_invoker(workspace)
    name = sandbox_name_for_workspace(workspace)
    cmd = list(command) if command else list(config.agent.command)
    guest_workdir = workdir or resolve_agent_workdir(config, workspace)

    try:
        env_map = {
            **config.env,
            **resolve_passthrough(config.env_passthrough),
            **({"SSH_AUTH_SOCK": SSH_AGENT_GUEST_SOCKET} if config.ssh else {}),
            **parse_env_assignments(env or []),
        }
    except ConfigurationError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    _ensure_sandbox_running(invoker, config, workspace, quiet=acp)

    tracing = replace(
        config.tracing,
        enabled=(
            trace
            if trace is not None
            else (True if trace_payloads is True else config.tracing.enabled)
        ),
        directory=(
            str(trace_dir) if trace_dir is not None else config.tracing.directory
        ),
        capture_payloads=(
            trace_payloads
            if trace_payloads is not None
            else config.tracing.capture_payloads
        ),
        sample_interval=(
            trace_sample_interval
            if trace_sample_interval is not None
            else config.tracing.sample_interval
        ),
    )
    trace_session: TraceSession | None = None
    if tracing.enabled:
        root = Path(tracing.directory)
        if not root.is_absolute():
            root = workspace / root
        try:
            trace_session = TraceSession.create(
                workspace=workspace,
                root=root,
                config=tracing,
                msb_config=config,
                sandbox=name,
                command=cmd,
                acp=acp,
                workdir=guest_workdir,
                env_names=list(env_map),
                msb_version=probe_version(invoker.binary),
            )
        except OSError as e:
            typer.echo(f"Error: could not create trace files: {e}", err=True)
            raise typer.Exit(code=1) from e
        logger.info("Trace directory: %s", trace_session.directory)

    argv = [
        str(invoker.binary),
        *build_exec_args(
            name,
            cmd,
            acp=acp,
            env=env_map or None,
            workdir=guest_workdir,
            user=user,
        ),
    ]
    returncode = 1
    try:
        if acp:
            if trace_session is None:
                returncode = run_acp_stream(argv)
            else:
                returncode = run_acp_stream(
                    argv,
                    observer=trace_session.record_stream,
                    on_started=lambda _proc: trace_session.start_collectors(
                        invoker.binary, name
                    ),
                )
        else:
            if trace_session is None:
                returncode = subprocess.run(argv, check=False).returncode
            else:
                with subprocess.Popen(argv) as process:
                    trace_session.start_collectors(invoker.binary, name)
                    returncode = process.wait()
    except FileNotFoundError as e:
        typer.echo(f"Error: msb binary not found: {e}", err=True)
        raise typer.Exit(code=1) from e
    finally:
        if trace_session is not None:
            try:
                trace_session.finish(returncode)
            except OSError as e:
                logger.warning("Could not finish the execution trace: %s", e)
    if returncode != 0:
        raise typer.Exit(code=returncode)


def _format_columns(rows: list[list[str]]) -> str:
    """Format table rows as aligned, space-separated columns.

    Every column is padded with :meth:`str.ljust` to its widest cell
    plus 2 spaces of separation; the last column is not padded. Trailing
    spaces are stripped from each line.

    :param rows: Table rows, header row first, all of equal width.
    :returns: The rendered table, lines joined with newlines.
    """
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for row in rows:
        cells = [cell.ljust(width) for cell, width in zip(row[:-1], widths[:-1])]
        cells.append(row[-1])
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def _require_running_sandbox(invoker: MsbInvoker, name: str) -> None:
    """Exit with an error when the sandbox is not running.

    :param invoker: Invoker bound to the resolved msb binary.
    :param name: Sandbox name to look for among the running sandboxes.
    :raises typer.Exit: With code 1 when the list call fails or the
        sandbox is not running.
    """
    try:
        sandboxes = _list_sandboxes(invoker, running_only=True)
    except (MsbCommandError, ConfigurationError):
        sandboxes = []
    if not any(sandbox.name == name for sandbox in sandboxes):
        typer.echo(
            f"Error: sandbox {name} is not running. Start it with: msb-circus up",
            err=True,
        )
        raise typer.Exit(code=1)


def _require_daemon(config: MsbConfig, daemon_name: str) -> DaemonConfig:
    """Return the configured daemon with the given name, or exit.

    :param config: Resolved msb-circus configuration.
    :param daemon_name: Daemon name to look up.
    :returns: The matching daemon configuration.
    :raises typer.Exit: With code 1 when no configured daemon matches.
    """
    for daemon in config.daemons:
        if daemon.name == daemon_name:
            return daemon
    names = [daemon.name for daemon in config.daemons]
    hint = f" (configured: {', '.join(names)})" if names else ""
    typer.echo(
        f"Error: no daemon named {daemon_name!r} in [[msb.daemons]]{hint}",
        err=True,
    )
    raise typer.Exit(code=1)


def _require_hook(config: MsbConfig, hook_name: str) -> HookConfig:
    """Return the configured hook with the given name, or exit.

    :param config: Resolved msb-circus configuration.
    :param hook_name: Hook name to look up (``provision`` or ``startup``).
    :returns: The matching hook configuration.
    :raises typer.Exit: With code 1 when the name is unknown or the hook
        is not configured.
    """
    if hook_name not in HOOK_NAMES:
        typer.echo(
            f"Error: unknown hook {hook_name!r}; use provision or startup.",
            err=True,
        )
        raise typer.Exit(code=1)
    hook = getattr(config.hooks, hook_name)
    if hook is None:
        typer.echo(
            f"Error: the {hook_name} hook is not configured in [msb.hooks].",
            err=True,
        )
        raise typer.Exit(code=1)
    return hook


def _run_daemon_script(
    invoker: MsbInvoker, name: str, daemon_name: str, script: str
) -> None:
    """Run a daemon control script in the sandbox or exit with an error.

    :param invoker: Invoker bound to the resolved msb binary.
    :param name: Sandbox name.
    :param daemon_name: Daemon the script acts on (for error messages).
    :param script: Shell script text to run in the guest.
    :raises typer.Exit: With code 1 when the exec fails, the daemon is
        not registered (script exit code 4), or the script fails.
    """
    try:
        result = invoker.run(
            build_exec_args(name, ["sh", "-c", script], tty=None), check=False
        )
    except MsbCommandError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    if result.returncode == 4:
        typer.echo(
            f"Error: daemon {daemon_name} is not registered in {name}. "
            "Register it with: msb-circus up",
            err=True,
        )
        raise typer.Exit(code=1)
    if result.returncode != 0:
        typer.echo(
            f"Error: the daemon command failed (exit code {result.returncode}): "
            f"{(result.stderr or '').strip()}",
            err=True,
        )
        raise typer.Exit(code=1)


def _print_daemon_ready(
    invoker: MsbInvoker, config: MsbConfig, name: str, daemon: DaemonConfig
) -> None:
    """Wait for a daemon's port and print its host URL when it opened.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    :param daemon: Daemon that was started or restarted.
    """
    if daemon.port is None:
        return
    if not _wait_for_daemon_port(invoker, config, name, daemon):
        return
    url = _daemon_url(config, daemon.port)
    if url:
        print(f"  {url}")


@app.command(name="daemons")
def daemons_list(
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
    """List the configured daemons and their state in the workspace sandbox."""
    config, invoker = _resolve_invoker(workspace)
    name = sandbox_name_for_workspace(workspace)
    _require_running_sandbox(invoker, name)
    try:
        result = invoker.run(
            build_exec_args(name, ["sh", "-c", build_status_script()], tty=None),
            check=False,
        )
    except MsbCommandError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    if result.returncode != 0:
        typer.echo(
            f"Error: the daemon status check failed (exit code {result.returncode}): "
            f"{(result.stderr or '').strip()}",
            err=True,
        )
        raise typer.Exit(code=1)
    statuses = parse_status_output(result.stdout or "")
    if not config.daemons and not statuses:
        typer.echo(
            "No daemons configured. Add them under [[msb.daemons]] "
            "in .agent-circus/config.toml."
        )
        return
    configured = {daemon.name for daemon in config.daemons}
    rows = [["NAME", "STATE", "PID", "UPTIME", "PORT", "URL"]]
    for daemon in config.daemons:
        status = statuses.get(daemon.name)
        url = _daemon_url(config, daemon.port) if daemon.port is not None else None
        rows.append(
            [
                daemon.name,
                status.state if status is not None else "not registered",
                str(status.pid)
                if status is not None and status.pid is not None
                else "-",
                format_duration(status.seconds) if status is not None else "-",
                str(daemon.port) if daemon.port is not None else "-",
                url or "-",
            ]
        )
    for status_name, status in statuses.items():
        if status_name in configured:
            continue
        rows.append(
            [
                status_name,
                f"{status.state} (not in config)",
                str(status.pid) if status.pid is not None else "-",
                format_duration(status.seconds),
                "-",
                "-",
            ]
        )
    print(_format_columns(rows))


@app.command(name="hooks")
def hooks_list(
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
    """List the configured hooks and their state in the workspace sandbox."""
    config, invoker = _resolve_invoker(workspace)
    name = sandbox_name_for_workspace(workspace)
    if config.hooks.provision is None and config.hooks.startup is None:
        typer.echo(
            "No hooks configured. Add them under [msb.hooks] "
            "in .agent-circus/config.toml."
        )
        return
    _require_running_sandbox(invoker, name)
    try:
        result = invoker.run(
            build_exec_args(name, ["sh", "-c", build_hook_status_script()], tty=None),
            check=False,
        )
    except MsbCommandError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    if result.returncode != 0:
        typer.echo(
            f"Error: the hook status check failed (exit code {result.returncode}): "
            f"{(result.stderr or '').strip()}",
            err=True,
        )
        raise typer.Exit(code=1)
    boot, statuses = parse_hook_status_output(result.stdout or "")
    rows = [["HOOK", "STATE", "LAST EXIT", "FINISHED"]]
    for hook_name in HOOK_NAMES:
        hook = getattr(config.hooks, hook_name)
        status = statuses.get(hook_name)
        if hook is None:
            rows.append([hook_name, "not configured", "-", "-"])
            continue
        if status is None:
            rows.append([hook_name, "never run", "-", "-"])
            continue
        if hook_name == "provision":
            try:
                content = load_hook_script(hook, workspace)
            except ConfigurationError as e:
                state = f"error: {e}"
            else:
                state = "up to date" if status.key == hook_key_hash(content) else "due"
        else:
            state = "up to date" if boot is not None and status.key == boot else "due"
        rows.append(
            [
                hook_name,
                state,
                str(status.exit_code) if status.exit_code is not None else "-",
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(status.finished))
                if status.finished is not None
                else "-",
            ]
        )
    print(_format_columns(rows))


@daemon_app.command(name="start")
def daemon_start(
    daemon_name: Annotated[
        str, typer.Argument(help="Daemon name from [[msb.daemons]].")
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
    """Start a configured daemon in the workspace sandbox."""
    config, invoker = _resolve_invoker(workspace)
    name = sandbox_name_for_workspace(workspace)
    daemon = _require_daemon(config, daemon_name)
    _require_running_sandbox(invoker, name)
    _run_daemon_script(invoker, name, daemon_name, build_start_script(daemon_name))
    print(f"✓ Started daemon {daemon_name}")
    _print_daemon_ready(invoker, config, name, daemon)


@daemon_app.command(name="stop")
def daemon_stop(
    daemon_name: Annotated[
        str, typer.Argument(help="Daemon name from [[msb.daemons]].")
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
    """Stop a configured daemon in the workspace sandbox."""
    config, invoker = _resolve_invoker(workspace)
    name = sandbox_name_for_workspace(workspace)
    _require_daemon(config, daemon_name)
    _require_running_sandbox(invoker, name)
    _run_daemon_script(invoker, name, daemon_name, build_stop_script(daemon_name))
    print(f"✓ Stopped daemon {daemon_name}")


@daemon_app.command(name="restart")
def daemon_restart(
    daemon_name: Annotated[
        str, typer.Argument(help="Daemon name from [[msb.daemons]].")
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
    """Restart a configured daemon in the workspace sandbox."""
    config, invoker = _resolve_invoker(workspace)
    name = sandbox_name_for_workspace(workspace)
    daemon = _require_daemon(config, daemon_name)
    _require_running_sandbox(invoker, name)
    _run_daemon_script(invoker, name, daemon_name, build_restart_script(daemon_name))
    print(f"✓ Restarted daemon {daemon_name}")
    _print_daemon_ready(invoker, config, name, daemon)


@daemon_app.command(name="logs")
def daemon_logs(
    daemon_name: Annotated[
        str, typer.Argument(help="Daemon name from [[msb.daemons]].")
    ],
    lines: Annotated[
        int,
        typer.Option("--lines", "-n", min=1, help="Number of lines to show."),
    ] = 100,
    follow: Annotated[
        bool,
        typer.Option("--follow", "-f", help="Keep printing new log lines."),
    ] = False,
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
    """Show the log output of a configured daemon."""
    config, invoker = _resolve_invoker(workspace)
    name = sandbox_name_for_workspace(workspace)
    _require_daemon(config, daemon_name)
    _require_running_sandbox(invoker, name)
    try:
        result = invoker.run(
            build_exec_args(
                name, build_logs_command(daemon_name, lines, follow), tty=None
            ),
            check=False,
            capture_output=False,
        )
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except MsbCommandError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)


@hook_app.command(name="run")
def hook_run(
    hook_name: Annotated[str, typer.Argument(help="provision or startup.")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Run the hook even when it is up to date."),
    ] = False,
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
    """Run a project hook in the running workspace sandbox now."""
    config, invoker = _resolve_invoker(workspace)
    name = sandbox_name_for_workspace(workspace)
    hook = _require_hook(config, hook_name)
    _require_running_sandbox(invoker, name)
    try:
        content = load_hook_script(hook, workspace)
    except ConfigurationError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    ok = _run_hook(
        invoker,
        config,
        name,
        workspace,
        hook,
        content,
        force=force,
        quiet=False,
        strict=True,
    )
    if not ok:
        raise typer.Exit(code=1)


@hook_app.command(name="logs")
def hook_logs(
    hook_name: Annotated[str, typer.Argument(help="provision or startup.")],
    lines: Annotated[
        int,
        typer.Option("--lines", "-n", min=1, help="Number of lines to show."),
    ] = 100,
    follow: Annotated[
        bool,
        typer.Option("--follow", "-f", help="Keep printing new log lines."),
    ] = False,
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
    """Show the log of the last run of a project hook."""
    config, invoker = _resolve_invoker(workspace)
    name = sandbox_name_for_workspace(workspace)
    _require_hook(config, hook_name)
    _require_running_sandbox(invoker, name)
    try:
        result = invoker.run(
            build_exec_args(
                name, build_hook_logs_command(hook_name, lines, follow), tty=None
            ),
            check=False,
            capture_output=False,
        )
    except KeyboardInterrupt:
        raise typer.Exit(code=130) from None
    except MsbCommandError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    if result.returncode != 0:
        raise typer.Exit(code=result.returncode)


def run_cli() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    run_cli()
