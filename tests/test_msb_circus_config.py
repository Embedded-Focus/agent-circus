"""Tests for msb-circus configuration loading."""

import logging
from pathlib import Path

import pytest

from msb_circus.config import (
    SshConfig,
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
    assert config.sandbox.init == "auto"
    assert config.sandbox.stop_timeout == 45
    assert config.sandbox.docker_stop_timeout == 20
    assert config.network.mode == "public,private"
    assert config.agent.command == ("pi",)
    assert config.agent.workdir is None
    assert config.tracing.enabled is False
    assert config.tracing.directory == ".agent-circus/traces"
    assert config.tracing.sample_interval == pytest.approx(1.0)
    assert config.tracing.repetition_threshold == 64
    assert config.tracing.capture_payloads is False
    assert config.tracing.max_payload_bytes == 100 * 1024 * 1024
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
                "tracing": {
                    "enabled": True,
                    "directory": "diagnostics",
                    "sample_interval": 0.5,
                    "repetition_threshold": 20,
                    "capture_payloads": True,
                    "max_payload_bytes": 1234,
                },
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
    assert config.tracing.enabled is True
    assert config.tracing.directory == "diagnostics"
    assert config.tracing.sample_interval == pytest.approx(0.5)
    assert config.tracing.repetition_threshold == 20
    assert config.tracing.capture_payloads is True
    assert config.tracing.max_payload_bytes == 1234
    assert config.mounts == {".": "/workspace", "/data": "/data:ro"}
    assert config.env == {"FOO": "bar"}
    assert config.env_passthrough == ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    assert config.logging_level == "DEBUG"
    assert config.logging_file == "/tmp/msbc.log"


def test_parse_config_logs_unsupported_keys_at_debug(caplog):
    # Unsupported keys are shared agent-circus config; expecting them is
    # normal, so they only surface at debug level.
    with caplog.at_level(logging.DEBUG):
        parse_config({"services": ["pi"], "runtime": {"engine": "docker"}})
    messages = [record.getMessage() for record in caplog.records]
    assert any("services" in message for message in messages)
    assert any("runtime" in message for message in messages)
    assert all(record.levelno == logging.DEBUG for record in caplog.records)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"msb": "nope"}, "msb must be a table"),
        ({"msb": {"binary": 1}}, "msb.binary must be a string"),
        ({"msb": {"image": {"tag": ""}}}, "msb.image.tag must be a non-empty string"),
        ({"msb": {"sandbox": {"memory": ""}}}, "msb.sandbox.memory"),
        ({"msb": {"sandbox": {"cpus": "four"}}}, "msb.sandbox.cpus"),
        ({"msb": {"agent": {"command": []}}}, "msb.agent.command"),
        ({"msb": {"tracing": []}}, "msb.tracing must be a table"),
        (
            {"msb": {"tracing": {"sample_interval": 0}}},
            "msb.tracing.sample_interval",
        ),
        (
            {"msb": {"tracing": {"repetition_threshold": 1}}},
            "msb.tracing.repetition_threshold",
        ),
        (
            {"msb": {"tracing": {"max_payload_bytes": 0}}},
            "msb.tracing.max_payload_bytes",
        ),
        ({"msb": {"mounts": {"a": 1}}}, "msb.mounts.a must be a string"),
        ({"env_passthrough": "KEY"}, "env_passthrough must be a list"),
        ({"env_passthrough": [1]}, "env_passthrough must be a list"),
        ({"logging": {"level": ""}}, "logging.level"),
    ],
)
def test_parse_config_rejects_malformed_values(raw, message):
    with pytest.raises(ConfigurationError, match=message):
        parse_config(raw)


def test_parse_config_trust_host_cas_defaults_to_false():
    assert parse_config({}).network.trust_host_cas is False


def test_parse_config_reads_trust_host_cas():
    config = parse_config({"msb": {"network": {"trust_host_cas": True}}})
    assert config.network.trust_host_cas is True
    assert config.network.mode == "public,private"


def test_parse_config_rejects_non_bool_trust_host_cas():
    with pytest.raises(ConfigurationError, match="trust_host_cas"):
        parse_config({"msb": {"network": {"trust_host_cas": "yes"}}})


def test_parse_config_logs_info_for_ca_certs(caplog):
    with caplog.at_level(logging.INFO, logger="msb_circus.config"):
        parse_config({"ca_certs": {"patterns": ["x.crt"]}})
    records = [r for r in caplog.records if "trust_host_cas" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO


def test_parse_config_accepts_sandbox_init_values():
    assert parse_config({"msb": {"sandbox": {"init": "none"}}}).sandbox.init == "none"
    config = parse_config({"msb": {"sandbox": {"init": "/sbin/init"}}})
    assert config.sandbox.init == "/sbin/init"


@pytest.mark.parametrize("value", ["systemd", "", "relative/init"])
def test_parse_config_rejects_bad_sandbox_init(value):
    with pytest.raises(ConfigurationError, match=r"msb\.sandbox\.init"):
        parse_config({"msb": {"sandbox": {"init": value}}})


@pytest.mark.parametrize(
    "key",
    ["stop_timeout", "docker_stop_timeout"],
)
@pytest.mark.parametrize("value", [0, -1, "45", True])
def test_parse_config_rejects_bad_sandbox_timeouts(key, value):
    with pytest.raises(
        ConfigurationError, match=f"msb.sandbox.{key} must be a positive"
    ):
        parse_config({"msb": {"sandbox": {key: value}}})


def test_parse_config_rejects_stop_timeout_not_greater_than_docker():
    with pytest.raises(
        ConfigurationError,
        match=r"must be greater than msb\.sandbox\.docker_stop_timeout",
    ):
        parse_config(
            {"msb": {"sandbox": {"stop_timeout": 20, "docker_stop_timeout": 20}}}
        )


def test_parse_config_accepts_valid_sandbox_timeouts():
    config = parse_config(
        {"msb": {"sandbox": {"stop_timeout": 60, "docker_stop_timeout": 30}}}
    )
    assert config.sandbox.stop_timeout == 60
    assert config.sandbox.docker_stop_timeout == 30


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


def test_parse_ssh_absent_disables_forwarding():
    assert parse_config({}).ssh is None


def test_parse_ssh_empty_table_enables_forwarding():
    config = parse_config({"ssh": {}})
    assert config.ssh == SshConfig()


def test_parse_ssh_expands_home_in_paths(monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    config = parse_config(
        {
            "ssh": {
                "config_path": "~/.ssh/config",
                "known_hosts_path": "~/.ssh/known_hosts",
            }
        }
    )
    assert config.ssh is not None
    assert config.ssh.config_path == "/home/tester/.ssh/config"
    assert config.ssh.known_hosts_path == "/home/tester/.ssh/known_hosts"


def test_parse_ssh_rejects_relative_path():
    with pytest.raises(ConfigurationError, match="must be an absolute path"):
        parse_config({"ssh": {"config_path": "relative/config"}})


def test_parse_ssh_rejects_non_string_path():
    with pytest.raises(ConfigurationError, match="must be a non-empty string"):
        parse_config({"ssh": {"known_hosts_path": 42}})


def test_parse_ssh_rejects_non_table():
    with pytest.raises(ConfigurationError, match="ssh must be a table"):
        parse_config({"ssh": "yes"})


def test_parse_ssh_ignores_unknown_keys():
    config = parse_config({"ssh": {"unknown_key": 1}})
    assert config.ssh == SshConfig()


def test_parse_hosts_absent_disables_forwarding():
    assert parse_config({}).hosts is None


def test_parse_hosts_defaults_file_to_etc_hosts():
    config = parse_config({"hosts": {"patterns": ["*.corp"]}})
    assert config.hosts is not None
    assert config.hosts.patterns == ("*.corp",)
    assert config.hosts.file == "/etc/hosts"


def test_parse_hosts_rejects_non_list_patterns():
    with pytest.raises(ConfigurationError, match="must be a list of non-empty strings"):
        parse_config({"hosts": {"patterns": "*.corp"}})


def test_parse_hosts_rejects_invalid_regex():
    with pytest.raises(ConfigurationError, match="not a valid regular expression"):
        parse_config({"hosts": {"patterns": ["re:([unclosed"]}})


def test_parse_hosts_rejects_relative_file():
    with pytest.raises(
        ConfigurationError, match=r"hosts\.file must be an absolute path"
    ):
        parse_config({"hosts": {"file": "relative/hosts"}})


def test_parse_hosts_expands_home_in_file(monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    config = parse_config({"hosts": {"file": "~/.hosts"}})
    assert config.hosts is not None
    assert config.hosts.file == "/home/tester/.hosts"


def test_parse_config_agent_table_without_command_uses_default():
    config = parse_config({"msb": {"agent": {"workdir": "/w"}}})
    assert config.agent.command == ("pi",)
    assert config.agent.workdir == "/w"
