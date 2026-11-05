"""Tests for the additional_dirs compose override."""

import json

from agent_circus.config import (
    AVAILABLE_SERVICES,
    additional_dir_container_path,
    build_additional_dirs_override,
    build_claude_trust_additions,
)


def test_build_additional_dirs_override_readonly() -> None:
    dirs = [{"path": "/home/user/shared-libs", "readonly": True}]
    result = json.loads(build_additional_dirs_override(dirs))
    for svc in AVAILABLE_SERVICES:
        assert (
            "/home/user/shared-libs:/workspaces/shared-libs:ro"
            in result["services"][svc]["volumes"]
        )


def test_build_additional_dirs_override_readwrite() -> None:
    dirs = [{"path": "/home/user/other-project"}]
    result = json.loads(build_additional_dirs_override(dirs))
    for svc in AVAILABLE_SERVICES:
        assert (
            "/home/user/other-project:/workspaces/other-project:cached"
            in result["services"][svc]["volumes"]
        )


def test_build_additional_dirs_override_default_name() -> None:
    dirs = [{"path": "/some/deep/path/myrepo"}]
    result = json.loads(build_additional_dirs_override(dirs))
    volumes = result["services"]["claude-code"]["volumes"]
    assert any("/workspaces/myrepo" in v for v in volumes)


def test_build_additional_dirs_override_custom_name() -> None:
    dirs = [{"path": "/home/user/myrepo", "name": "custom-name"}]
    result = json.loads(build_additional_dirs_override(dirs))
    for svc in AVAILABLE_SERVICES:
        assert (
            "/home/user/myrepo:/workspaces/custom-name:cached"
            in result["services"][svc]["volumes"]
        )


def test_build_additional_dirs_override_multiple() -> None:
    dirs = [
        {"path": "/home/user/alpha", "readonly": True},
        {"path": "/home/user/beta"},
    ]
    result = json.loads(build_additional_dirs_override(dirs))
    volumes = result["services"]["claude-code"]["volumes"]
    assert "/home/user/alpha:/workspaces/alpha:ro" in volumes
    assert "/home/user/beta:/workspaces/beta:cached" in volumes


def test_build_additional_dirs_override_all_services() -> None:
    dirs = [{"path": "/tmp/extra"}]
    result = json.loads(build_additional_dirs_override(dirs))
    assert set(result["services"].keys()) == set(AVAILABLE_SERVICES)


def test_build_additional_dirs_override_empty() -> None:
    result = build_additional_dirs_override([])
    parsed = json.loads(result)
    for svc in AVAILABLE_SERVICES:
        assert parsed["services"][svc]["volumes"] == []


def test_additional_dir_container_path_defaults_to_basename() -> None:
    assert additional_dir_container_path({"path": "/a/b/myrepo"}) == (
        "/workspaces/myrepo"
    )


def test_additional_dir_container_path_uses_custom_name() -> None:
    entry = {"path": "/a/b/myrepo", "name": "lib"}
    assert additional_dir_container_path(entry) == "/workspaces/lib"


def test_build_claude_trust_additions_trusts_workspace_and_additional_dirs() -> None:
    dirs = [{"path": "/home/user/shared-libs"}, {"path": "/x/repo", "name": "lib"}]
    assert build_claude_trust_additions(dirs) == {
        "projects": {
            "/workspace": {"hasTrustDialogAccepted": True},
            "/workspaces/shared-libs": {"hasTrustDialogAccepted": True},
            "/workspaces/lib": {"hasTrustDialogAccepted": True},
        }
    }


def test_build_claude_trust_additions_without_additional_dirs() -> None:
    assert build_claude_trust_additions([]) == {
        "projects": {"/workspace": {"hasTrustDialogAccepted": True}}
    }
