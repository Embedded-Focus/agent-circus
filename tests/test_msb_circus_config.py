"""Tests for msb-circus configuration loading."""

import logging
from pathlib import Path

import pytest

from msb_circus.config import (
    effective_mounts,
    get_user_config_path,
    load_config,
    parse_config,
    sandbox_name_for_workspace,
    sanitize_project_name,
)
from msb_circus.exceptions import ConfigurationError


def test_parse_config_returns_defaults_for_empty_dict():
    config = parse_config({})
    assert config.binary is None
    assert config.image.tag == "docker.io/library/msb-agents:26"
    assert config.image.dockerfile is None
    assert config.sandbox.memory == "4G"
    assert config.sandbox.cpus is None
    assert config.network.mode == "public,private"
    assert config.agent.command == ("pi",)
    assert config.agent.workdir == "/workspace"
    assert config.mounts == {".": "/workspace"}
    assert config.env == {}
    assert config.env_passthrough == ()
    assert config.logging_level == "INFO"
    assert config.logging_file is None


def test_parse_config_interprets_supported_keys():
    config = parse_config(
        {
            "msb": {
                "binary": "/usr/local/bin/msb",
                "image": {"tag": "example:1", "platform": "linux/arm64"},
                "sandbox": {"memory": "8G", "cpus": 4},
                "network": {"mode": "none"},
                "agent": {"command": ["opencode", "acp"], "workdir": "/w"},
                "mounts": {".": "/workspace", "/data": "/data:ro"},
                "env": {"FOO": "bar"},
            },
            "env_passthrough": ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
            "logging": {"level": "DEBUG", "file": "/tmp/msbc.log"},
        }
    )
    assert config.binary == "/usr/local/bin/msb"
    assert config.image.tag == "example:1"
    assert config.image.platform == "linux/arm64"
    assert config.sandbox.memory == "8G"
    assert config.sandbox.cpus == 4
    assert config.network.mode == "none"
    assert config.agent.command == ("opencode", "acp")
    assert config.agent.workdir == "/w"
    assert config.mounts == {".": "/workspace", "/data": "/data:ro"}
    assert config.env == {"FOO": "bar"}
    assert config.env_passthrough == ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    assert config.logging_level == "DEBUG"
    assert config.logging_file == "/tmp/msbc.log"


def test_parse_config_warns_on_unsupported_keys(caplog):
    with caplog.at_level(logging.WARNING):
        parse_config({"services": ["pi"], "runtime": {"engine": "docker"}})
    messages = [record.getMessage() for record in caplog.records]
    assert any("services" in message for message in messages)
    assert any("runtime" in message for message in messages)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"msb": "nope"}, "msb must be a table"),
        ({"msb": {"binary": 1}}, "msb.binary must be a string"),
        ({"msb": {"image": {"tag": ""}}}, "msb.image.tag must be a non-empty string"),
        ({"msb": {"sandbox": {"memory": ""}}}, "msb.sandbox.memory"),
        ({"msb": {"sandbox": {"cpus": "four"}}}, "msb.sandbox.cpus"),
        ({"msb": {"agent": {"command": []}}}, "msb.agent.command"),
        ({"msb": {"mounts": {"a": 1}}}, "msb.mounts.a must be a string"),
        ({"env_passthrough": "KEY"}, "env_passthrough must be a list"),
        ({"env_passthrough": [1]}, "env_passthrough must be a list"),
        ({"logging": {"level": ""}}, "logging.level"),
    ],
)
def test_parse_config_rejects_malformed_values(raw, message):
    with pytest.raises(ConfigurationError, match=message):
        parse_config(raw)


def test_load_config_merges_user_global_and_project_layers(tmp_path, monkeypatch):
    user_dir = tmp_path / "userconfig"
    user_dir.mkdir()
    (user_dir / "agent-circus").mkdir()
    (user_dir / "agent-circus" / "config.toml").write_bytes(
        b'env_passthrough = ["GLOBAL"]\n[msb]\nbinary = "/global/msb"\n'
        b'[msb.sandbox]\nmemory = "2G"\n'
    )
    project_dir = tmp_path / "project"
    (project_dir / ".agent-circus").mkdir(parents=True)
    (project_dir / ".agent-circus" / "config.toml").write_bytes(
        b'env_passthrough = ["A", "B"]\n[msb.sandbox]\nmemory = "8G"\n'
    )
    environ = {"XDG_CONFIG_HOME": str(user_dir)}
    config = load_config(project_dir, environ)
    # Shallow top-level merge (agent-circus semantics): the project's [msb]
    # table and env_passthrough list replace the user-global ones wholesale.
    assert config.binary is None
    assert config.sandbox.memory == "8G"
    assert config.env_passthrough == ("A", "B")


def test_get_user_config_path_defaults_to_xdg_home():
    assert get_user_config_path({"XDG_CONFIG_HOME": "/cfg"}) == Path(
        "/cfg/agent-circus/config.toml"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("My Project", "my-project"),
        ("agent_circus.dev", "agent-circus-dev"),
        ("--Weird__Name--", "weird-name"),
        ("...", ""),
    ],
)
def test_sanitize_project_name(raw, expected):
    assert sanitize_project_name(raw) == expected


def test_sandbox_name_for_workspace(tmp_path):
    assert sandbox_name_for_workspace(tmp_path / "My Repo") == "agents-my-repo"


def test_sandbox_name_for_workspace_rejects_empty_project(tmp_path):
    with pytest.raises(ConfigurationError, match="Cannot derive a sandbox name"):
        sandbox_name_for_workspace(tmp_path / "...")


def test_parse_additional_dirs_with_defaults_and_explicit_fields():
    config = parse_config(
        {
            "additional_dirs": [
                {"path": "/home/user/shared-libs", "readonly": True},
                {"path": "/home/user/other-project", "name": "other"},
            ]
        }
    )
    assert config.additional_dirs[0].path == "/home/user/shared-libs"
    assert config.additional_dirs[0].name == "shared-libs"  # basename default
    assert config.additional_dirs[0].readonly is True
    assert config.additional_dirs[1].name == "other"
    assert config.additional_dirs[1].readonly is False


def test_additional_dirs_is_supported_and_does_not_warn(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        parse_config({"additional_dirs": [{"path": "/data"}]})
    assert not [r for r in caplog.records if "additional_dirs" in r.getMessage()]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"additional_dirs": "nope"}, "must be an array of tables"),
        ({"additional_dirs": [{}]}, "path must be a non-empty string"),
        ({"additional_dirs": [{"path": "relative/dir"}]}, "absolute path"),
        ({"additional_dirs": [{"path": "/d", "readonly": "yes"}]}, "boolean"),
        (
            {"additional_dirs": [{"path": "/d", "name": ""}]},
            "name must be a non-empty string",
        ),
        (
            {
                "additional_dirs": [
                    {"path": "/a", "name": "x"},
                    {"path": "/b", "name": "x"},
                ]
            },
            "duplicates an earlier entry",
        ),
        (
            {
                "additional_dirs": [
                    {"path": "/a"},
                    {"path": "/a"},
                ]
            },
            "duplicates an earlier entry",
        ),
    ],
)
def test_parse_additional_dirs_rejects_malformed_entries(raw, message):
    with pytest.raises(ConfigurationError, match=message):
        parse_config(raw)


def test_effective_mounts_appends_additional_dirs(tmp_path):
    config = parse_config(
        {
            "additional_dirs": [
                {"path": "/home/user/shared-libs", "readonly": True},
                {"path": "/home/user/other-project", "name": "other"},
            ]
        }
    )
    mounts = effective_mounts(config, tmp_path)
    assert mounts == [
        (str(tmp_path), "/workspace"),
        ("/home/user/shared-libs", "/workspaces/shared-libs:ro"),
        ("/home/user/other-project", "/workspaces/other"),
    ]


def test_effective_mounts_rejects_guest_target_collision(tmp_path):
    config = parse_config(
        {
            "msb": {"mounts": {"/srv/data": "/workspaces/shared-libs"}},
            "additional_dirs": [{"path": "/home/user/shared-libs"}],
        }
    )
    with pytest.raises(ConfigurationError, match="conflicts with the existing mount"):
        effective_mounts(config, tmp_path)
