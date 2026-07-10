"""Tests for container runtime selection."""

import pytest

from agent_circus.exceptions import ConfigurationError
from agent_circus.runtime import compose_command, resolve_runtime


def test_resolve_runtime_defaults_to_docker() -> None:
    assert resolve_runtime(None, {}, env={}) == "docker"


def test_resolve_runtime_uses_config_when_no_cli_or_env() -> None:
    assert resolve_runtime(None, {"runtime": {"engine": "podman"}}, env={}) == "podman"


def test_resolve_runtime_env_overrides_config() -> None:
    assert (
        resolve_runtime(
            None,
            {"runtime": {"engine": "docker"}},
            env={"AGENT_CIRCUS_RUNTIME": "podman"},
        )
        == "podman"
    )


def test_resolve_runtime_cli_overrides_env() -> None:
    assert (
        resolve_runtime(
            "docker",
            {"runtime": {"engine": "podman"}},
            env={"AGENT_CIRCUS_RUNTIME": "podman"},
        )
        == "docker"
    )


def test_resolve_runtime_rejects_unsupported_runtime() -> None:
    with pytest.raises(ConfigurationError, match="Unsupported runtime"):
        resolve_runtime("containerd", {}, env={})


def test_compose_command_for_docker() -> None:
    assert compose_command("docker") == ["docker", "compose"]


def test_compose_command_for_podman() -> None:
    assert compose_command("podman") == ["podman", "compose"]
