"""Pure helpers for the ``msb`` sandbox lifecycle subcommands.

argv builders and output parsers return/accept plain data so they can be
unit-tested without msb installed. Execution lives in
:mod:`msb_circus.msb`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from .config import DEFAULT_MEMORY, PortForward
from .exceptions import ConfigurationError

SANDBOX_COMMANDS = ("run", "create", "start", "stop", "list", "remove")

#: Guest path of the dedicated docker data disk.
DOCKER_DATA_GUEST_PATH = "/var/lib/docker"

#: Root disk kinds as serialized by msb (mirrors the SDK's ``RootDiskKind``).
ROOT_DISK_KINDS = frozenset({"managed", "tmpfs", "flat", "disk-image"})


def build_run_args(
    *,
    name: str,
    image: str,
    memory: str = DEFAULT_MEMORY,
    cpus: int | None = None,
    mounts: dict[str, str] | None = None,
    net: str | None = None,
    port_forwards: tuple[PortForward, ...] | list[PortForward] | None = None,
    env: dict[str, str] | None = None,
    workdir: str | None = None,
    docker_disk: str | None = None,
    root_disk: str | None = None,
    detach: bool = True,
) -> list[str]:
    """Build argv for ``msb run`` (first-time sandbox boot).

    The boot must go through ``msb run --detach`` so the image ENTRYPOINT
    applies (it starts dockerd and imports the TLS CA); ``msb create`` and
    ``msb exec`` do not run the entrypoint.

    :param name: Sandbox name (``agents-<project>``).
    :param image: Image reference to boot.
    :param memory: Memory limit (e.g. ``"4G"``).
    :param cpus: Optional CPU count.
    :param mounts: Host path -> guest spec mapping (guest spec may include
        options, e.g. ``"/workspace:ro"``).
    :param net: Optional network profile (e.g. ``"public,private"``).
    :param port_forwards: Optional guest ports to publish on the host.
    :param env: Optional sandbox-level environment variables.
    :param workdir: Optional guest working directory.
    :param docker_disk: Optional size of a dedicated ext4 disk for the
        docker data root, sent as ``--mount-owned``. ``None`` sends no
        disk: dockerd then has no usable storage backend.
    :param root_disk: Optional root disk spec sent as ``--root-disk``
        verbatim (e.g. ``"flat:32G"``); ``None`` keeps the msb default.
    :param detach: Whether to boot detached (default True).
    :returns: Argv list for :class:`msb_circus.msb.MsbInvoker`.
    """
    args = ["run", "--name", name, "--memory", memory]
    if cpus is not None:
        args += ["--cpus", str(cpus)]
    for host, guest in (mounts or {}).items():
        args += ["--volume", f"{host}:{guest}"]
    if root_disk is not None:
        args += ["--root-disk", root_disk]
    if docker_disk is not None:
        args += [
            "--mount-owned",
            f"{DOCKER_DATA_GUEST_PATH}:kind=disk,size={docker_disk}",
        ]
    if net:
        args += ["--net", net]
    for forward in port_forwards or ():
        token = f"{forward.host}:{forward.host_port}:{forward.guest_port}"
        if forward.protocol == "udp":
            token += "/udp"
        args += ["--port", token]
    for key, value in (env or {}).items():
        args += ["--env", f"{key}={value}"]
    if workdir:
        args += ["--workdir", workdir]
    if detach:
        args.append("--detach")
    args.append(image)
    return args


def build_list_args(
    *, running_only: bool = True, json_format: bool = True
) -> list[str]:
    """Build argv for ``msb list``.

    :param running_only: Restrict output to running sandboxes.
    :param json_format: Request machine-readable JSON output.
    :returns: Argv list.
    """
    args = ["list"]
    if running_only:
        args.append("--running")
    if json_format:
        args += ["--format", "json"]
    return args


def build_inspect_args(name: str, *, json_format: bool = True) -> list[str]:
    """Build argv for ``msb inspect``.

    :param name: Sandbox name.
    :param json_format: Request machine-readable JSON output.
    :returns: Argv list.
    """
    args = ["inspect", name]
    if json_format:
        args += ["--format", "json"]
    return args


def build_stop_args(name: str, *, force: bool = False) -> list[str]:
    """Build argv for ``msb stop``.

    :param name: Sandbox name.
    :param force: Kill immediately instead of graceful shutdown.
    :returns: Argv list.
    """
    args = ["stop"]
    if force:
        args.append("--force")
    args.append(name)
    return args


def build_start_args(name: str) -> list[str]:
    """Build argv for ``msb start``.

    :param name: Sandbox name.
    :returns: Argv list.
    """
    return ["start", name]


def build_remove_args(name: str, *, force: bool = False) -> list[str]:
    """Build argv for ``msb remove``.

    :param name: Sandbox name.
    :param force: Stop the sandbox if running, then remove it.
    :returns: Argv list.
    """
    args = ["remove"]
    if force:
        args.append("--force")
    args.append(name)
    return args


def parse_inspect_ports(output: str) -> frozenset[tuple[str, int, int, str]]:
    """Extract published ports from ``msb inspect --format json`` output.

    msb reports port mappings under ``config.network.ports`` (with an
    ``active_config`` mirror) as objects of ``host_bind``, ``host_port``,
    ``guest_port``, and ``protocol``. Ranges given on the command line are
    expanded to one entry per port. The parse is defensive: msb is beta
    and its schema may change between releases.

    :param output: Raw stdout of the inspect command.
    :returns: Set of ``(host, host_port, guest_port, protocol)`` tuples;
        an empty set when no ports are published.
    :raises ConfigurationError: If the output is not valid JSON, has an
        unexpected shape, or contains a malformed port entry.
    """
    try:
        document = json.loads(output)
    except json.JSONDecodeError as e:
        raise ConfigurationError(
            f"Could not parse msb inspect output as JSON: {e}"
        ) from e
    if not isinstance(document, dict):
        raise ConfigurationError("Unexpected msb inspect output: expected an object")
    config = document.get("config")
    if not isinstance(config, dict):
        config = document.get("active_config")
    if not isinstance(config, dict):
        raise ConfigurationError(
            "Unexpected msb inspect output: no 'config' or 'active_config' object"
        )
    network = config.get("network")
    ports = network.get("ports") if isinstance(network, dict) else None
    if ports is None:
        # A sandbox without published ports may omit the table entirely.
        return frozenset()
    if not isinstance(ports, list):
        raise ConfigurationError("Unexpected msb inspect output: network.ports")
    published: set[tuple[str, int, int, str]] = set()
    for index, entry in enumerate(ports):
        if not isinstance(entry, dict):
            raise ConfigurationError(
                f"Unexpected msb inspect port entry at index {index}: expected an object"
            )
        host = entry.get("host_bind", "127.0.0.1")
        protocol = entry.get("protocol", "tcp")
        host_port = entry.get("host_port")
        guest_port = entry.get("guest_port")
        if (
            not isinstance(host, str)
            or not isinstance(protocol, str)
            or isinstance(host_port, bool)
            or not isinstance(host_port, int)
            or isinstance(guest_port, bool)
            or not isinstance(guest_port, int)
        ):
            raise ConfigurationError(
                f"Malformed msb inspect port entry at index {index}"
            )
        published.add((host, host_port, guest_port, protocol))
    return frozenset(published)


def configured_ports(
    port_forwards: tuple[PortForward, ...] | list[PortForward],
) -> frozenset[tuple[str, int, int, str]]:
    """Normalize configured port forwards to the parsed-inspect shape.

    :param port_forwards: Port forwards from :class:`msb_circus.config.MsbConfig`.
    :returns: Set of ``(host, host_port, guest_port, protocol)`` tuples,
        matching :func:`parse_inspect_ports`.
    """
    return frozenset(
        (f.host, f.host_port, f.guest_port, f.protocol) for f in port_forwards
    )


@dataclass(frozen=True)
class GuestDiskConfig:
    """Disk-relevant settings extracted from ``msb inspect`` output.

    Every field is ``None``/``False`` when the corresponding setting was
    not found confidently. Callers must treat "not found" as "cannot
    verify" and skip drift warnings instead of reporting a mismatch.

    The schema mirrors microsandbox's persisted sandbox config (see
    ``VolumeMount::serialize`` and ``RootDisk`` in
    ``packages/microsandbox-types/rust/lib/domain.rs``): volume mounts are
    internally tagged with ``"type"`` (``Owned``/``Bind``/``Named``/
    ``Tmpfs``/``DiskImage``), an ``Owned`` mount carries its storage in a
    nested ``storage`` object tagged with ``kind`` (``disk``/``directory``),
    and the root disk is tagged with ``kind`` (``managed``/``tmpfs``/
    ``flat``/``disk-image``).

    :ivar root_disk_kind: Root disk kind (``managed``, ``tmpfs``,
        ``flat``, ``disk-image``), if found.
    :ivar root_disk_size_mib: Root disk size in MiB, if reported.
    :ivar docker_disk_size_mib: Size (``capacity_mib``) of the mount-owned
        docker data disk at :data:`DOCKER_DATA_GUEST_PATH`, if present.
    :ivar mounts_seen: ``True`` when at least one mount entry with a
        known shape was found, so absence of a docker disk is meaningful.
    """

    root_disk_kind: str | None = None
    root_disk_size_mib: int | None = None
    docker_disk_size_mib: int | None = None
    mounts_seen: bool = False


def parse_size_mib(spec: str) -> int | None:
    """Parse a msb-style size string into MiB.

    Accepts an integer or decimal value followed by an optional binary
    unit suffix (``K``, ``M``, ``G``, ``T``, ``P``, case-insensitive,
    with optional ``i``/``B`` decoration). Bare numbers without a unit
    are rejected: their unit is undefined in the msb grammar.

    :param spec: Size string (e.g. ``"10G"``, ``"512MiB"``).
    :returns: Size in MiB, or ``None`` when the string cannot be parsed.
    """
    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*([KMGTP])(?:I?B)?", spec.strip(), re.IGNORECASE
    )
    if match is None:
        return None
    value = float(match.group(1))
    factor = 1024 ** {"K": 1, "M": 2, "G": 3, "T": 4, "P": 5}[match.group(2).upper()]
    return round(value * factor / (1024 * 1024))


def parse_root_disk_spec(
    spec: str,
) -> tuple[str | None, int | None]:
    """Parse a ``--root-disk`` spec into ``(kind, size_mib)``.

    :param spec: Spec string as configured (e.g. ``"flat:32G"``, ``"16G"``).
    :returns: Kind and size in MiB; ``(None, None)`` for forms msb-circus
        does not model (e.g. user-supplied disk images), so drift checks
        skip the comparison.
    """
    for prefix in ("flat", "tmpfs"):
        if spec.startswith(f"{prefix}:"):
            return prefix, parse_size_mib(spec.split(":", 1)[1])
    size = parse_size_mib(spec)
    if size is not None:
        return "managed", size
    return None, None


def _walk_dicts(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every dictionary in a JSON-like structure, depth-first.

    :param node: Parsed JSON value.
    :yields: Each dictionary found anywhere in the structure.
    """
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_dicts(item)


def parse_inspect_disk_config(output: str) -> GuestDiskConfig:
    """Extract disk settings from ``msb inspect --format json`` output.

    The parse is defensive: msb is beta and its schema may change between
    releases. Entries are recognized by the shapes of microsandbox's
    persisted sandbox config: volume mounts tagged with ``"type"`` (an
    ``Owned`` mount nests its ``storage`` object with ``kind: "disk"`` and
    ``capacity_mib``) and the root disk tagged with ``kind`` (``managed``,
    ``tmpfs``, ``flat``, ``disk-image``); anything else is ignored. The
    docker disk is assumed to be the sandbox's only mount-owned disk,
    which holds while sandboxes are created by msb-circus.

    :param output: Raw stdout of the inspect command.
    :returns: Extracted settings; fields stay ``None`` when not found.
    :raises ConfigurationError: If the output is not valid JSON or does
        not have the expected top-level shape.
    """
    try:
        document = json.loads(output)
    except json.JSONDecodeError as e:
        raise ConfigurationError(
            f"Could not parse msb inspect output as JSON: {e}"
        ) from e
    if not isinstance(document, dict):
        raise ConfigurationError("Unexpected msb inspect output: expected an object")
    config = document.get("config", document.get("active_config"))
    if not isinstance(config, dict):
        raise ConfigurationError(
            "Unexpected msb inspect output: no 'config' or 'active_config' object"
        )

    result = GuestDiskConfig()
    for entry in _walk_dicts(config):
        mount_type = entry.get("type")
        if mount_type == "Owned" and isinstance(entry.get("storage"), dict):
            storage = entry["storage"]
            if storage.get("kind") == "disk":
                capacity = storage.get("capacity_mib")
                result = GuestDiskConfig(
                    root_disk_kind=result.root_disk_kind,
                    root_disk_size_mib=result.root_disk_size_mib,
                    docker_disk_size_mib=(
                        capacity if isinstance(capacity, int) else None
                    ),
                    mounts_seen=True,
                )
            else:
                result = GuestDiskConfig(
                    root_disk_kind=result.root_disk_kind,
                    root_disk_size_mib=result.root_disk_size_mib,
                    docker_disk_size_mib=result.docker_disk_size_mib,
                    mounts_seen=True,
                )
            continue
        if isinstance(mount_type, str) and mount_type in {
            "Bind",
            "Named",
            "Tmpfs",
            "DiskImage",
        }:
            result = GuestDiskConfig(
                root_disk_kind=result.root_disk_kind,
                root_disk_size_mib=result.root_disk_size_mib,
                docker_disk_size_mib=result.docker_disk_size_mib,
                mounts_seen=True,
            )
            continue
        kind = entry.get("kind")
        if (
            result.root_disk_kind is None
            and isinstance(kind, str)
            and kind in ROOT_DISK_KINDS
            and any(key in entry for key in ("size_mib", "path", "fstype", "clone"))
        ):
            size = entry.get("size_mib")
            result = GuestDiskConfig(
                root_disk_kind=kind,
                root_disk_size_mib=size if isinstance(size, int) else None,
                docker_disk_size_mib=result.docker_disk_size_mib,
                mounts_seen=result.mounts_seen,
            )
    return result


@dataclass(frozen=True)
class SandboxInfo:
    """A single entry of ``msb list`` output.

    Field extraction is defensive: msb is beta and its JSON schema may
    change between releases; unexpected entries are preserved in
    :attr:`raw`.

    :ivar name: Sandbox name.
    :ivar status: Lifecycle status (e.g. ``Running``, ``Stopped``).
    :ivar id: Sandbox identifier, if present.
    :ivar created_at: Creation timestamp (epoch seconds), if present.
    :ivar updated_at: Last-update timestamp (epoch seconds), if present.
    :ivar raw: The original JSON entry.
    """

    name: str
    status: str | None = None
    id: str | None = None
    created_at: float | None = None
    updated_at: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def parse_sandbox_list_output(output: str) -> list[SandboxInfo]:
    """Parse ``msb list --format json`` output.

    Accepts a top-level JSON array of entries, or an object with a
    ``sandboxes`` array (in case msb changes to a paginated envelope).
    The status is read from ``status`` or ``state``, whichever is present.

    :param output: Raw stdout of the list command.
    :returns: Parsed sandbox entries.
    :raises ConfigurationError: If the output is not valid JSON or has an
        unexpected shape.
    """
    try:
        document = json.loads(output)
    except json.JSONDecodeError as e:
        raise ConfigurationError(f"Could not parse msb list output as JSON: {e}") from e
    if isinstance(document, dict):
        if "sandboxes" not in document:
            raise ConfigurationError(
                "Unexpected msb list output shape: object without 'sandboxes'"
            )
        document = document["sandboxes"]
    if not isinstance(document, list):
        raise ConfigurationError(
            "Unexpected msb list output shape: expected a JSON array"
        )
    sandboxes: list[SandboxInfo] = []
    for index, entry in enumerate(document):
        if not isinstance(entry, dict):
            raise ConfigurationError(
                f"Unexpected msb list entry at index {index}: expected an object"
            )
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigurationError(
                f"msb list entry at index {index} has no valid 'name'"
            )
        status = entry.get("status", entry.get("state"))
        sandboxes.append(
            SandboxInfo(
                name=name,
                status=status if isinstance(status, str) else None,
                id=entry.get("id") if isinstance(entry.get("id"), str) else None,
                created_at=(
                    entry["created_at"]
                    if isinstance(entry.get("created_at"), int | float)
                    else None
                ),
                updated_at=(
                    entry["updated_at"]
                    if isinstance(entry.get("updated_at"), int | float)
                    else None
                ),
                raw=entry,
            )
        )
    return sandboxes
