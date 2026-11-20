"""Tests for agent-circus configuration loading."""

import logging
from pathlib import Path

import pytest

from agent_circus.config import (
    _WARNED_KEYS,
    SshConfig,
    effective_mounts,
    get_user_config_path,
    load_config,
    parse_config,
    sandbox_name_for_workspace,
    sanitize_project_name,
)
from agent_circus.exceptions import ConfigurationError


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
            "env_passthrough": ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
            "logging": {"level": "DEBUG", "file": "/tmp/msbc.log"},
            "image": {"tag": "example:1", "platform": "linux/arm64"},
            "agent": {"command": ["opencode", "acp"], "workdir": "/w"},
            "tracing": {
                "enabled": True,
                "directory": "diagnostics",
                "sample_interval": 0.5,
                "repetition_threshold": 20,
                "capture_payloads": True,
                "max_payload_bytes": 1234,
            },
            "env": {"FOO": "bar"},
            "msb": {
                "binary": "/usr/local/bin/msb",
                "sandbox": {"memory": "8G", "cpus": 4},
                "network": {"mode": "none"},
                "mounts": {".": "/workspace", "/data": "/data:ro"},
            },
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


def test_parse_config_warns_on_legacy_keys_and_rejects_unknown(caplog):
    # Known 1.x keys surface as a once-per-process warning that names
    # MIGRATING.md; a truly unknown key (a typo) raises.
    _WARNED_KEYS.clear()
    with pytest.raises(ConfigurationError, match=r"unknown config key \[services\]"):
        parse_config({"services": ["pi"], "runtime": {"engine": "docker"}})
    _WARNED_KEYS.clear()
    with caplog.at_level(logging.WARNING):
        parse_config({"runtime": {"engine": "docker"}, "mcp_servers": []})
    messages = [record.getMessage() for record in caplog.records]
    assert any("runtime" in message for message in messages)
    assert any("mcp_servers" in message for message in messages)
    assert all(record.levelno == logging.WARNING for record in caplog.records)
    assert all("MIGRATING.md" in message for message in messages)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"msb": "nope"}, "msb must be a table"),
        ({"msb": {"binary": 1}}, "msb.binary must be a string"),
        ({"image": {"tag": ""}}, "image.tag must be a non-empty string"),
        ({"msb": {"sandbox": {"memory": ""}}}, "msb.sandbox.memory"),
        ({"msb": {"sandbox": {"cpus": "four"}}}, "msb.sandbox.cpus"),
        ({"agent": {"command": []}}, "agent.command"),
        ({"tracing": []}, "tracing must be a table"),
        (
            {"tracing": {"sample_interval": 0}},
            "tracing.sample_interval",
        ),
        (
            {"tracing": {"repetition_threshold": 1}},
            "tracing.repetition_threshold",
        ),
        (
            {"tracing": {"max_payload_bytes": 0}},
            "tracing.max_payload_bytes",
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


def test_parse_config_skills_store_defaults_to_none():
    assert parse_config({}).skills.store is None


def test_parse_config_reads_skills_store():
    config = parse_config({"skills": {"store": "/x"}})
    assert config.skills.store == "/x"


def test_parse_config_rejects_non_table_skills():
    with pytest.raises(ConfigurationError, match=r"skills must be a table"):
        parse_config({"skills": "/x"})


def test_parse_config_rejects_empty_skills_store():
    with pytest.raises(ConfigurationError, match="non-empty string"):
        parse_config({"skills": {"store": ""}})


@pytest.mark.parametrize("store", ["rel/path", "~no-such-user-agent-circus/x"])
def test_parse_config_rejects_invalid_skills_store(store):
    with pytest.raises(ConfigurationError, match=r"skills\.store"):
        parse_config({"skills": {"store": store}})


def test_parse_config_accepts_tilde_skills_store():
    config = parse_config({"skills": {"store": "~/skills"}})
    assert config.skills.store == "~/skills"


def test_parse_config_reads_trust_host_cas():
    config = parse_config({"msb": {"network": {"trust_host_cas": True}}})
    assert config.network.trust_host_cas is True
    assert config.network.mode == "public,private"


def test_parse_config_rejects_non_bool_trust_host_cas():
    with pytest.raises(ConfigurationError, match="trust_host_cas"):
        parse_config({"msb": {"network": {"trust_host_cas": "yes"}}})


def test_parse_config_warns_for_ca_certs(caplog):
    _WARNED_KEYS.clear()
    with caplog.at_level(logging.WARNING, logger="agent_circus.config"):
        parse_config({"ca_certs": {"patterns": ["x.crt"]}})
    records = [r for r in caplog.records if "trust_host_cas" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING


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
    # Per-key merge (D21): tables merge recursively, so the user's
    # msb.binary survives; arrays are replaced as a whole.
    assert config.binary == "/global/msb"
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
            "git": {"worktree_mirror": False},
            "additional_dirs": [
                {"path": "/home/user/shared-libs", "readonly": True},
                {"path": "/home/user/other-project", "name": "other"},
            ],
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


def test_parse_ssh_rejects_unknown_keys():
    with pytest.raises(
        ConfigurationError, match=r"ssh has unknown key\(s\): unknown_key"
    ):
        parse_config({"ssh": {"unknown_key": 1}})


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
    config = parse_config({"agent": {"workdir": "/w"}})
    assert config.agent.command == ("pi",)
    assert config.agent.workdir == "/w"


def test_load_config_reads_skills_store_from_user_config_only(tmp_path):
    """A user-global store survives a project [msb] table that shadows [msb]."""
    user_dir = tmp_path / "userconfig"
    (user_dir / "agent-circus").mkdir(parents=True)
    (user_dir / "agent-circus" / "config.toml").write_bytes(
        b'[skills]\nstore = "/user-store"\n'
    )
    project_dir = tmp_path / "project"
    (project_dir / ".agent-circus").mkdir(parents=True)
    (project_dir / ".agent-circus" / "config.toml").write_bytes(
        b'[msb.sandbox]\nmemory = "2G"\n'
    )
    environ = {"XDG_CONFIG_HOME": str(user_dir)}
    config = load_config(project_dir, environ)
    assert config.sandbox.memory == "2G"
    assert config.skills.store == "/user-store"


def test_load_config_rejects_project_skills_store(tmp_path):
    """A project [skills] store raises and names the user config path."""
    user_dir = tmp_path / "userconfig"
    (user_dir / "agent-circus").mkdir(parents=True)
    (user_dir / "agent-circus" / "config.toml").write_bytes(
        b'[skills]\nstore = "/user-store"\n'
    )
    project_dir = tmp_path / "project"
    (project_dir / ".agent-circus").mkdir(parents=True)
    (project_dir / ".agent-circus" / "config.toml").write_bytes(
        b'[skills]\nstore = "/project-store"\n'
    )
    environ = {"XDG_CONFIG_HOME": str(user_dir)}
    with pytest.raises(ConfigurationError, match="user config") as excinfo:
        load_config(project_dir, environ)
    assert str(user_dir / "agent-circus" / "config.toml") in str(excinfo.value)


def test_load_config_gives_no_skills_store_without_user_config(tmp_path):
    """No user config file: the store setting is the default (None)."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    environ = {"XDG_CONFIG_HOME": str(tmp_path / "no-user-config")}
    config = load_config(project_dir, environ)
    assert config.skills.store is None


@pytest.mark.parametrize(
    ("key", "table"),
    [
        ("image", r"\[image\]"),
        ("agent", r"\[agent\]"),
        ("tracing", r"\[tracing\]"),
        ("skills", r"\[skills\]"),
        ("daemons", r"\[\[daemons\]\]"),
        ("hooks", r"\[hooks\]"),
        ("env", r"\[env\]"),
    ],
)
def test_parse_config_rejects_moved_msb_keys(key, table):
    """A moved key under [msb] raises and names the new table."""
    with pytest.raises(
        ConfigurationError, match=f"msb.{key} is not supported; use {table}"
    ):
        parse_config({"msb": {key: {}}})


def test_parse_config_rejects_unknown_msb_key():
    with pytest.raises(ConfigurationError, match=r"msb\.foo is not a supported key"):
        parse_config({"msb": {"foo": 1}})


def test_parse_config_still_accepts_direct_msb_keys():
    config = parse_config(
        {
            "msb": {
                "binary": "/bin/msb",
                "sandbox": {"memory": "2G"},
                "network": {"trust_host_cas": True},
                "mounts": {".": "/workspace"},
            }
        }
    )
    assert config.binary == "/bin/msb"
    assert config.sandbox.memory == "2G"
    assert config.network.trust_host_cas is True
    assert config.mounts == {".": "/workspace"}


@pytest.mark.parametrize(
    ("table", "raw"),
    [
        ("image", {"bogus": 1}),
        ("agent", {"bogus": 1}),
        ("tracing", {"bogus": 1}),
        ("skills", {"bogus": 1}),
        ("msb.sandbox", {"bogus": 1}),
        ("msb.network", {"bogus": 1}),
        ("logging", {"bogus": 1}),
        ("git", {"bogus": 1}),
        ("ssh", {"bogus": 1}),
        ("hosts", {"bogus": 1}),
    ],
)
def test_parse_config_rejects_unknown_subtable_keys(table, raw):
    """Every strict table rejects unknown keys (D19)."""
    top_key = table.split(".")[-1]
    raw_config = {"msb": {top_key: raw}} if "." in table else {top_key: raw}
    with pytest.raises(ConfigurationError, match=f"{table} has unknown key"):
        parse_config(raw_config)


def test_parse_config_1x_key_still_warns_only(caplog):
    """A 1.x top-level key keeps the C7 warning instead of raising."""
    from agent_circus.config import _WARNED_KEYS

    _WARNED_KEYS.clear()
    with caplog.at_level(logging.WARNING, logger="agent_circus.config"):
        config = parse_config({"mcp_servers": [{"name": "x"}]})
    assert config.daemons == ()
    assert any("mcp_servers" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "key",
    [
        "shadow",
        "mcp_servers",
        "data_stores",
        "ca_certs",
        "runtime",
        "llama_cpp",
        "claude_mem",
        "host_credentials",
    ],
)
def test_parse_config_legacy_keys_warn_and_do_not_raise(key, caplog):
    """Each 1.x key warns (once) instead of raising."""
    from agent_circus.config import _WARNED_KEYS

    _WARNED_KEYS.clear()
    with caplog.at_level(logging.WARNING, logger="agent_circus.config"):
        parse_config({key: None})
    records = [r for r in caplog.records if key in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING


def test_parse_config_rejects_typo_top_level_key():
    with pytest.raises(ConfigurationError, match=r"unknown config key \[tracng\]"):
        parse_config({"tracng": {}})


def test_parse_config_rejects_unknown_port_forward_key():
    with pytest.raises(
        ConfigurationError, match=r"port_forwards\[0\] has unknown key\(s\): bogus"
    ):
        parse_config({"port_forwards": [{"guest_port": 8000, "bogus": 1}]})


def test_parse_config_rejects_guest_port_and_container_port():
    with pytest.raises(
        ConfigurationError, match=r"port_forwards\[0\]: set either guest_port"
    ):
        parse_config({"port_forwards": [{"guest_port": 8000, "container_port": 8001}]})


def test_parse_config_rejects_unknown_additional_dir_key():
    with pytest.raises(
        ConfigurationError, match=r"additional_dirs\[0\] has unknown key\(s\): seed"
    ):
        parse_config({"additional_dirs": [{"path": "/data", "seed": True}]})


def test_parse_config_accepts_1x_style_port_forward():
    """A full 1.x entry (service, container_port, host_port) still works."""
    config = parse_config(
        {
            "port_forwards": [
                {"service": "web", "container_port": 4096, "host_port": 4097}
            ]
        }
    )
    assert config.port_forwards[0].guest_port == 4096
    assert config.port_forwards[0].host_port == 4097


def test_merge_layers_merges_tables_and_keeps_user_keys(tmp_path):
    """User msb.binary applies alongside a project [msb.sandbox]."""
    user_dir = tmp_path / "userconfig"
    (user_dir / "agent-circus").mkdir(parents=True)
    (user_dir / "agent-circus" / "config.toml").write_bytes(
        b'[msb]\nbinary = "/user/msb"\n'
    )
    project_dir = tmp_path / "project"
    (project_dir / ".agent-circus").mkdir(parents=True)
    (project_dir / ".agent-circus" / "config.toml").write_bytes(
        b'[msb.sandbox]\nmemory = "2G"\n'
    )
    environ = {"XDG_CONFIG_HOME": str(user_dir)}
    config = load_config(project_dir, environ)
    assert config.binary == "/user/msb"
    assert config.sandbox.memory == "2G"


def test_merge_layers_env_keys_from_both_files_project_wins(tmp_path):
    """[env] merges per key; the project value wins for the same key."""
    user_dir = tmp_path / "userconfig"
    (user_dir / "agent-circus").mkdir(parents=True)
    (user_dir / "agent-circus" / "config.toml").write_bytes(
        b'[env]\nA = "user"\nB = "user"\n'
    )
    project_dir = tmp_path / "project"
    (project_dir / ".agent-circus").mkdir(parents=True)
    (project_dir / ".agent-circus" / "config.toml").write_bytes(
        b'[env]\nB = "project"\n'
    )
    environ = {"XDG_CONFIG_HOME": str(user_dir)}
    config = load_config(project_dir, environ)
    assert config.env == {"A": "user", "B": "project"}


def test_merge_layers_project_daemons_replace_user_array(tmp_path):
    """A [[daemons]] array in the project config replaces the user array."""
    user_dir = tmp_path / "userconfig"
    (user_dir / "agent-circus").mkdir(parents=True)
    (user_dir / "agent-circus" / "config.toml").write_bytes(
        b'[[daemons]]\nname = "user-daemon"\ncommand = ["sleep", "1"]\n'
    )
    project_dir = tmp_path / "project"
    (project_dir / ".agent-circus").mkdir(parents=True)
    (project_dir / ".agent-circus" / "config.toml").write_bytes(
        b'[[daemons]]\nname = "project-daemon"\ncommand = ["sleep", "2"]\n'
    )
    environ = {"XDG_CONFIG_HOME": str(user_dir)}
    config = load_config(project_dir, environ)
    assert [d.name for d in config.daemons] == ["project-daemon"]


def test_merge_layers_does_not_change_inputs():
    """The helper returns a new dict and leaves its inputs untouched."""
    from agent_circus.config import _merge_layers

    base = {"msb": {"binary": "/user/msb", "sandbox": {"memory": "4G"}}}
    override = {"msb": {"sandbox": {"cpus": 2}}}
    base_before = repr(base)
    override_before = repr(override)
    merged = _merge_layers(base, override)
    assert repr(base) == base_before
    assert repr(override) == override_before
    assert merged == {
        "msb": {"binary": "/user/msb", "sandbox": {"memory": "4G", "cpus": 2}}
    }
    assert merged["msb"] is not base["msb"]
