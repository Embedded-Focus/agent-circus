"""Tests for ``[[msb.daemons]]`` parsing in msb-circus configuration."""

import re

import pytest

from msb_circus.config import DaemonConfig, parse_config
from msb_circus.exceptions import ConfigurationError

EXAMPLE_DAEMON = {
    "name": "opencode-web",
    "command": ["opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"],
    "autostart": True,
    "port": 4096,
    "workdir": "/workspace",
    "env": {"OPENCODE_LOG": "info"},
    "env_passthrough": ["OPENAI_API_KEY"],
}


def test_parse_config_without_daemons_returns_empty_tuple():
    config = parse_config({})
    assert config.daemons == ()


def test_parse_config_parses_full_daemon_example():
    config = parse_config({"msb": {"daemons": [EXAMPLE_DAEMON]}})
    assert config.daemons == (
        DaemonConfig(
            name="opencode-web",
            command=("opencode", "serve", "--hostname", "0.0.0.0", "--port", "4096"),
            autostart=True,
            port=4096,
            workdir="/workspace",
            env={"OPENCODE_LOG": "info"},
            env_passthrough=("OPENAI_API_KEY",),
        ),
    )


def test_parse_config_minimal_daemon_gets_defaults():
    config = parse_config(
        {
            "msb": {
                "daemons": [{"name": "web", "command": ["python", "-m", "http.server"]}]
            }
        }
    )
    assert config.daemons == (
        DaemonConfig(
            name="web",
            command=("python", "-m", "http.server"),
            autostart=True,
            port=None,
            workdir=None,
            env={},
            env_passthrough=(),
        ),
    )


def test_parse_config_keeps_daemon_order():
    config = parse_config(
        {
            "msb": {
                "daemons": [
                    {"name": "first", "command": ["a"]},
                    {"name": "second", "command": ["b"]},
                ]
            }
        }
    )
    assert [daemon.name for daemon in config.daemons] == ["first", "second"]


@pytest.mark.parametrize(
    ("daemons", "message"),
    [
        pytest.param(
            {"name": "web", "command": ["a"], "bogus": 1},
            "msb.daemons[0] has unknown key(s): bogus",
            id="unknown-key",
        ),
        pytest.param(
            {"name": "web", "command": ["a"], "extra": 1, "alsonot": 2},
            "msb.daemons[0] has unknown key(s): alsonot, extra",
            id="unknown-keys-sorted",
        ),
        pytest.param(
            {"command": ["a"]},
            "msb.daemons[0].name must match [a-z0-9][a-z0-9-]* (at most 40 characters)",
            id="name-missing",
        ),
        pytest.param(
            {"name": "Web", "command": ["a"]},
            "msb.daemons[0].name must match [a-z0-9][a-z0-9-]* (at most 40 characters)",
            id="name-uppercase",
        ),
        pytest.param(
            {"name": "-web", "command": ["a"]},
            "msb.daemons[0].name must match [a-z0-9][a-z0-9-]* (at most 40 characters)",
            id="name-leading-dash",
        ),
        pytest.param(
            {"name": "", "command": ["a"]},
            "msb.daemons[0].name must match [a-z0-9][a-z0-9-]* (at most 40 characters)",
            id="name-empty",
        ),
        pytest.param(
            {"name": "a" * 41, "command": ["a"]},
            "msb.daemons[0].name must match [a-z0-9][a-z0-9-]* (at most 40 characters)",
            id="name-too-long",
        ),
        pytest.param(
            {"name": "has space", "command": ["a"]},
            "msb.daemons[0].name must match [a-z0-9][a-z0-9-]* (at most 40 characters)",
            id="name-space",
        ),
        pytest.param(
            {"name": "web"},
            "msb.daemons[0].command must be a non-empty list of strings",
            id="command-missing",
        ),
        pytest.param(
            {"name": "web", "command": []},
            "msb.daemons[0].command must be a non-empty list of strings",
            id="command-empty",
        ),
        pytest.param(
            {"name": "web", "command": ["a", ""]},
            "msb.daemons[0].command must be a non-empty list of strings",
            id="command-empty-part",
        ),
        pytest.param(
            {"name": "web", "command": "a"},
            "msb.daemons[0].command must be a non-empty list of strings",
            id="command-not-list",
        ),
        pytest.param(
            {"name": "web", "command": ["a"], "env": {"A-B": "1"}},
            "msb.daemons[0].env key 'A-B' is not a valid environment variable name",
            id="env-key-invalid",
        ),
        pytest.param(
            {"name": "web", "command": ["a"], "autostart": "yes"},
            "msb.daemons[0].autostart must be true or false",
            id="autostart-not-bool",
        ),
        pytest.param(
            {"name": "web", "command": ["a"], "port": "80"},
            "msb.daemons[0].port must be an integer",
            id="port-not-integer",
        ),
        pytest.param(
            {"name": "web", "command": ["a"], "port": 0},
            "msb.daemons[0].port must be in range 1..65535",
            id="port-out-of-range",
        ),
        pytest.param(
            {"name": "web", "command": ["a"], "workdir": ""},
            "msb.daemons[0].workdir must be a non-empty string",
            id="workdir-empty",
        ),
        pytest.param(
            {"name": "web", "command": ["a"], "workdir": 3},
            "msb.daemons[0].workdir must be a non-empty string",
            id="workdir-not-string",
        ),
        pytest.param(
            {"name": "web", "command": ["a"], "env": "LOG=info"},
            "msb.daemons[0].env must be a table",
            id="env-not-table",
        ),
        pytest.param(
            {"name": "web", "command": ["a"], "env": {"LOG": 1}},
            "msb.daemons[0].env.LOG must be a string",
            id="env-value-not-string",
        ),
        pytest.param(
            {"name": "web", "command": ["a"], "env_passthrough": "KEY"},
            "msb.daemons[0].env_passthrough must be a list of non-empty strings",
            id="env-passthrough-not-list",
        ),
        pytest.param(
            {"name": "web", "command": ["a"], "env_passthrough": ["KEY", ""]},
            "msb.daemons[0].env_passthrough must be a list of non-empty strings",
            id="env-passthrough-empty-entry",
        ),
        pytest.param(
            [
                {"name": "web", "command": ["a"]},
                {"name": "web", "command": ["b"]},
            ],
            "msb.daemons[1].name 'web' is used more than once",
            id="duplicate-name",
        ),
        pytest.param(
            "nope",
            "msb.daemons must be a list of tables",
            id="daemons-not-list",
        ),
        pytest.param(
            ["nope"],
            "msb.daemons[0] must be a table",
            id="daemon-entry-not-table",
        ),
    ],
)
def test_parse_config_invalid_daemons_raise_configuration_error(
    daemons: object, message: str
):
    raw = [daemons] if isinstance(daemons, dict) else daemons
    with pytest.raises(ConfigurationError, match=re.escape(message)):
        parse_config({"msb": {"daemons": raw}})
