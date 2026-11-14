"""Configuration loading and validation for msb-circus.

msb-circus reads the same ``.agent-circus/config.toml`` file that
agent-circus uses.  Only the keys this tool supports are interpreted
(:data:`SUPPORTED_TOP_LEVEL_KEYS`); every other top-level key is logged
as unsupported and ignored.  msb-circus-specific settings live under the
``[msb]`` table, which agent-circus in turn warns-and-ignores.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .exceptions import ConfigurationError

logger = logging.getLogger(__name__)

CONFIG_DIR_NAME = ".agent-circus"
CONFIG_FILE_NAME = "config.toml"

USER_CONFIG_DIR_ENV_VAR = "XDG_CONFIG_HOME"

#: Top-level config keys interpreted by msb-circus; everything else warns.
SUPPORTED_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "msb",
        "logging",
        "env",
        "env_passthrough",
        "additional_dirs",
        "port_forwards",
        "git",
        "ssh",
        "hosts",
    }
)

DEFAULT_IMAGE_TAG = "docker.io/library/msb-agents:26"
DEFAULT_MEMORY = "4G"
DEFAULT_INIT = "auto"
"""Default ``--init`` value: msb detects s6-overlay's ``/init``."""

INIT_NONE = "none"
"""``[msb.sandbox] init`` value that sends no ``--init`` flag."""

DEFAULT_STOP_TIMEOUT = 45
"""Seconds msb-circus waits for an s6 shutdown before forcing a stop."""

DEFAULT_DOCKER_STOP_TIMEOUT = 20
"""Seconds dockerd may take to stop containers at shutdown."""

DEFAULT_NETWORK_MODE = "public,private"
DEFAULT_AGENT_COMMAND: tuple[str, ...] = ("pi",)
DEFAULT_WORKDIR = "/workspace"
#: Default host bind address for published ports (msb's own default too).
DEFAULT_PORT_HOST = "127.0.0.1"
DEFAULT_SANDBOX_NAME_PREFIX = "agents"
DEFAULT_MOUNTS: dict[str, str] = {".": "/workspace"}
ADDITIONAL_DIRS_GUEST_BASE = "/workspaces"

#: Suffix of the persistent docker volume name (see
#: :func:`docker_volume_name_for_workspace`).
DOCKER_VOLUME_SUFFIX = "-docker"

#: vsock port the host SSH agent is exposed on (guest connects to CID 2).
SSH_AGENT_VSOCK_PORT = 22022
#: Guest path of the bridged SSH agent socket.
SSH_AGENT_GUEST_SOCKET = "/run/ssh-agent.sock"
#: Sandbox env var telling the image's s6 services to start the bridge.
#: Not prefixed with ``MSB_``: msb reserves that prefix for its own env
#: vars and rejects ``--env`` values using it.
SSH_AGENT_PORT_ENV_VAR = "CIRCUS_SSH_AGENT_VSOCK_PORT"
#: Guest directory for copied SSH config files (the guest runs as root).
SSH_GUEST_DIR = "/root/.ssh"

#: Sandbox env var read by the image's dockerd service (seconds at
#: shutdown). Not prefixed with ``MSB_``: msb reserves that prefix.
DOCKERD_SHUTDOWN_TIMEOUT_ENV_VAR = "CIRCUS_DOCKERD_SHUTDOWN_TIMEOUT"

#: Host file that ``[hosts]`` entries are read from.
DEFAULT_HOSTS_FILE = "/etc/hosts"

#: Read-write bind mounts of host agent harness config directories into the
#: guest, mirroring agent-circus's ``DEFAULT_AGENT_CONFIG_MOUNTS``. Values
#: are ``(directory below $HOME, guest path)``; the guest runs as root, so
#: the guest paths live under ``/root``. The mount carries the harness's
#: credentials file (``.credentials.json`` / ``auth.json``) so token
#: refreshes persist to the host, and is applied at sandbox creation when
#: the host directory exists (missing directories are skipped with a
#: warning). msb fixes mounts at creation, so changes require a re-create.
AGENT_CONFIG_DIR_MOUNTS: dict[str, tuple[str, str]] = {
    "claude-code": (".claude", "/root/.claude"),
    "codex": (".codex", "/root/.codex"),
}

#: Host agent harness config files copied into the guest (one-way) after
#: every boot or start. Entries are ``(harness, path below $HOME, guest
#: path)``. Missing host files are skipped silently; a failed copy warns.
#: Unlike :data:`AGENT_CONFIG_DIR_MOUNTS` these copies do not round-trip:
#: guest-side edits are lost on the next refresh.
AGENT_CONFIG_FILE_COPIES: tuple[tuple[str, str, str], ...] = (
    (
        "claude-code",
        ".claude.json",
        "/root/.claude.json",
    ),
    ("pi", ".pi/agent/settings.json", "/root/.pi/agent/settings.json"),
    ("pi", ".pi/agent/models.json", "/root/.pi/agent/models.json"),
    (
        "opencode",
        ".config/opencode/opencode.json",
        "/root/.config/opencode/opencode.json",
    ),
)

#: Read-only bind mounts of host user skill directories into the guest.
#: Entries are ``(directory below $HOME, guest path)``. Read-only keeps
#: agents in the sandbox from changing skills that also run on the host;
#: install user skills on the host, or project skills in the workspace.
#: Missing host directories are skipped (debug log only). msb fixes
#: mounts at creation, so a directory created later needs a re-create.
#: opencode 2.2.7 reads both ``skill`` and ``skills`` below its config
#: directory; it also reads ``~/.agents/skills`` and ``~/.claude/skills``.
AGENT_SKILL_DIR_MOUNTS: tuple[tuple[str, str], ...] = (
    (".agents/skills", "/root/.agents/skills"),
    (".pi/agent/skills", "/root/.pi/agent/skills"),
    (".config/opencode/skills", "/root/.config/opencode/skills"),
    (".config/opencode/skill", "/root/.config/opencode/skill"),
)


@dataclass(frozen=True)
class ImageConfig:
    """Image build/import settings (``[msb.image]``).

    :ivar tag: Full image reference to build and import.
    :ivar dockerfile: Dockerfile path relative to the workspace; ``None``
        selects the bundled template.
    :ivar platform: Target platform (e.g. ``linux/amd64``); ``None`` lets
        the host builder pick the native architecture.
    """

    tag: str = DEFAULT_IMAGE_TAG
    dockerfile: str | None = None
    platform: str | None = None


@dataclass(frozen=True)
class SandboxConfig:
    """Sandbox resource settings (``[msb.sandbox]``).

    The sandbox name itself is derived from the workspace directory, not
    configured: ``agents-<sanitized project name>``.

    :ivar memory: Memory limit string passed to ``msb`` verbatim.
    :ivar cpus: Optional CPU count.
    :ivar root_disk: Optional ``--root-disk`` spec (e.g. ``"flat:32G"``);
        ``None`` keeps msb's managed default root.
    :ivar docker_disk: Optional size of a dedicated ext4 disk mounted at
        ``/var/lib/docker`` (the docker opt-in); ``None`` disables docker.
    :ivar docker_disk_persist: Keep the docker data disk in a named msb
        volume that survives sandbox removal; requires ``docker_disk``.
    :ivar init: Value for ``--init`` at sandbox creation (``"auto"``,
        ``"none"`` sends no flag, or an absolute path to another init);
        applies at creation only.
    :ivar stop_timeout: Host-side seconds msb-circus waits for an s6
        shutdown before forcing the stop.
    :ivar docker_stop_timeout: Seconds dockerd may take to stop
        containers at shutdown; sent as sandbox env at creation.
    """

    memory: str = DEFAULT_MEMORY
    cpus: int | None = None
    root_disk: str | None = None
    docker_disk: str | None = None
    docker_disk_persist: bool = False
    init: str = DEFAULT_INIT
    stop_timeout: int = DEFAULT_STOP_TIMEOUT
    docker_stop_timeout: int = DEFAULT_DOCKER_STOP_TIMEOUT


def docker_enabled(config: MsbConfig) -> bool:
    """Return whether the sandbox provides a usable docker storage backend.

    Docker 29 needs a real filesystem under ``/var/lib/docker``. That is
    the case when a dedicated docker disk is requested, or when the root
    disk is a flat (non-overlayfs) root.

    :param config: Resolved msb-circus configuration.
    :returns: ``True`` when dockerd can create containers in the sandbox.
    """
    if config.sandbox.docker_disk is not None:
        return True
    root_disk = config.sandbox.root_disk
    return root_disk is not None and root_disk.startswith("flat:")


@dataclass(frozen=True)
class NetworkConfig:
    """Network profile settings (``[msb.network]``).

    :ivar mode: Network profile passed as ``--net``.
    :ivar trust_host_cas: Ship the host's trusted CAs into the guest
        (``--trust-host-cas``); applied at sandbox creation only.
    """

    mode: str = DEFAULT_NETWORK_MODE
    trust_host_cas: bool = False


@dataclass(frozen=True)
class AgentConfig:
    """Default exec target (``[msb.agent]``).

    :ivar workdir: Explicit guest working directory, or None when unset
        (see resolve_agent_workdir).
    """

    command: tuple[str, ...] = DEFAULT_AGENT_COMMAND
    workdir: str | None = None


@dataclass(frozen=True)
class TracingConfig:
    """Trace settings for agent executions (``[msb.tracing]``).

    :ivar enabled: Enable tracing for every ``exec`` command.
    :ivar directory: Host trace root. Relative paths use the workspace as base.
    :ivar sample_interval: Interval in seconds for resource samples.
    :ivar repetition_threshold: Number of equal bytes that creates an event.
    :ivar capture_payloads: Save raw ACP streams when ``True``.
    :ivar max_payload_bytes: Maximum bytes saved for each raw ACP stream.
    """

    enabled: bool = False
    directory: str = ".agent-circus/traces"
    sample_interval: float = 1.0
    repetition_threshold: int = 64
    capture_payloads: bool = False
    max_payload_bytes: int = 100 * 1024 * 1024


@dataclass(frozen=True)
class PortForward:
    """An entry of the ``[[port_forwards]]`` array.

    Publishes a guest port on the host. The field names follow
    agent-circus (``container_port``) so the same table works in both
    tools; ``guest_port`` is accepted as an msb-native alias. The
    ``service`` field of agent-circus entries is accepted but ignored:
    msb-circus runs one sandbox per workspace, so there is nothing to
    select a service from.

    :ivar guest_port: Port inside the sandbox.
    :ivar host_port: Port on the host; defaults to ``guest_port``.
    :ivar host: Host bind address; defaults to ``127.0.0.1`` (msb's own
        default, keeping published ports off the network by default).
    :ivar protocol: ``"tcp"`` or ``"udp"``.
    """

    guest_port: int
    host_port: int
    host: str = DEFAULT_PORT_HOST
    protocol: str = "tcp"


#: Allowed daemon names: lowercase letters, digits and "-", starting with
#: a letter or digit, at most 40 characters. The name is used in guest
#: paths, so it must stay filesystem- and shell-safe.
DAEMON_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")

#: Valid environment variable names (POSIX shell identifiers).
_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class DaemonConfig:
    """A long-running process supervised in the sandbox (``[[msb.daemons]]``).

    :ivar name: Unique daemon name (see :data:`DAEMON_NAME_PATTERN`).
    :ivar command: Command and arguments, run without a shell.
    :ivar autostart: Start the daemon on every sandbox boot.
    :ivar port: Optional guest TCP port the daemon listens on; used to
        wait for readiness and to show a URL. It does not forward a port.
    :ivar workdir: Optional guest working directory.
    :ivar env: Extra environment variables for the daemon.
    :ivar env_passthrough: Host environment variables passed to the
        daemon (same matching rules as the top-level ``env_passthrough``).
    """

    name: str
    command: tuple[str, ...]
    autostart: bool = True
    port: int | None = None
    workdir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    env_passthrough: tuple[str, ...] = ()


#: Hook names in run order.
HOOK_NAMES: tuple[str, ...] = ("provision", "startup")

#: Largest accepted hook script in bytes (UTF-8). The script travels as one
#: ``msb exec`` argument, and Linux limits one argument to 128 KiB.
MAX_HOOK_SCRIPT_BYTES = 64 * 1024

#: Default of ``required`` per hook name.
HOOK_REQUIRED_DEFAULTS: dict[str, bool] = {"provision": True, "startup": False}


@dataclass(frozen=True)
class HookConfig:
    """A project hook (``[msb.hooks]`` ``provision`` or ``startup``).

    :ivar name: ``provision`` or ``startup``.
    :ivar script: Inline script, or ``None`` when *file* is set.
    :ivar file: Host path of the script file, or ``None`` when *script* is set.
        Relative paths are resolved against the workspace root.
    :ivar required: Fail the command when the hook fails.
    :ivar timeout: Guest timeout in seconds, or ``None`` for no timeout.
    :ivar workdir: Guest working directory, or ``None`` for the agent workdir.
    :ivar env: Extra environment variables for the hook.
    :ivar env_passthrough: Extra host variables for the hook (same matching
        rules as the top-level ``env_passthrough``).
    """

    name: str
    script: str | None = None
    file: str | None = None
    required: bool = False
    timeout: int | None = None
    workdir: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    env_passthrough: tuple[str, ...] = ()


@dataclass(frozen=True)
class HooksConfig:
    """Project hooks (``[msb.hooks]``).

    :ivar provision: Hook run once per sandbox and when its script changes.
    :ivar startup: Hook run once per guest boot.
    """

    provision: HookConfig | None = None
    startup: HookConfig | None = None


@dataclass(frozen=True)
class AdditionalDir:
    """An entry of the ``[[additional_dirs]]`` array.

    Mirrors the agent-circus semantics: an extra host directory mounted
    into the guest under ``/workspaces/<name>``.

    :ivar path: Absolute path on the host.
    :ivar name: Guest mount name (``/workspaces/<name>``).
    :ivar readonly: Mount read-only when ``True``.
    """

    path: str
    name: str
    readonly: bool = False


@dataclass(frozen=True)
class SshConfig:
    """SSH agent forwarding settings (``[ssh]``, agent-circus compatible).

    :ivar config_path: Absolute host path of an SSH config file copied
        to ``/root/.ssh/config``, or ``None``.
    :ivar known_hosts_path: Absolute host path of a known_hosts file
        copied to ``/root/.ssh/known_hosts``, or ``None``.
    """

    config_path: str | None = None
    known_hosts_path: str | None = None


@dataclass(frozen=True)
class GitConfig:
    """Git settings (``[git]``, agent-circus compatible).

    :ivar worktree_mirror: Mount the workspace a second time at its
        host absolute path.
    :ivar config_path: Explicit absolute host path of the gitconfig to
        forward, or ``None`` to search the default locations.
    :ivar signing_key_path: Absolute host path of an SSH public key used
        for commit signing, or ``None``.
    """

    worktree_mirror: bool = False
    config_path: str | None = None
    signing_key_path: str | None = None


@dataclass(frozen=True)
class HostsConfig:
    """Host entry forwarding settings (``[hosts]``, agent-circus compatible).

    Selected lines of the host's hosts file are written into the guest's
    ``/etc/hosts`` after every boot, start, and reuse, so matching names
    resolve inside the sandbox without a DNS change.

    :ivar patterns: fnmatch globs or ``re:``-prefixed regexes matched
        case-insensitively against host names. When any name of a hosts
        line matches, all names of that line are forwarded.
    :ivar file: Host path of the hosts file to read.
    """

    patterns: tuple[str, ...] = ()
    file: str = DEFAULT_HOSTS_FILE


@dataclass(frozen=True)
class MsbConfig:
    """Resolved msb-circus configuration.

    :ivar binary: Optional explicit path to the ``msb`` binary.
    :ivar image: Image build/import settings.
    :ivar sandbox: Sandbox resource settings.
    :ivar network: Network profile settings.
    :ivar agent: Default exec target.
    :ivar tracing: Trace settings for agent executions.
    :ivar mounts: Host path -> guest path mapping (options may be appended
        to the guest path, e.g. ``"/workspace:ro"``). Defaults to mounting
        the workspace at ``/workspace`` per the documented workflow.
    :ivar additional_dirs: Extra host directories mounted under
        ``/workspaces/<name>`` (agent-circus compatible).
    :ivar port_forwards: Guest ports published on the host
        (``[[port_forwards]]``, agent-circus compatible).
    :ivar daemons: Long-running processes supervised in the sandbox
        (``[[msb.daemons]]``).
    :ivar hooks: Project hooks (``[msb.hooks]``).
    :ivar env: Non-secret variables from ``[env]`` and ``[msb.env]``; set at
        sandbox creation and passed with every exec, hook and daemon.
    :ivar env_passthrough: Host environment variables to pass into the
        sandbox. Literal names are required to exist on the host; entries
        are otherwise treated as glob or ``re:``-prefixed regex patterns
        (like agent-circus's ``filter_env``).
    :ivar git_worktree_mirror: Mount the workspace a second time at its
        exact host absolute path (``[git] worktree_mirror``) so Git's
        recorded paths stay valid across the sandbox boundary.
    :ivar git: Git settings; ``None`` when the ``[git]`` table is absent.
    :ivar ssh: SSH agent forwarding settings; ``None`` disables forwarding.
    :ivar hosts: Host entry forwarding; ``None`` disables it.
    :ivar logging_level: Log level for msb-circus's own logging.
    :ivar logging_file: Optional log file path.
    """

    binary: str | None = None
    image: ImageConfig = field(default_factory=ImageConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    tracing: TracingConfig = field(default_factory=TracingConfig)
    mounts: dict[str, str] = field(default_factory=dict)
    additional_dirs: tuple[AdditionalDir, ...] = ()
    port_forwards: tuple[PortForward, ...] = ()
    daemons: tuple[DaemonConfig, ...] = ()
    hooks: HooksConfig = field(default_factory=HooksConfig)
    env: dict[str, str] = field(default_factory=dict)
    env_passthrough: tuple[str, ...] = ()
    git_worktree_mirror: bool = False
    git: GitConfig | None = None
    ssh: SshConfig | None = None
    hosts: HostsConfig | None = None
    logging_level: str = "INFO"
    logging_file: str | None = None


def get_project_config_path(workspace: Path) -> Path:
    """Return the project-local config file path.

    :param workspace: Workspace directory.
    :returns: Path to ``<workspace>/.agent-circus/config.toml``.
    """
    return workspace / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def get_user_config_path(environ: dict[str, str] | None = None) -> Path:
    """Return the user-global config file path.

    Mirrors agent-circus: ``$XDG_CONFIG_HOME/agent-circus/config.toml``,
    defaulting to ``~/.config/agent-circus/config.toml``.

    :param environ: Environment mapping, defaults to :data:`os.environ`.
    :returns: Path to the user-global config file.
    """
    env = os.environ if environ is None else environ
    xdg = Path(env.get(USER_CONFIG_DIR_ENV_VAR, Path.home() / ".config"))
    return xdg / "agent-circus" / CONFIG_FILE_NAME


def _load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file into a dictionary.

    :param path: File to read.
    :returns: Parsed TOML document.
    :raises ConfigurationError: If the file cannot be read or parsed.
    """
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except OSError as e:
        raise ConfigurationError(f"Cannot read config file {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigurationError(f"Invalid TOML in {path}: {e}") from e


def read_config_layers(
    workspace: Path, environ: dict[str, str] | None = None
) -> dict[str, Any]:
    """Read and shallow-merge the config layers (project-local wins).

    :param workspace: Workspace directory.
    :param environ: Environment mapping for the XDG lookup.
    :returns: Merged raw configuration dictionary.
    :raises ConfigurationError: If a config file is invalid.
    """
    merged: dict[str, Any] = {}
    for path in (get_user_config_path(environ), get_project_config_path(workspace)):
        if path.is_file():
            layer = _load_toml(path)
            merged.update(layer)
    return merged


def warn_unsupported_keys(config: dict[str, Any]) -> None:
    """Log a debug message for every unsupported top-level config key.

    Agent-circus and msb-circus share a config file, so unsupported keys
    are expected and not worth a warning on every invocation. The
    ``ca_certs`` key is logged at INFO level instead, because users may
    expect it to work here as it does in agent-circus.

    :param config: Merged raw configuration dictionary.
    """
    for key in config:
        if key == "ca_certs":
            logger.info(
                "msb-circus does not support [ca_certs]; ignoring it. Add the "
                "certificates to the host CA store and set "
                "msb.network.trust_host_cas = true instead."
            )
            continue
        if key not in SUPPORTED_TOP_LEVEL_KEYS:
            logger.debug("Unsupported config key %r for msb-circus — ignoring", key)


def sanitize_project_name(name: str) -> str:
    """Sanitize a project name for use in sandbox names.

    Lowercases, replaces runs of non-alphanumeric characters with a single
    hyphen, and strips leading/trailing hyphens.

    :param name: Raw project name (usually the workspace directory name).
    :returns: Sanitized name; empty string if nothing remains.
    """
    sanitized = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return sanitized


def resolve_mounts(mounts: dict[str, str], workspace: Path) -> dict[str, str]:
    """Resolve relative host paths in mount specifications.

    A host path of ``.`` is replaced with the absolute workspace path so
    the mount is independent of the CLI's working directory.

    :param mounts: Raw mount mapping from configuration.
    :param workspace: Workspace directory.
    :returns: Mount mapping with resolved host paths.
    """
    resolved: dict[str, str] = {}
    for host, guest in mounts.items():
        resolved[str(workspace) if host == "." else host] = guest
    return resolved


def _parse_additional_dirs(raw: Any) -> tuple[AdditionalDir, ...]:
    """Parse the ``[[additional_dirs]]`` array.

    Mirrors the agent-circus fields: ``path`` (required, absolute),
    ``readonly`` (default ``False``), and ``name`` (default: the basename
    of *path*). Names and paths must be unique across entries.

    :param raw: Raw value or ``None``.
    :returns: Tuple of parsed entries.
    :raises ConfigurationError: If the value is malformed or ambiguous.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigurationError(
            "additional_dirs must be an array of tables ([[additional_dirs]])"
        )
    dirs: list[AdditionalDir] = []
    names: set[str] = set()
    paths: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigurationError(f"additional_dirs[{index}] must be a table")
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            raise ConfigurationError(
                f"additional_dirs[{index}].path must be a non-empty string"
            )
        if not os.path.isabs(path):
            raise ConfigurationError(
                f"additional_dirs[{index}].path must be an absolute path"
            )
        readonly = entry.get("readonly", False)
        if not isinstance(readonly, bool):
            raise ConfigurationError(
                f"additional_dirs[{index}].readonly must be a boolean"
            )
        name = entry.get("name", os.path.basename(os.path.normpath(path)))
        if not isinstance(name, str) or not name:
            raise ConfigurationError(
                f"additional_dirs[{index}].name must be a non-empty string"
            )
        if name in names:
            raise ConfigurationError(
                f"additional_dirs[{index}].name {name!r} duplicates an earlier entry"
            )
        if path in paths:
            raise ConfigurationError(
                f"additional_dirs[{index}].path {path!r} duplicates an earlier entry"
            )
        names.add(name)
        paths.add(path)
        dirs.append(AdditionalDir(path=path, name=name, readonly=readonly))
    return tuple(dirs)


def effective_mounts(config: MsbConfig, workspace: Path) -> list[tuple[str, str]]:
    """Combine ``[msb.mounts]``, the git worktree mirror, and
    ``additional_dirs`` into msb volumes.

    Resolves the ``.`` workspace mount to the absolute workspace path,
    optionally adds the git worktree mirror (the workspace mounted a
    second time at its exact host absolute path), and appends every
    additional directory as ``/workspaces/<name>``, read-only when the
    entry says so.

    :param config: Resolved msb-circus configuration.
    :param workspace: Workspace directory.
    :returns: ``(host path, guest mount specification)`` pairs. A list
        because the same host path may appear at several guest targets.
    :raises ConfigurationError: If a guest target collides with another.
    """
    resolved = resolve_mounts(config.mounts, workspace)
    mounts: list[tuple[str, str]] = list(resolved.items())
    taken = {guest.split(":", 1)[0] for guest in resolved.values()}
    if config.git_worktree_mirror:
        host_path = str(workspace)
        if host_path in taken:
            raise ConfigurationError(
                f"git.worktree_mirror cannot mount the workspace at {host_path!r}: "
                "the path is already used as a mount target"
            )
        mounts.append((host_path, host_path))
        taken.add(host_path)
    for entry in config.additional_dirs:
        guest = f"{ADDITIONAL_DIRS_GUEST_BASE}/{entry.name}"
        if guest in taken:
            raise ConfigurationError(
                f"additional_dirs entry {entry.name!r} conflicts with the "
                f"existing mount target {guest}"
            )
        taken.add(guest)
        mounts.append((entry.path, f"{guest}:ro" if entry.readonly else guest))
    return mounts


def resolve_agent_workdir(config: MsbConfig, workspace: Path) -> str:
    """Return the guest working directory for agent commands.

    An explicit ``[msb.agent] workdir`` wins. Otherwise the host path of
    the workspace is used when the git worktree mirror is on, so Git
    records paths that are valid on the host, and ``/workspace`` when
    it is off.

    :param config: Resolved msb-circus configuration.
    :param workspace: Workspace directory (absolute host path).
    :returns: Guest working directory.
    """
    if config.agent.workdir is not None:
        return config.agent.workdir
    if config.git_worktree_mirror:
        return str(workspace)
    return DEFAULT_WORKDIR


def resolve_agent_config_mounts(
    environ: dict[str, str] | None = None,
    *,
    warn_missing: bool = True,
) -> list[tuple[str, str]]:
    """Return bind mounts for existing agent harness config directories.

    Iterates :data:`AGENT_CONFIG_DIR_MOUNTS`, keeps every entry whose host
    directory (``$HOME/<dir>``) exists, and logs a warning for each missing
    one so silent skips stay visible. Guest paths assume the msb-circus
    image's root user.

    :param environ: Environment mapping for the ``$HOME`` lookup, defaults
        to :data:`os.environ`.
    :param warn_missing: Log a warning for each missing directory; callers
        that only need the mount list pass ``False``.
    :returns: ``(host directory, guest path)`` pairs in declaration order.
    """
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME", str(Path.home())))
    mounts: list[tuple[str, str]] = []
    for harness, (below_home, guest) in AGENT_CONFIG_DIR_MOUNTS.items():
        host_dir = home / below_home
        if not host_dir.is_dir():
            if warn_missing:
                logger.warning(
                    "Host config directory %s for %s not found; skipping its "
                    "sandbox mount (the harness starts without host config and "
                    "credentials). Log in inside the guest or create the "
                    "directory, then re-create the sandbox: msb-circus rm -f "
                    "&& msb-circus up.",
                    host_dir,
                    harness,
                )
            continue
        mounts.append((str(host_dir), guest))
    return mounts


def resolve_agent_skill_mounts(
    environ: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Return read-only bind mounts for existing user skill directories.

    Iterates :data:`AGENT_SKILL_DIR_MOUNTS`, keeps every entry whose host
    directory (``$HOME/<dir>``) exists, and appends ``:ro`` to the guest
    path so agents in the sandbox cannot change skills that also run on
    the host. A missing directory is normal and only logged at debug
    level. Guest paths assume the msb-circus image's root user.

    :param environ: Environment mapping for the ``$HOME`` lookup, defaults
        to :data:`os.environ`.
    :returns: ``(host directory, guest path)`` pairs in declaration order;
        each guest path carries the ``:ro`` read-only option.
    """
    env = os.environ if environ is None else environ
    home = Path(env.get("HOME", str(Path.home())))
    mounts: list[tuple[str, str]] = []
    for below_home, guest in AGENT_SKILL_DIR_MOUNTS:
        host_dir = home / below_home
        if not host_dir.is_dir():
            logger.debug(
                "Host skill directory %s not found; skipping its sandbox mount.",
                host_dir,
            )
            continue
        mounts.append((str(host_dir), f"{guest}:ro"))
    return mounts


def sandbox_name_for_workspace(workspace: Path) -> str:
    """Derive the sandbox name for a workspace.

    :param workspace: Workspace directory.
    :returns: ``agents-<sanitized project name>``.
    :raises ConfigurationError: If the sanitized name is empty.
    """
    project = sanitize_project_name(workspace.name)
    if not project:
        raise ConfigurationError(
            f"Cannot derive a sandbox name from workspace {str(workspace)!r}"
        )
    return f"{DEFAULT_SANDBOX_NAME_PREFIX}-{project}"


def docker_volume_name_for_workspace(workspace: Path) -> str:
    """Derive the persistent docker volume name for a workspace.

    :param workspace: Workspace directory.
    :returns: ``agents-<sanitized project name>-docker``.
    :raises ConfigurationError: If the sanitized name is empty.
    """
    return f"{sandbox_name_for_workspace(workspace)}{DOCKER_VOLUME_SUFFIX}"


def _parse_image(raw: Any) -> ImageConfig:
    """Parse the ``[msb.image]`` table.

    :param raw: Raw table value or ``None``.
    :returns: Parsed image config.
    :raises ConfigurationError: If the table is malformed.
    """
    if raw is None:
        return ImageConfig()
    if not isinstance(raw, dict):
        raise ConfigurationError("msb.image must be a table")
    tag = raw.get("tag", DEFAULT_IMAGE_TAG)
    if not isinstance(tag, str) or not tag:
        raise ConfigurationError("msb.image.tag must be a non-empty string")
    dockerfile = raw.get("dockerfile")
    if dockerfile is not None and not isinstance(dockerfile, str):
        raise ConfigurationError("msb.image.dockerfile must be a string")
    platform = raw.get("platform")
    if platform is not None and not isinstance(platform, str):
        raise ConfigurationError("msb.image.platform must be a string")
    return ImageConfig(tag=tag, dockerfile=dockerfile, platform=platform)


def _parse_sandbox(raw: Any) -> SandboxConfig:
    """Parse the ``[msb.sandbox]`` table.

    :param raw: Raw table value or ``None``.
    :returns: Parsed sandbox config.
    :raises ConfigurationError: If the table is malformed.
    """
    if raw is None:
        return SandboxConfig()
    if not isinstance(raw, dict):
        raise ConfigurationError("msb.sandbox must be a table")
    memory = raw.get("memory", DEFAULT_MEMORY)
    if not isinstance(memory, str) or not memory:
        raise ConfigurationError("msb.sandbox.memory must be a non-empty string")
    cpus = raw.get("cpus")
    if cpus is not None and not isinstance(cpus, int):
        raise ConfigurationError("msb.sandbox.cpus must be an integer")

    # Size/spec strings are passed to msb verbatim: msb is the authority on
    # the grammar and validates it at sandbox creation.
    root_disk = raw.get("root_disk")
    if root_disk is not None and (not isinstance(root_disk, str) or not root_disk):
        raise ConfigurationError(
            'msb.sandbox.root_disk must be a non-empty string (e.g. "flat:32G")'
        )
    docker_disk = raw.get("docker_disk")
    if docker_disk is not None and (
        not isinstance(docker_disk, str) or not docker_disk
    ):
        raise ConfigurationError(
            'msb.sandbox.docker_disk must be a non-empty string (e.g. "10G")'
        )
    docker_disk_persist = raw.get("docker_disk_persist", False)
    if not isinstance(docker_disk_persist, bool):
        raise ConfigurationError("msb.sandbox.docker_disk_persist must be a boolean")
    if docker_disk_persist and docker_disk is None:
        raise ConfigurationError(
            "msb.sandbox.docker_disk_persist requires msb.sandbox.docker_disk "
            '(e.g. "20G")'
        )
    init = raw.get("init", DEFAULT_INIT)
    if (
        not isinstance(init, str)
        or not init
        or not (init in ("auto", "none") or init.startswith("/"))
    ):
        raise ConfigurationError(
            'msb.sandbox.init must be "auto", "none" or an absolute path'
        )

    timeouts: dict[str, int] = {}
    for key in ("stop_timeout", "docker_stop_timeout"):
        value = raw.get(
            key,
            DEFAULT_STOP_TIMEOUT
            if key == "stop_timeout"
            else DEFAULT_DOCKER_STOP_TIMEOUT,
        )
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigurationError(
                f"msb.sandbox.{key} must be a positive integer (seconds)"
            )
        timeouts[key] = value
    if timeouts["stop_timeout"] <= timeouts["docker_stop_timeout"]:
        raise ConfigurationError(
            "msb.sandbox.stop_timeout must be greater than "
            "msb.sandbox.docker_stop_timeout"
        )
    return SandboxConfig(
        memory=memory,
        cpus=cpus,
        root_disk=root_disk,
        docker_disk=docker_disk,
        docker_disk_persist=docker_disk_persist,
        init=init,
        stop_timeout=timeouts["stop_timeout"],
        docker_stop_timeout=timeouts["docker_stop_timeout"],
    )


def _parse_network(raw: Any) -> NetworkConfig:
    """Parse the ``[msb.network]`` table.

    :param raw: Raw table value or ``None``.
    :returns: Parsed network config.
    :raises ConfigurationError: If the table is malformed.
    """
    if raw is None:
        return NetworkConfig()
    if not isinstance(raw, dict):
        raise ConfigurationError("msb.network must be a table")
    mode = raw.get("mode", DEFAULT_NETWORK_MODE)
    if not isinstance(mode, str) or not mode:
        raise ConfigurationError("msb.network.mode must be a non-empty string")
    trust_host_cas = raw.get("trust_host_cas", False)
    if not isinstance(trust_host_cas, bool):
        raise ConfigurationError("msb.network.trust_host_cas must be a boolean")
    return NetworkConfig(mode=mode, trust_host_cas=trust_host_cas)


def _parse_tracing(raw: Any) -> TracingConfig:
    """Parse the ``[msb.tracing]`` table.

    :param raw: Raw table value or ``None``.
    :returns: Parsed trace settings.
    :raises ConfigurationError: If the table is malformed.
    """
    if raw is None:
        return TracingConfig()
    if not isinstance(raw, dict):
        raise ConfigurationError("msb.tracing must be a table")

    enabled = raw.get("enabled", False)
    directory = raw.get("directory", ".agent-circus/traces")
    sample_interval = raw.get("sample_interval", 1.0)
    repetition_threshold = raw.get("repetition_threshold", 64)
    capture_payloads = raw.get("capture_payloads", False)
    max_payload_bytes = raw.get("max_payload_bytes", 100 * 1024 * 1024)

    if not isinstance(enabled, bool):
        raise ConfigurationError("msb.tracing.enabled must be a boolean")
    if not isinstance(directory, str) or not directory:
        raise ConfigurationError("msb.tracing.directory must be a non-empty string")
    if (
        isinstance(sample_interval, bool)
        or not isinstance(sample_interval, int | float)
        or sample_interval <= 0
    ):
        raise ConfigurationError(
            "msb.tracing.sample_interval must be greater than zero"
        )
    if (
        isinstance(repetition_threshold, bool)
        or not isinstance(repetition_threshold, int)
        or repetition_threshold < 2
    ):
        raise ConfigurationError(
            "msb.tracing.repetition_threshold must be an integer of at least 2"
        )
    if not isinstance(capture_payloads, bool):
        raise ConfigurationError("msb.tracing.capture_payloads must be a boolean")
    if (
        isinstance(max_payload_bytes, bool)
        or not isinstance(max_payload_bytes, int)
        or max_payload_bytes < 1
    ):
        raise ConfigurationError(
            "msb.tracing.max_payload_bytes must be a positive integer"
        )
    return TracingConfig(
        enabled=enabled,
        directory=directory,
        sample_interval=float(sample_interval),
        repetition_threshold=repetition_threshold,
        capture_payloads=capture_payloads,
        max_payload_bytes=max_payload_bytes,
    )


def _validate_port(value: Any, label: str) -> int:
    """Validate a port number.

    :param value: Raw value from the configuration.
    :param label: Field name used in the error message.
    :returns: The validated port number.
    :raises ConfigurationError: If the value is not an integer in
        ``1..65535``.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{label} must be an integer")
    if not 1 <= value <= 65535:
        raise ConfigurationError(f"{label} must be in range 1..65535")
    return value


def _parse_port_forwards(raw: Any) -> tuple[PortForward, ...]:
    """Parse the ``[[port_forwards]]`` array.

    Accepts agent-circus entries (``container_port`` plus an ignored
    ``service``) and msb-native entries (``guest_port``). ``host``,
    ``host_port``, and ``protocol`` are optional.

    :param raw: Raw array value or ``None``.
    :returns: Tuple of validated port forwards.
    :raises ConfigurationError: If an entry is malformed.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigurationError("port_forwards must be a list of tables")
    forwards: list[PortForward] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ConfigurationError(f"port_forwards[{index}] must be a table")
        guest_port = entry.get("guest_port", entry.get("container_port"))
        if guest_port is None:
            raise ConfigurationError(
                f"port_forwards[{index}] needs a guest_port (or container_port)"
            )
        guest_port = _validate_port(guest_port, f"port_forwards[{index}] guest_port")
        host_port = entry.get("host_port", guest_port)
        host_port = _validate_port(host_port, f"port_forwards[{index}] host_port")
        host = entry.get("host", DEFAULT_PORT_HOST)
        if not isinstance(host, str) or not host:
            raise ConfigurationError(
                f"port_forwards[{index}] host must be a non-empty string"
            )
        protocol = entry.get("protocol", "tcp")
        if protocol not in ("tcp", "udp"):
            raise ConfigurationError(
                f"port_forwards[{index}] protocol must be 'tcp' or 'udp'"
            )
        forwards.append(
            PortForward(
                guest_port=guest_port,
                host_port=host_port,
                host=host,
                protocol=protocol,
            )
        )
    return tuple(forwards)


def _parse_daemons(raw: Any) -> tuple[DaemonConfig, ...]:
    """Parse the ``[[msb.daemons]]`` array.

    Each entry needs a unique ``name`` matching
    :data:`DAEMON_NAME_PATTERN` and a non-empty ``command`` list. All
    other fields are optional.

    :param raw: Raw array value or ``None``.
    :returns: Tuple of parsed daemon configs in configuration order.
    :raises ConfigurationError: If an entry is malformed or a name is
        used twice.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigurationError("msb.daemons must be a list of tables")
    daemons: list[DaemonConfig] = []
    names: set[str] = set()
    for index, entry in enumerate(raw):
        label = f"msb.daemons[{index}]"
        if not isinstance(entry, dict):
            raise ConfigurationError(f"{label} must be a table")
        known_keys = (
            "name",
            "command",
            "autostart",
            "port",
            "workdir",
            "env",
            "env_passthrough",
        )
        unknown = sorted(key for key in entry if key not in known_keys)
        if unknown:
            raise ConfigurationError(
                f"{label} has unknown key(s): {', '.join(unknown)}"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not DAEMON_NAME_PATTERN.fullmatch(name):
            raise ConfigurationError(
                f"{label}.name must match [a-z0-9][a-z0-9-]* (at most 40 characters)"
            )
        if name in names:
            raise ConfigurationError(f"{label}.name {name!r} is used more than once")
        names.add(name)
        command = entry.get("command")
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise ConfigurationError(
                f"{label}.command must be a non-empty list of strings"
            )
        autostart = entry.get("autostart", True)
        if not isinstance(autostart, bool):
            raise ConfigurationError(f"{label}.autostart must be true or false")
        port = entry.get("port")
        if port is not None:
            port = _validate_port(port, f"{label}.port")
        workdir = entry.get("workdir")
        if workdir is not None and (not isinstance(workdir, str) or not workdir):
            raise ConfigurationError(f"{label}.workdir must be a non-empty string")
        env = _parse_string_dict(entry.get("env"), f"{label}.env")
        for key in env:
            if not _ENV_NAME_PATTERN.fullmatch(key):
                raise ConfigurationError(
                    f"{label}.env key {key!r} is not a valid environment variable name"
                )
        env_passthrough = entry.get("env_passthrough")
        if env_passthrough is None:
            env_passthrough = ()
        elif not isinstance(env_passthrough, list) or not all(
            isinstance(item, str) and item for item in env_passthrough
        ):
            raise ConfigurationError(
                f"{label}.env_passthrough must be a list of non-empty strings"
            )
        daemons.append(
            DaemonConfig(
                name=name,
                command=tuple(command),
                autostart=autostart,
                port=port,
                workdir=workdir,
                env=env,
                env_passthrough=tuple(env_passthrough),
            )
        )
    return tuple(daemons)


def _parse_hooks(raw: Any) -> HooksConfig:
    """Parse the ``[msb.hooks]`` table.

    Each hook accepts either a plain string (an inline script) or a table
    with one of ``script`` / ``file`` plus optional settings. Unknown hook
    names and unknown keys are rejected.

    :param raw: Raw table value or ``None``.
    :returns: Parsed hooks configuration.
    :raises ConfigurationError: If the table or a hook entry is malformed.
    """
    if raw is None:
        return HooksConfig()
    if not isinstance(raw, dict):
        raise ConfigurationError("msb.hooks must be a table")
    unknown = sorted(key for key in raw if key not in HOOK_NAMES)
    if unknown:
        raise ConfigurationError(f"msb.hooks has unknown key(s): {', '.join(unknown)}")
    hooks: dict[str, HookConfig | None] = {}
    for name in HOOK_NAMES:
        if name not in raw:
            hooks[name] = None
            continue
        label = f"msb.hooks.{name}"
        value = raw[name]
        if isinstance(value, str):
            if not value.strip():
                raise ConfigurationError(f"{label} must not be empty")
            hook = HookConfig(
                name=name, script=value, required=HOOK_REQUIRED_DEFAULTS[name]
            )
        elif isinstance(value, dict):
            hook = _parse_hook_table(name, label, value)
        else:
            raise ConfigurationError(f"{label} must be a string or a table")
        if hook.script is not None and (
            len(hook.script.encode("utf-8")) > MAX_HOOK_SCRIPT_BYTES
        ):
            raise ConfigurationError(f"{label} script is larger than 64 KiB")
        hooks[name] = hook
    return HooksConfig(provision=hooks["provision"], startup=hooks["startup"])


def _parse_hook_table(name: str, label: str, value: dict[str, Any]) -> HookConfig:
    """Parse one ``[msb.hooks.<name>]`` table.

    :param name: Hook name (``provision`` or ``startup``).
    :param label: Config key label used in error messages.
    :param value: Raw hook table.
    :returns: Parsed hook config.
    :raises ConfigurationError: If the table is malformed.
    """
    known_keys = (
        "script",
        "file",
        "required",
        "timeout",
        "workdir",
        "env",
        "env_passthrough",
    )
    unknown = sorted(key for key in value if key not in known_keys)
    if unknown:
        raise ConfigurationError(f"{label} has unknown key(s): {', '.join(unknown)}")
    script = value.get("script")
    file = value.get("file")
    if script is not None and file is not None:
        raise ConfigurationError(f"{label}: set either script or file, not both")
    if script is None and file is None:
        raise ConfigurationError(f"{label} needs script or file")
    if script is not None:
        if not isinstance(script, str) or not script.strip():
            raise ConfigurationError(f"{label}.script must be a non-empty string")
    else:
        if not isinstance(file, str) or not file:
            raise ConfigurationError(f"{label}.file must be a non-empty string")
    required = value.get("required", HOOK_REQUIRED_DEFAULTS[name])
    if not isinstance(required, bool):
        raise ConfigurationError(f"{label}.required must be true or false")
    timeout = value.get("timeout")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
    ):
        raise ConfigurationError(
            f"{label}.timeout must be a positive integer (seconds)"
        )
    workdir = value.get("workdir")
    if workdir is not None and (not isinstance(workdir, str) or not workdir):
        raise ConfigurationError(f"{label}.workdir must be a non-empty string")
    env = _parse_string_dict(value.get("env"), f"{label}.env")
    for key in env:
        if not _ENV_NAME_PATTERN.fullmatch(key):
            raise ConfigurationError(
                f"{label}.env key {key!r} is not a valid environment variable name"
            )
    env_passthrough = value.get("env_passthrough")
    if env_passthrough is None:
        env_passthrough = ()
    elif not isinstance(env_passthrough, list) or not all(
        isinstance(item, str) and item for item in env_passthrough
    ):
        raise ConfigurationError(
            f"{label}.env_passthrough must be a list of non-empty strings"
        )
    return HookConfig(
        name=name,
        script=script,
        file=file,
        required=required,
        timeout=timeout,
        workdir=workdir,
        env=env,
        env_passthrough=tuple(env_passthrough),
    )


def load_hook_script(hook: HookConfig, workspace: Path) -> str:
    """Return the hook script text for a hook.

    Inline scripts are returned verbatim; ``file`` hooks are read from the
    host. Relative file paths resolve against *workspace*.

    :param hook: Parsed hook configuration.
    :param workspace: Workspace directory used to resolve relative paths.
    :returns: The hook script text.
    :raises ConfigurationError: If the file cannot be read, is too large,
        is not UTF-8 text, or is empty.
    """
    if hook.script is not None:
        return hook.script
    if hook.file is None:
        raise ConfigurationError(f"msb.hooks.{hook.name} needs script or file")
    path = Path(hook.file).expanduser()
    if not path.is_absolute():
        path = workspace / path
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ConfigurationError(
            f"msb.hooks.{hook.name}.file {str(path)!r} cannot be read: {e}"
        ) from e
    if len(data) > MAX_HOOK_SCRIPT_BYTES:
        raise ConfigurationError(
            f"msb.hooks.{hook.name}.file {str(path)!r} is larger than 64 KiB"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ConfigurationError(
            f"msb.hooks.{hook.name}.file {str(path)!r} is not UTF-8 text"
        ) from e
    if not text.strip():
        raise ConfigurationError(f"msb.hooks.{hook.name}.file {str(path)!r} is empty")
    return text


def _parse_agent(raw: Any) -> AgentConfig:
    """Parse the ``[msb.agent]`` table.

    :param raw: Raw table value or ``None``.
    :returns: Parsed agent config.
    :raises ConfigurationError: If the table is malformed.
    """
    if raw is None:
        return AgentConfig()
    if not isinstance(raw, dict):
        raise ConfigurationError("msb.agent must be a table")
    command = raw.get("command")
    if command is None:
        command = list(DEFAULT_AGENT_COMMAND)
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise ConfigurationError(
            "msb.agent.command must be a non-empty list of strings"
        )
    workdir = raw.get("workdir")
    if workdir is not None and (not isinstance(workdir, str) or not workdir):
        raise ConfigurationError("msb.agent.workdir must be a non-empty string")
    return AgentConfig(command=tuple(command), workdir=workdir)


def _parse_git(raw: Any) -> GitConfig | None:
    """Parse the ``[git]`` table.

    All three keys are interpreted (``worktree_mirror``, ``config_path``,
    ``signing_key_path``); unknown keys are logged at debug level and
    ignored. File existence is checked later and never raises here.

    :param raw: Raw table value or ``None``.
    :returns: Parsed settings, or ``None`` when the table is absent.
    :raises ConfigurationError: If the table or a path is malformed.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigurationError("git must be a table")
    worktree_mirror = raw.get("worktree_mirror", False)
    if not isinstance(worktree_mirror, bool):
        raise ConfigurationError("git.worktree_mirror must be a boolean")

    paths: dict[str, str | None] = {}
    for key in ("config_path", "signing_key_path"):
        value = raw.get(key)
        if value is None:
            paths[key] = None
            continue
        if not isinstance(value, str) or not value:
            raise ConfigurationError(f"git.{key} must be a non-empty string")
        expanded = os.path.expanduser(value)
        if not os.path.isabs(expanded):
            raise ConfigurationError(f"git.{key} must be an absolute path")
        paths[key] = expanded

    for key in raw:
        if key not in ("worktree_mirror", "config_path", "signing_key_path"):
            logger.debug(
                "Unsupported config key 'git.%s' for msb-circus — ignoring", key
            )
    return GitConfig(
        worktree_mirror=worktree_mirror,
        config_path=paths["config_path"],
        signing_key_path=paths["signing_key_path"],
    )


def _parse_ssh(raw: Any) -> SshConfig | None:
    """Parse the ``[ssh]`` table.

    The table's presence enables SSH agent forwarding, even when empty.
    ``config_path`` and ``known_hosts_path`` are optional; ``~`` is
    expanded. Other keys are logged at debug level and ignored.

    :param raw: Raw table value or ``None``.
    :returns: Parsed settings, or ``None`` when the table is absent.
    :raises ConfigurationError: If the table or a path is malformed.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigurationError("ssh must be a table")

    paths: dict[str, str | None] = {}
    for key in ("config_path", "known_hosts_path"):
        value = raw.get(key)
        if value is None:
            paths[key] = None
            continue
        if not isinstance(value, str) or not value:
            raise ConfigurationError(f"ssh.{key} must be a non-empty string")
        expanded = os.path.expanduser(value)
        if not os.path.isabs(expanded):
            raise ConfigurationError(f"ssh.{key} must be an absolute path")
        paths[key] = expanded

    for key in raw:
        if key not in ("config_path", "known_hosts_path"):
            logger.debug(
                "Unsupported config key 'ssh.%s' for msb-circus — ignoring", key
            )
    return SshConfig(
        config_path=paths["config_path"],
        known_hosts_path=paths["known_hosts_path"],
    )


def _parse_hosts(raw: Any) -> HostsConfig | None:
    """Parse the ``[hosts]`` table.

    ``patterns`` is a list of non-empty strings; ``re:``-prefixed entries
    must compile as regexes. ``file`` defaults to :data:`DEFAULT_HOSTS_FILE`
    and must be an absolute host path (``~`` is expanded). Unknown keys are
    logged at debug level and ignored.

    :param raw: Raw table value or ``None``.
    :returns: Parsed settings, or ``None`` when the table is absent.
    :raises ConfigurationError: If the table or a field is malformed.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigurationError("hosts must be a table")

    patterns = raw.get("patterns", [])
    if not isinstance(patterns, list) or not all(
        isinstance(p, str) and p for p in patterns
    ):
        raise ConfigurationError("hosts.patterns must be a list of non-empty strings")
    for p in patterns:
        if p.startswith("re:"):
            try:
                re.compile(p[3:], re.IGNORECASE)
            except re.error as e:
                raise ConfigurationError(
                    f"hosts.patterns entry {p!r} is not a valid regular expression: {e}"
                ) from e

    file = raw.get("file", DEFAULT_HOSTS_FILE)
    if not isinstance(file, str) or not file:
        raise ConfigurationError("hosts.file must be a non-empty string")
    file = os.path.expanduser(file)
    if not os.path.isabs(file):
        raise ConfigurationError("hosts.file must be an absolute path")

    for key in raw:
        if key not in ("patterns", "file"):
            logger.debug(
                "Unsupported config key 'hosts.%s' for msb-circus — ignoring", key
            )
    return HostsConfig(patterns=tuple(patterns), file=file)


def _parse_string_dict(raw: Any, label: str) -> dict[str, str]:
    """Parse a ``KEY = "value"`` table into a plain string dict.

    :param raw: Raw table value or ``None``.
    :param label: Config key label used in error messages.
    :returns: Parsed string mapping.
    :raises ConfigurationError: If the table is malformed.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{label} must be a table")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            raise ConfigurationError(f"{label}.{key} must be a string")
        result[key] = value
    return result


def _parse_env_tables(top_raw: Any, msb_raw: Any) -> dict[str, str]:
    """Merge the top-level ``[env]`` and the ``[msb.env]`` tables.

    ``[msb.env]`` wins for a key set in both. Values that contain ``$``
    are skipped with a warning: agent-circus expands them at image build
    time, but msb passes values literally.

    :param top_raw: Raw ``[env]`` table or ``None``.
    :param msb_raw: Raw ``[msb.env]`` table or ``None``.
    :returns: Merged variables.
    :raises ConfigurationError: If a table is malformed, a name is not a
        valid environment variable name, or a name starts with ``MSB_``.
    """
    top = _parse_string_dict(top_raw, "env")
    msb = _parse_string_dict(msb_raw, "msb.env")
    merged = {**top, **msb}
    for key in merged:
        label = "msb.env" if key in msb else "env"
        if not _ENV_NAME_PATTERN.fullmatch(key):
            raise ConfigurationError(
                f"{label} key {key!r} is not a valid environment variable name"
            )
        if key.startswith("MSB_"):
            raise ConfigurationError(
                f"{label} key {key!r} uses the MSB_ prefix, which msb reserves"
            )
    for key, value in merged.items():
        if "$" in value:
            logger.warning(
                "Ignoring %s: msb-circus does not expand $ references in [env] "
                "values; set the full value instead.",
                key,
            )
    return {key: value for key, value in merged.items() if "$" not in value}


def _parse_env_passthrough(raw: Any) -> tuple[str, ...]:
    """Parse the ``env_passthrough`` list.

    :param raw: Raw value or ``None``.
    :returns: Tuple of variable names.
    :raises ConfigurationError: If the value is malformed.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(
        isinstance(name, str) and name for name in raw
    ):
        raise ConfigurationError("env_passthrough must be a list of non-empty strings")
    return tuple(raw)


def _parse_logging(raw: Any) -> tuple[str, str | None]:
    """Parse the ``[logging]`` table.

    :param raw: Raw table value or ``None``.
    :returns: Tuple of ``(level, file)``.
    :raises ConfigurationError: If the table is malformed.
    """
    if raw is None:
        return "INFO", None
    if not isinstance(raw, dict):
        raise ConfigurationError("logging must be a table")
    level = raw.get("level", "INFO")
    if not isinstance(level, str) or not level:
        raise ConfigurationError("logging.level must be a non-empty string")
    log_file = raw.get("file")
    if log_file is not None and not isinstance(log_file, str):
        raise ConfigurationError("logging.file must be a string")
    return level, log_file


def parse_config(config: dict[str, Any]) -> MsbConfig:
    """Parse and validate a merged raw configuration.

    :param config: Merged raw configuration dictionary.
    :returns: Validated :class:`MsbConfig`.
    :raises ConfigurationError: If any supported key is malformed.
    """
    warn_unsupported_keys(config)
    msb_raw = config.get("msb")
    if msb_raw is not None and not isinstance(msb_raw, dict):
        raise ConfigurationError("msb must be a table")
    msb_raw = msb_raw or {}
    if "hooks" in config and "hooks" not in msb_raw:
        logger.info(
            "[hooks] is an agent-circus setting and is not used by msb-circus; "
            "see [msb.hooks] in README.msb-circus.md."
        )
    binary = msb_raw.get("binary")
    if binary is not None and not isinstance(binary, str):
        raise ConfigurationError("msb.binary must be a string")
    level, log_file = _parse_logging(config.get("logging"))
    git = _parse_git(config.get("git"))
    return MsbConfig(
        binary=binary,
        image=_parse_image(msb_raw.get("image")),
        sandbox=_parse_sandbox(msb_raw.get("sandbox")),
        network=_parse_network(msb_raw.get("network")),
        agent=_parse_agent(msb_raw.get("agent")),
        tracing=_parse_tracing(msb_raw.get("tracing")),
        mounts=(
            _parse_string_dict(msb_raw.get("mounts"), "msb.mounts") or DEFAULT_MOUNTS
        ),
        additional_dirs=_parse_additional_dirs(config.get("additional_dirs")),
        port_forwards=_parse_port_forwards(config.get("port_forwards")),
        daemons=_parse_daemons(msb_raw.get("daemons")),
        hooks=_parse_hooks(msb_raw.get("hooks")),
        env=_parse_env_tables(config.get("env"), msb_raw.get("env")),
        env_passthrough=_parse_env_passthrough(config.get("env_passthrough")),
        git_worktree_mirror=git.worktree_mirror if git is not None else False,
        git=git,
        ssh=_parse_ssh(config.get("ssh")),
        hosts=_parse_hosts(config.get("hosts")),
        logging_level=level,
        logging_file=log_file,
    )


def load_config(workspace: Path, environ: dict[str, str] | None = None) -> MsbConfig:
    """Load, merge, and validate msb-circus configuration.

    :param workspace: Workspace directory.
    :param environ: Environment mapping for the XDG lookup.
    :returns: Resolved configuration.
    :raises ConfigurationError: If a config file is invalid.
    """
    return parse_config(read_config_layers(workspace, environ))
