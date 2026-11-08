"""Tests for the SSH agent forwarding host-side helpers."""

import os
import socket
from pathlib import Path

import pytest

from msb_circus.config import SshConfig
from msb_circus.exceptions import ConfigurationError
from msb_circus.ssh_agent import (
    agent_link_path,
    refresh_agent_link,
    ssh_file_copies,
    ssh_sandbox_env,
)


def _make_socket(name: str = "agent.sock") -> tuple[socket.socket, str]:
    """Create a listening Unix socket at a short path under /tmp.

    ``tmp_path`` can exceed the AF_UNIX path limit on macOS, so the socket
    is placed directly below /tmp instead.

    :param name: Socket file name.
    :returns: Tuple of the listening socket and its path.
    """
    import tempfile

    path = Path(tempfile.mkdtemp(prefix="msbc-ssh-")) / name
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    return sock, str(path)


def test_agent_link_path_prefers_xdg_runtime_dir(tmp_path):
    link = agent_link_path({"XDG_RUNTIME_DIR": str(tmp_path), "HOME": "/elsewhere"})
    assert link == tmp_path / "msb-circus" / "ssh-agent.sock"


def test_agent_link_path_falls_back_to_home_cache(tmp_path):
    link = agent_link_path({"HOME": str(tmp_path)})
    assert link == tmp_path / ".cache" / "msb-circus" / "ssh-agent.sock"


def test_refresh_agent_link_points_at_current_socket(tmp_path, monkeypatch):
    sock, sock_path = _make_socket()
    try:
        environ = {
            "XDG_RUNTIME_DIR": str(tmp_path),
            "SSH_AUTH_SOCK": sock_path,
        }
        link = refresh_agent_link(environ)
        assert os.readlink(link) == sock_path
    finally:
        sock.close()


def test_refresh_agent_link_replaces_stale_link(tmp_path):
    first, first_path = _make_socket("first.sock")
    second, second_path = _make_socket("second.sock")
    try:
        environ = {"XDG_RUNTIME_DIR": str(tmp_path), "SSH_AUTH_SOCK": first_path}
        link = refresh_agent_link(environ)
        assert os.readlink(link) == first_path
        environ = {"XDG_RUNTIME_DIR": str(tmp_path), "SSH_AUTH_SOCK": second_path}
        link = refresh_agent_link(environ)
        assert os.readlink(link) == second_path
    finally:
        first.close()
        second.close()


def test_refresh_agent_link_accepts_unchanged_socket(tmp_path):
    # Regression: the second refresh with the same agent must not mistake
    # the existing link (already pointing at that agent) for a loop.
    sock, sock_path = _make_socket("agent.sock")
    try:
        environ = {"XDG_RUNTIME_DIR": str(tmp_path), "SSH_AUTH_SOCK": sock_path}
        refresh_agent_link(environ)
        link = refresh_agent_link(environ)
        assert os.readlink(link) == sock_path
    finally:
        sock.close()


def test_refresh_agent_link_rejects_missing_env(tmp_path):
    with pytest.raises(ConfigurationError, match="SSH_AUTH_SOCK is not set"):
        refresh_agent_link({"XDG_RUNTIME_DIR": str(tmp_path)})


def test_refresh_agent_link_rejects_non_socket(tmp_path):
    regular = tmp_path / "regular.txt"
    regular.write_text("not a socket")
    with pytest.raises(ConfigurationError, match="is not a socket"):
        refresh_agent_link(
            {"XDG_RUNTIME_DIR": str(tmp_path), "SSH_AUTH_SOCK": str(regular)}
        )


def test_refresh_agent_link_rejects_missing_socket(tmp_path):
    with pytest.raises(ConfigurationError, match="is not a socket"):
        refresh_agent_link(
            {"XDG_RUNTIME_DIR": str(tmp_path), "SSH_AUTH_SOCK": str(tmp_path / "gone")}
        )


def test_refresh_agent_link_rejects_self_reference(tmp_path):
    _real, real_path = _make_socket("real.sock")
    try:
        link_dir = tmp_path / "msb-circus"
        link_dir.mkdir()
        link = link_dir / "ssh-agent.sock"
        link.symlink_to(real_path)
        # SSH_AUTH_SOCK resolves through the link to a real socket; the
        # refresh must still detect the loop.
        with pytest.raises(
            ConfigurationError, match="points at the msb-circus agent link"
        ):
            refresh_agent_link(
                {"XDG_RUNTIME_DIR": str(tmp_path), "SSH_AUTH_SOCK": str(link)}
            )
    finally:
        _real.close()


def test_refresh_agent_link_rejects_long_path():
    _sock, sock_path = _make_socket()
    try:
        deep = "/tmp/" + ("d" * 90)
        environ = {"XDG_RUNTIME_DIR": deep, "SSH_AUTH_SOCK": sock_path}
        with pytest.raises(ConfigurationError, match="too long"):
            refresh_agent_link(environ)
    finally:
        _sock.close()


def test_ssh_file_copies_omits_unset_entries():
    assert ssh_file_copies(SshConfig()) == []
    assert ssh_file_copies(SshConfig(config_path="/host/config")) == [
        ("/host/config", "/root/.ssh/config")
    ]
    assert ssh_file_copies(
        SshConfig(config_path="/host/config", known_hosts_path="/host/kh")
    ) == [
        ("/host/config", "/root/.ssh/config"),
        ("/host/kh", "/root/.ssh/known_hosts"),
    ]


def test_ssh_sandbox_env_sets_guest_socket_and_port():
    env = ssh_sandbox_env()
    assert env["SSH_AUTH_SOCK"] == "/run/ssh-agent.sock"
    assert env["MSB_CIRCUS_SSH_AGENT_VSOCK_PORT"] == "22022"
