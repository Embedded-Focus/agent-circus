"""Tests for Git configuration compose override."""

import json

from agent_circus.config import AVAILABLE_SERVICES, build_git_override


def test_build_git_override_volume_spec() -> None:
    result = json.loads(build_git_override("/home/user/.gitconfig"))
    for svc in AVAILABLE_SERVICES:
        assert (
            "/home/user/.gitconfig:/run/git-host/config:ro"
            in result["services"][svc]["volumes"]
        )


def test_build_git_override_env_var() -> None:
    result = json.loads(build_git_override("/home/user/.gitconfig"))
    for svc in AVAILABLE_SERVICES:
        assert (
            result["services"][svc]["environment"]["GIT_CONFIG_GLOBAL"]
            == "/home/node/.gitconfig"
        )


def test_build_git_override_signing_key() -> None:
    result = json.loads(
        build_git_override("/home/user/.gitconfig", "/home/user/.ssh/id_ed25519.pub")
    )
    for svc in AVAILABLE_SERVICES:
        assert (
            "/home/user/.ssh/id_ed25519.pub:/run/git-host/signingkey.pub:ro"
            in result["services"][svc]["volumes"]
        )


def test_build_git_override_no_signing_key() -> None:
    result = json.loads(build_git_override("/home/user/.gitconfig"))
    for svc in AVAILABLE_SERVICES:
        volumes = result["services"][svc]["volumes"]
        assert not any("signingkey" in v for v in volumes)


def test_build_git_override_all_services() -> None:
    result = json.loads(build_git_override("/home/user/.gitconfig"))
    assert set(result["services"].keys()) == set(AVAILABLE_SERVICES)
