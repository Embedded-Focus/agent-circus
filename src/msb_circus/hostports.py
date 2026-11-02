"""Host-side port availability probes for msb-circus port forwards.

msb binds the host side of every ``[[port_forwards]]`` entry when the
sandbox boots, but does not reliably fail when the port is already held
by another process (e.g. a second VM) — the forward then silently
answers with empty replies. This module probes each configured host
port with a plain bind attempt so msb-circus can fail with a clear
message before booting.
"""

from __future__ import annotations

import errno
import socket

from .config import PortForward


def _can_bind(host: str, port: int, protocol: str) -> bool:
    """Return ``True`` when *host*:*port* can still be bound on this host.

    The probe mimics a server socket (``SO_REUSEADDR`` set, so sockets in
    ``TIME_WAIT`` do not cause false positives). Known blind spot: on
    Linux, binding a specific address while a ``SO_REUSEADDR`` wildcard
    listener holds the same port is permitted, matching what msb itself
    could do; active listeners without ``SO_REUSEADDR`` are detected.
    The check is best-effort: another process can still grab the port
    between probe and boot; msb then reports the conflict itself.

    :param host: Host bind address (IPv4 or IPv6 literal).
    :param port: Host port number.
    :param protocol: ``"tcp"`` or ``"udp"``.
    :returns: ``True`` when free to bind, ``False`` when already occupied.
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock_type = socket.SOCK_DGRAM if protocol == "udp" else socket.SOCK_STREAM
    with socket.socket(family, sock_type) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as e:
            # A bind address not assigned to this host cannot be occupied
            # by anyone; msb reports the real error when it tries to bind.
            return e.errno == errno.EADDRNOTAVAIL
    return True


def find_occupied_ports(
    forwards: tuple[PortForward, ...] | list[PortForward],
) -> list[PortForward]:
    """Return the forwards whose host port is already bound on this host.

    :param forwards: Port forwards from :class:`msb_circus.config.MsbConfig`.
    :returns: Subset of *forwards* whose ``host:``:attr:`PortForward.host_port`
        pair cannot be bound right now.
    """
    return [f for f in forwards if not _can_bind(f.host, f.host_port, f.protocol)]
