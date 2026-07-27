"""Tests for SSH agent forwarding compose override."""

import json

import pytest

from agent_circus.config import AVAILABLE_SERVICES, build_ssh_override
from agent_circus.exceptions import ConfigurationError


def test_build_ssh_override_volume_spec() -> None:
    result = json.loads(build_ssh_override())
    for svc in AVAILABLE_SERVICES:
        assert (
            "${SSH_AUTH_SOCK}:/run/ssh-agent.sock:ro"
            in result["services"][svc]["volumes"]
        )


def test_build_ssh_override_env_var() -> None:
    result = json.loads(build_ssh_override())
    for svc in AVAILABLE_SERVICES:
        assert (
            result["services"][svc]["environment"]["SSH_AUTH_SOCK"]
            == "/run/ssh-agent.sock"
        )


def test_build_ssh_override_all_services() -> None:
    result = json.loads(build_ssh_override())
    assert set(result["services"].keys()) == set(AVAILABLE_SERVICES)


def test_build_ssh_override_returns_valid_json() -> None:
    output = build_ssh_override()
    parsed = json.loads(output)
    assert "services" in parsed


def test_build_ssh_override_config_path() -> None:
    result = json.loads(build_ssh_override(config_path="/home/user/.ssh/config"))
    for svc in AVAILABLE_SERVICES:
        assert (
            "/home/user/.ssh/config:/run/ssh-host/config:ro"
            in result["services"][svc]["volumes"]
        )


def test_build_ssh_override_known_hosts_path() -> None:
    result = json.loads(
        build_ssh_override(known_hosts_path="/home/user/.ssh/known_hosts")
    )
    for svc in AVAILABLE_SERVICES:
        assert (
            "/home/user/.ssh/known_hosts:/run/ssh-host/known_hosts:ro"
            in result["services"][svc]["volumes"]
        )


def test_build_ssh_override_no_optional_paths() -> None:
    result = json.loads(build_ssh_override())
    for svc in AVAILABLE_SERVICES:
        volumes = result["services"][svc]["volumes"]
        assert not any("/run/ssh-host/" in v for v in volumes)


def test_build_ssh_override_config_path_all_services() -> None:
    result = json.loads(build_ssh_override(config_path="/etc/ssh/config"))
    for svc in AVAILABLE_SERVICES:
        assert (
            "/etc/ssh/config:/run/ssh-host/config:ro"
            in result["services"][svc]["volumes"]
        )


def test_ssh_config_present_without_agent_raises(tmp_path, monkeypatch) -> None:
    """ConfigurationError is raised when [ssh] is set but SSH_AUTH_SOCK is unset."""
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("[ssh]\n")

    from agent_circus.context import build_compose_context

    with (
        pytest.raises(ConfigurationError, match="SSH_AUTH_SOCK"),
        build_compose_context(tmp_path),
    ):
        pass
