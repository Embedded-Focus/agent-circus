"""Tests for msb-circus port forwarding: config, builders, drift parsing."""

import pytest

from msb_circus.config import DEFAULT_PORT_HOST, PortForward, parse_config
from msb_circus.exceptions import ConfigurationError
from msb_circus.sandbox import (
    build_inspect_args,
    build_run_args,
    configured_ports,
    parse_inspect_ports,
    parse_inspect_ports_ordered,
)


def test_port_forward_defaults():
    forward = PortForward(guest_port=8000, host_port=8000)
    assert forward.host == DEFAULT_PORT_HOST == "127.0.0.1"
    assert forward.protocol == "tcp"


def test_parse_config_accepts_agent_circus_style_entries():
    config = parse_config(
        {
            "port_forwards": [
                {
                    "service": "codex",
                    "container_port": 3333,
                    "host_port": 3333,
                },
                {
                    "container_port": 8080,
                    "host_port": 18080,
                    "host": "0.0.0.0",
                    "protocol": "udp",
                },
            ]
        }
    )
    assert config.port_forwards == (
        PortForward(guest_port=3333, host_port=3333),
        PortForward(guest_port=8080, host_port=18080, host="0.0.0.0", protocol="udp"),
    )


def test_parse_config_accepts_guest_port_alias_and_defaults():
    config = parse_config({"port_forwards": [{"guest_port": 5173}]})
    assert config.port_forwards == (PortForward(guest_port=5173, host_port=5173),)


def test_port_forwards_is_supported_and_does_not_warn(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        parse_config({"port_forwards": [{"guest_port": 8000}]})
    assert not [r for r in caplog.records if "port_forwards" in r.getMessage()]


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"port_forwards": "8000:8000"}, "port_forwards must be a list"),
        ({"port_forwards": [42]}, "must be a table"),
        ({"port_forwards": [{}]}, "needs a guest_port"),
        (
            {"port_forwards": [{"guest_port": 0}]},
            "guest_port must be in range 1..65535",
        ),
        (
            {"port_forwards": [{"guest_port": 70000}]},
            "guest_port must be in range 1..65535",
        ),
        (
            {"port_forwards": [{"guest_port": 80, "host_port": "80"}]},
            "host_port must be an integer",
        ),
        (
            {"port_forwards": [{"guest_port": 80, "host": ""}]},
            "host must be a non-empty string",
        ),
        (
            {"port_forwards": [{"guest_port": 80, "protocol": "sctp"}]},
            "protocol must be 'tcp' or 'udp'",
        ),
    ],
)
def test_parse_config_rejects_malformed_port_forwards(raw, message):
    with pytest.raises(ConfigurationError, match=message):
        parse_config(raw)


def test_build_run_args_publishes_ports():
    args = build_run_args(
        name="agents-proj",
        image="img",
        port_forwards=(
            PortForward(guest_port=8000, host_port=8000),
            PortForward(
                guest_port=8080, host_port=18080, host="0.0.0.0", protocol="udp"
            ),
        ),
    )
    assert args == [
        "run",
        "--name",
        "agents-proj",
        "--memory",
        "4G",
        "--port",
        "127.0.0.1:8000:8000",
        "--port",
        "0.0.0.0:18080:8080/udp",
        "--detach",
        "img",
    ]


def test_build_run_args_without_ports_omits_flag():
    args = build_run_args(name="agents-proj", image="img")
    assert "--port" not in args


def test_build_inspect_args():
    assert build_inspect_args("agents-proj") == [
        "inspect",
        "agents-proj",
        "--format",
        "json",
    ]


def test_parse_inspect_ports_extracts_published_ports():
    # Trimmed from real `msb inspect --format json` output of msb 0.7.12:
    # `-p 8000:8000 -p 127.0.0.1:9229:9229 -p 5000-5002:5000-5002/udp`
    output = """
    {
      "config": {
        "network": {
          "enabled": true,
          "ports": [
            {"guest_port": 8000, "host_bind": "127.0.0.1", "host_port": 8000, "protocol": "tcp"},
            {"guest_port": 9229, "host_bind": "127.0.0.1", "host_port": 9229, "protocol": "tcp"},
            {"guest_port": 5000, "host_bind": "127.0.0.1", "host_port": 5000, "protocol": "udp"},
            {"guest_port": 5001, "host_bind": "127.0.0.1", "host_port": 5001, "protocol": "udp"},
            {"guest_port": 5002, "host_bind": "127.0.0.1", "host_port": 5002, "protocol": "udp"}
          ]
        }
      }
    }
    """
    assert parse_inspect_ports(output) == frozenset(
        {
            ("127.0.0.1", 8000, 8000, "tcp"),
            ("127.0.0.1", 9229, 9229, "tcp"),
            ("127.0.0.1", 5000, 5000, "udp"),
            ("127.0.0.1", 5001, 5001, "udp"),
            ("127.0.0.1", 5002, 5002, "udp"),
        }
    )


def test_parse_inspect_ports_falls_back_to_active_config():
    output = """
    {
      "active_config": {"network": {"ports": [
        {"guest_port": 80, "host_bind": "0.0.0.0", "host_port": 8080, "protocol": "tcp"}
      ]}}
    }
    """
    assert parse_inspect_ports(output) == frozenset({("0.0.0.0", 8080, 80, "tcp")})


def test_parse_inspect_ports_empty_when_omitted():
    assert (
        parse_inspect_ports('{"config": {"network": {"enabled": true}}}') == frozenset()
    )


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        "[]",
        '{"unexpected": true}',
        '{"config": {"network": {"ports": {}}}}',
        '{"config": {"network": {"ports": [{"host_port": "x"}]}}}',
        '{"config": {"network": {"ports": [42]}}}',
    ],
)
def test_parse_inspect_ports_rejects_malformed_output(output):
    with pytest.raises(ConfigurationError):
        parse_inspect_ports(output)


def test_parse_inspect_ports_ordered_preserves_msb_order():
    """The ordered variant keeps msb's order, unlike the set wrapper."""
    output = """
    {
      "config": {"network": {"ports": [
        {"guest_port": 9000, "host_bind": "127.0.0.1", "host_port": 9000, "protocol": "tcp"},
        {"guest_port": 8000, "host_bind": "127.0.0.1", "host_port": 8000, "protocol": "tcp"}
      ]}}
    }
    """
    assert parse_inspect_ports_ordered(output) == [
        ("127.0.0.1", 9000, 9000, "tcp"),
        ("127.0.0.1", 8000, 8000, "tcp"),
    ]


def test_parse_inspect_ports_ordered_keeps_duplicates():
    output = """
    {
      "config": {"network": {"ports": [
        {"guest_port": 8000, "host_bind": "127.0.0.1", "host_port": 8000, "protocol": "tcp"},
        {"guest_port": 8000, "host_bind": "127.0.0.1", "host_port": 8000, "protocol": "tcp"}
      ]}}
    }
    """
    entries = parse_inspect_ports_ordered(output)
    assert entries == [("127.0.0.1", 8000, 8000, "tcp")] * 2
    # The set wrapper collapses duplicates.
    assert parse_inspect_ports(output) == frozenset({("127.0.0.1", 8000, 8000, "tcp")})


def test_parse_inspect_ports_ordered_empty_when_omitted():
    assert parse_inspect_ports_ordered('{"config": {}}') == []


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        '{"config": {"network": {"ports": {}}}}',
        '{"config": {"network": {"ports": [42]}}}',
    ],
)
def test_parse_inspect_ports_ordered_rejects_malformed_output(output):
    with pytest.raises(ConfigurationError):
        parse_inspect_ports_ordered(output)


def test_configured_ports_matches_parsed_inspect_shape():
    forwards = (
        PortForward(guest_port=8000, host_port=8000),
        PortForward(guest_port=8080, host_port=18080, host="0.0.0.0", protocol="udp"),
    )
    assert configured_ports(forwards) == frozenset(
        {("127.0.0.1", 8000, 8000, "tcp"), ("0.0.0.0", 18080, 8080, "udp")}
    )
