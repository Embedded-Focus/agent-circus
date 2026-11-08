"""Main CLI entry point for msb-circus."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import (
    ADDITIONAL_DIRS_GUEST_BASE,
    AGENT_CONFIG_FILE_COPIES,
    SSH_AGENT_GUEST_SOCKET,
    SSH_AGENT_VSOCK_PORT,
    MsbConfig,
    docker_enabled,
    effective_mounts,
    load_config,
    resolve_agent_config_mounts,
    sandbox_name_for_workspace,
)
from .envs import parse_env_assignments, resolve_passthrough
from .exceptions import ConfigurationError, MsbCircusError, MsbCommandError
from .exec_ import (
    build_exec_args,
    build_prepare_ssh_dir_args,
    build_repair_codex_daemon_args,
    build_ssh_agent_probe_args,
    build_write_claude_trust_args,
    build_write_trust_args,
    run_acp_stream,
)
from .hostports import find_occupied_ports
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
    build_copy_args,
    build_inspect_args,
    build_list_args,
    build_remove_args,
    build_run_args,
    build_start_args,
    build_stop_args,
    configured_ports,
    parse_inspect_bind_mounts,
    parse_inspect_disk_config,
    parse_inspect_ports,
    parse_root_disk_spec,
    parse_sandbox_list_output,
    parse_size_mib,
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

logger = logging.getLogger(__name__)

#: Budget for the post-boot/start dockerd readiness probe.
DOCKERD_PROBE_ATTEMPTS = 20
DOCKERD_PROBE_DELAY = 1.5

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


def _check_dockerd(invoker: MsbInvoker, config: MsbConfig, name: str) -> None:
    """Probe dockerd readiness or explain why docker is unavailable.

    When the sandbox has no usable docker storage backend (no docker disk
    and no flat root), dockerd is never started by the image entrypoint and
    ``docker`` commands in the guest fail with a socket error. That state
    is reported as a visible warning — including for already-running
    sandboxes, which skip the boot/start paths — together with the fix.
    Otherwise dockerd readiness is probed and a failure warns with
    diagnostics.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    """
    if not docker_enabled(config):
        logger.warning(
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
        logger.info("dockerd is ready in the guest.")
    else:
        logger.warning("%s", _dockerd_warning(name, last_error))


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
    # `msb modify`-style growth or manually).
    requested_docker_disk = config.sandbox.docker_disk
    if requested_docker_disk is not None:
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
        "sandbox. Remove and re-create it to apply (this deletes the docker "
        "data disk): msb-circus rm -f && msb-circus up (%s).",
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
    logger.warning(
        "git.worktree_mirror is enabled in the configuration, but sandbox %s "
        "was created without the workspace mirror mount. Mounts are fixed at "
        "sandbox creation, so the change is not applied to this sandbox. "
        "Remove and re-create it to apply: msb-circus rm -f && msb-circus up.",
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


def _trusted_guest_dirs(config: MsbConfig) -> list[str]:
    """Return the guest directories to pre-trust for the agent harnesses.

    The exec workdir plus every ``additional_dirs`` guest target, in first-
    occurrence order and without duplicates.

    :param config: Resolved msb-circus configuration.
    :returns: Guest directory paths.
    """
    dirs = [config.agent.workdir]
    dirs += [
        f"{ADDITIONAL_DIRS_GUEST_BASE}/{entry.name}" for entry in config.additional_dirs
    ]
    return list(dict.fromkeys(dirs))


def _ensure_pi_trust(invoker: MsbInvoker, config: MsbConfig, name: str) -> None:
    """Pre-trust the mounted work directories for pi and pi-acp.

    Writes ``$HOME/.pi/agent/trust.json`` in the guest so the first harness
    start in a fresh sandbox does not prompt for directory trust. The write
    is idempotent and overwrites an existing trust file. Failures are
    non-fatal: pi then simply asks on first start, as before.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    """
    try:
        result = invoker.run(
            build_write_trust_args(name, _trusted_guest_dirs(config)), check=False
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


def _ensure_claude_trust(invoker: MsbInvoker, config: MsbConfig, name: str) -> None:
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
    """
    try:
        result = invoker.run(
            build_write_claude_trust_args(name, _trusted_guest_dirs(config)),
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
            invoker.run(build_copy_args(name, str(host_file), guest))
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
            invoker.run(build_copy_args(name, host, guest))
            invoker.run(build_exec_args(name, ["chmod", "600", guest], tty=None))
        except MsbCommandError as e:
            logger.warning(
                "Could not copy the SSH file %s into sandbox %s; the guest "
                "keeps its previous copy (%s)",
                host,
                name,
                e,
            )


def _check_ssh_agent(invoker: MsbInvoker, config: MsbConfig, name: str) -> None:
    """Probe the forwarded SSH agent inside the guest; warn on problems.

    The probe is ``ssh-add -l``: exit code 0 lists keys, 1 means the agent
    has no identities, and 2 means it cannot connect (missing route or
    bridge). Never raises.

    :param invoker: Invoker bound to the resolved msb binary.
    :param config: Resolved msb-circus configuration.
    :param name: Sandbox name.
    """
    if config.ssh is None:
        return
    try:
        result = invoker.run(build_ssh_agent_probe_args(name), check=False)
    except MsbCommandError as e:
        logger.debug("SSH agent probe in %s failed: %s", name, e)
        return
    if result.returncode == 0:
        logger.debug("SSH agent reachable in %s", name)
    elif result.returncode == 1:
        logger.warning(
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


def _ensure_sandbox_running(
    invoker: MsbInvoker,
    config: MsbConfig,
    workspace: Path,
    *,
    allow_build: bool = False,
    engine: str = "podman",
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
    :raises typer.Exit: If the image is missing and building is not allowed.
    """
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
        _ensure_pi_trust(invoker, config, name)
        _ensure_agent_config_copies(invoker, name)
        _ensure_claude_trust(invoker, config, name)
        _repair_codex_daemon(invoker, name)
        _ensure_ssh_files(invoker, config, name)
        _check_ssh_agent(invoker, config, name)
        _warn_on_agent_config_mount_drift(invoker, name)
        _warn_on_port_drift(invoker, config, name)
        _warn_on_disk_drift(invoker, config, name)
        _warn_on_mount_drift(invoker, config, name, workspace)
        _check_dockerd(invoker, config, name)
        return

    if existing is not None:
        logger.info("Starting existing sandbox %s...", name)
        _fail_on_occupied_ports(config)
        invoker.run(build_start_args(name))
        _ensure_pi_trust(invoker, config, name)
        _ensure_agent_config_copies(invoker, name)
        _ensure_claude_trust(invoker, config, name)
        _repair_codex_daemon(invoker, name)
        _ensure_ssh_files(invoker, config, name)
        _check_ssh_agent(invoker, config, name)
        _warn_on_agent_config_mount_drift(invoker, name)
        _warn_on_port_drift(invoker, config, name)
        _warn_on_disk_drift(invoker, config, name)
        _warn_on_mount_drift(invoker, config, name, workspace)
    else:
        logger.info("Booting sandbox %s from %s...", name, image_tag)
        _fail_on_occupied_ports(config)
        invoker.run(
            build_run_args(
                name=name,
                image=image_tag,
                memory=config.sandbox.memory,
                cpus=config.sandbox.cpus,
                mounts=(
                    effective_mounts(config, workspace) + resolve_agent_config_mounts()
                ),
                net=config.network.mode,
                port_forwards=config.port_forwards,
                workdir=config.agent.workdir,
                docker_disk=config.sandbox.docker_disk,
                root_disk=config.sandbox.root_disk,
                env={**config.env, **(ssh_sandbox_env() if link else {})},
                vsock=[(str(link), SSH_AGENT_VSOCK_PORT)] if link else None,
            )
        )
        _ensure_pi_trust(invoker, config, name)
        _ensure_agent_config_copies(invoker, name)
        _ensure_claude_trust(invoker, config, name)
        _repair_codex_daemon(invoker, name)
        _ensure_ssh_files(invoker, config, name)
        _check_ssh_agent(invoker, config, name)

    _check_dockerd(invoker, config, name)


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
    config, invoker = _resolve_invoker(workspace)
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
    _ensure_ssh_files(invoker, config, name)
    _check_ssh_agent(invoker, config, name)
    _warn_on_disk_drift(invoker, config, name)
    _warn_on_mount_drift(invoker, config, name, workspace)
    if docker_enabled(config):
        ready, last_error = _probe_dockerd(invoker, name)
        if ready:
            typer.echo(f"✓ Started {name}; dockerd is ready.")
        else:
            typer.echo(_dockerd_warning(name, last_error), err=True)
    else:
        typer.echo(f"✓ Started {name} (docker is not enabled for this sandbox)")


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
            help="Guest working directory. Defaults to [msb] agent.workdir.",
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
    guest_workdir = workdir or config.agent.workdir

    try:
        env_map = {
            **resolve_passthrough(config.env_passthrough),
            **({"SSH_AUTH_SOCK": SSH_AGENT_GUEST_SOCKET} if config.ssh else {}),
            **parse_env_assignments(env or []),
        }
    except ConfigurationError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    _ensure_sandbox_running(invoker, config, workspace)

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


def run_cli() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    run_cli()
