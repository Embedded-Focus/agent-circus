"""Tests for port forwarding compose overrides."""

import json

import pytest

from agent_circus.config import build_port_forwards_override
from agent_circus.exceptions import ConfigurationError


def test_build_port_forwards_override_minimal_entry() -> None:
    result = build_port_forwards_override(
        [{"service": "codex", "container_port": 3333}]
    )
    data = json.loads(result)

    assert data == {"services": {"codex": {"ports": ["127.0.0.1:3333:3333/tcp"]}}}


def test_build_port_forwards_override_custom_host_port_host_protocol() -> None:
    result = build_port_forwards_override(
        [
            {
                "service": "codex",
                "container_port": 8080,
                "host_port": 18080,
                "host": "0.0.0.0",
                "protocol": "udp",
            }
        ]
    )
    data = json.loads(result)

    assert data["services"]["codex"]["ports"] == ["0.0.0.0:18080:8080/udp"]


def test_build_port_forwards_override_groups_by_service() -> None:
    result = build_port_forwards_override(
        [
            {"service": "codex", "container_port": 3333},
            {"service": "codex", "container_port": 5173, "host_port": 15173},
        ]
    )
    data = json.loads(result)

    assert data["services"] == {
        "codex": {
            "ports": [
                "127.0.0.1:3333:3333/tcp",
                "127.0.0.1:15173:5173/tcp",
            ]
        }
    }


def test_build_port_forwards_override_multiple_services() -> None:
    result = build_port_forwards_override(
        [
            {"service": "codex", "container_port": 3333},
            {"service": "claude-code", "container_port": 5173},
        ]
    )
    data = json.loads(result)

    assert data["services"]["codex"]["ports"] == ["127.0.0.1:3333:3333/tcp"]
    assert data["services"]["claude-code"]["ports"] == ["127.0.0.1:5173:5173/tcp"]


def test_build_port_forwards_override_invalid_service() -> None:
    with pytest.raises(ConfigurationError):
        build_port_forwards_override([{"service": "missing", "container_port": 3333}])


@pytest.mark.parametrize("port", [0, 65536, "3333"])
def test_build_port_forwards_override_invalid_container_port(port: object) -> None:
    with pytest.raises(ConfigurationError):
        build_port_forwards_override([{"service": "codex", "container_port": port}])


@pytest.mark.parametrize("port", [0, 65536, "3333"])
def test_build_port_forwards_override_invalid_host_port(port: object) -> None:
    with pytest.raises(ConfigurationError):
        build_port_forwards_override(
            [{"service": "codex", "container_port": 3333, "host_port": port}]
        )


def test_build_port_forwards_override_invalid_protocol() -> None:
    with pytest.raises(ConfigurationError):
        build_port_forwards_override(
            [{"service": "codex", "container_port": 3333, "protocol": "http"}]
        )
