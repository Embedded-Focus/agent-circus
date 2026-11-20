"""Tests for the host-side port availability probe."""

import socket

from agent_circus.config import PortForward
from agent_circus.hostports import find_occupied_ports


def _bound_tcp_socket() -> tuple[socket.socket, int]:
    """Bind a TCP listener to an ephemeral port and return it with the port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def test_find_occupied_ports_detects_bound_tcp_port():
    sock, port = _bound_tcp_socket()
    try:
        forwards = [
            PortForward(guest_port=port, host_port=port),
            PortForward(guest_port=9999, host_port=9999, host="0.0.0.0"),
        ]
        assert find_occupied_ports(forwards) == [forwards[0]]
    finally:
        sock.close()


def test_find_occupied_ports_empty_when_all_free():
    forwards = [PortForward(guest_port=9999, host_port=9999)]
    assert find_occupied_ports(forwards) == []


def test_find_occupied_ports_detects_bound_udp_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    try:
        port = sock.getsockname()[1]
        forwards = [
            PortForward(
                guest_port=port, host_port=port, host="127.0.0.1", protocol="udp"
            ),
        ]
        assert find_occupied_ports(forwards) == forwards
    finally:
        sock.close()


def test_find_occupied_ports_ignores_unassigned_bind_address():
    # TEST-NET-1 is reserved and not assigned locally; the probe must not
    # report it as occupied (msb reports the real error at boot).
    forwards = [
        PortForward(guest_port=9999, host_port=9999, host="203.0.113.1"),
    ]
    assert find_occupied_ports(forwards) == []


def test_pick_free_port_returns_bindable_port():
    from agent_circus.hostports import pick_free_port

    port = pick_free_port()
    # The picked port is bindable again immediately (SO_REUSEADDR probe).
    from agent_circus.hostports import can_bind

    assert can_bind("127.0.0.1", port, "tcp")


def test_can_bind_detects_occupied_tcp_port():
    from agent_circus.hostports import can_bind

    sock, port = _bound_tcp_socket()
    try:
        assert not can_bind("127.0.0.1", port, "tcp")
        assert can_bind("127.0.0.1", port + 1, "tcp")
    finally:
        sock.close()
