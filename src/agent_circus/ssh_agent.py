"""Host-side helpers for SSH agent forwarding into msb sandboxes.

The sandbox's vsock route to the host SSH agent is fixed when the sandbox
is created, but ``$SSH_AUTH_SOCK`` changes with every host login session.
msb-circus therefore points the route at a stable symlink (refreshed
before every boot, start, and reuse) and passes a sandbox-level env var
that makes the image's s6 services bridge a guest Unix socket to the
host agent over virtio-vsock. No private key material enters the guest.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from .config import (
    SSH_AGENT_GUEST_SOCKET,
    SSH_AGENT_PORT_ENV_VAR,
    SSH_AGENT_VSOCK_PORT,
    SSH_GUEST_DIR,
    SshConfig,
)
from .exceptions import ConfigurationError

logger = logging.getLogger(__name__)

#: Unix socket addresses are limited to about 104 bytes on common systems.
_SOCKET_PATH_LIMIT = 100


def _resolve_parent(path: str | Path) -> str:
    """Resolve symlinks in the directory part of *path* only.

    The final component is kept as is, so a symlink at *path* is not
    followed. Shortcoming: a chain of symlinks ending at the agent link is
    not detected by comparing these results.

    :param path: Path to normalize.
    :returns: Absolute path with a symlink-free directory part.
    """
    absolute = os.path.abspath(path)
    return os.path.join(
        os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute)
    )


def agent_link_path(environ: Mapping[str, str] | None = None) -> Path:
    """Return the stable host path that the sandbox vsock route points at.

    ``$XDG_RUNTIME_DIR/msb-circus/ssh-agent.sock`` when ``XDG_RUNTIME_DIR``
    is set and non-empty, else ``$HOME/.cache/msb-circus/ssh-agent.sock``.

    :param environ: Environment mapping, defaults to :data:`os.environ`.
    :returns: Absolute path of the symlink.
    """
    env = os.environ if environ is None else environ
    xdg = env.get("XDG_RUNTIME_DIR", "")
    if xdg:
        return Path(xdg) / "msb-circus" / "ssh-agent.sock"
    home = env.get("HOME", str(Path.home()))
    return Path(home) / ".cache" / "msb-circus" / "ssh-agent.sock"


def refresh_agent_link(environ: Mapping[str, str] | None = None) -> Path:
    """Point the stable link at the current host SSH agent socket.

    :param environ: Environment mapping, defaults to :data:`os.environ`.
    :returns: The link path (see :func:`agent_link_path`).
    :raises ConfigurationError: If ``SSH_AUTH_SOCK`` is unset, empty, not
        a socket, resolves to the link itself, or the link path is too long
        for a Unix socket address.
    """
    env = os.environ if environ is None else environ
    sock = env.get("SSH_AUTH_SOCK", "")
    if not sock:
        raise ConfigurationError(
            "SSH agent forwarding requested but SSH_AUTH_SOCK is not set. "
            "Is ssh-agent running? Start one with: eval $(ssh-agent) && ssh-add"
        )
    target = os.path.abspath(sock)
    try:
        st = os.stat(target)
    except OSError as e:
        raise ConfigurationError(f"SSH_AUTH_SOCK {sock!r} is not a socket") from e
    if not stat.S_ISSOCK(st.st_mode):
        raise ConfigurationError(f"SSH_AUTH_SOCK {sock!r} is not a socket")
    link = agent_link_path(environ)
    # Compare without following the final path component: the link itself
    # usually exists already and points at the current agent, so resolving
    # it would report a loop on every refresh after the first one.
    if _resolve_parent(target) == _resolve_parent(link):
        raise ConfigurationError(
            "SSH_AUTH_SOCK points at the msb-circus agent link itself; "
            "export the real agent socket instead"
        )
    if len(os.fsencode(str(link))) > _SOCKET_PATH_LIMIT:
        raise ConfigurationError(
            f"SSH agent link path {str(link)!r} is too long for a Unix "
            f"socket address (limit {_SOCKET_PATH_LIMIT} bytes). Set a "
            "shorter XDG_RUNTIME_DIR or HOME."
        )
    link.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    os.symlink(target, tmp)
    os.replace(tmp, link)
    logger.debug("SSH agent link %s -> %s", link, target)
    return link


def ssh_file_copies(ssh: SshConfig) -> list[tuple[str, str]]:
    """Return ``(host path, guest path)`` pairs of SSH files to copy.

    :param ssh: Parsed ``[ssh]`` settings.
    :returns: Pairs for ``config_path`` -> ``/root/.ssh/config`` and
        ``known_hosts_path`` -> ``/root/.ssh/known_hosts``, omitting unset
        entries. Existence is checked by the caller.
    """
    copies: list[tuple[str, str]] = []
    if ssh.config_path is not None:
        copies.append((ssh.config_path, f"{SSH_GUEST_DIR}/config"))
    if ssh.known_hosts_path is not None:
        copies.append((ssh.known_hosts_path, f"{SSH_GUEST_DIR}/known_hosts"))
    return copies


def ssh_sandbox_env() -> dict[str, str]:
    """Return sandbox-level env vars enabling the guest agent bridge.

    :returns: ``SSH_AUTH_SOCK`` (guest socket path) and the bridge port
        variable read by the image's s6 services.
    """
    return {
        "SSH_AUTH_SOCK": SSH_AGENT_GUEST_SOCKET,
        SSH_AGENT_PORT_ENV_VAR: str(SSH_AGENT_VSOCK_PORT),
    }
