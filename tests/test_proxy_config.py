"""Tests for the expose setting and proxy helpers in agent-circus config."""

import pytest

from agent_circus.config import (
    DEFAULT_PROXY_PORT,
    PROXY_PORT_ENV_VAR,
    ROUTER_GUEST_PORT,
    DaemonConfig,
    exposed_routes,
    parse_config,
    proxy_url,
    resolve_proxy_port,
)
from agent_circus.exceptions import ConfigurationError


def test_parse_daemons_expose_defaults_to_true():
    config = parse_config(
        {"msb": {"daemons": [{"name": "web", "command": ["a"], "port": 4096}]}}
    )
    assert config.daemons[0].expose is True


def test_parse_daemons_expose_false_is_kept():
    config = parse_config(
        {
            "msb": {
                "daemons": [
                    {
                        "name": "web",
                        "command": ["a"],
                        "port": 4096,
                        "expose": False,
                    }
                ]
            }
        }
    )
    assert config.daemons[0].expose is False


def test_parse_daemons_expose_non_bool_raises():
    with pytest.raises(ConfigurationError, match=r"msb\.daemons\[0\]\.expose"):
        parse_config(
            {
                "msb": {
                    "daemons": [
                        {"name": "web", "command": ["a"], "port": 4096, "expose": 1}
                    ]
                }
            }
        )


def test_parse_daemons_explicit_expose_without_port_raises():
    with pytest.raises(ConfigurationError, match=r"msb\.daemons\[0\]\.expose"):
        parse_config(
            {"msb": {"daemons": [{"name": "web", "command": ["a"], "expose": True}]}}
        )


def test_parse_daemons_router_port_raises():
    with pytest.raises(
        ConfigurationError, match="reserved for the agent-circus router"
    ):
        parse_config(
            {
                "msb": {
                    "daemons": [
                        {"name": "web", "command": ["a"], "port": ROUTER_GUEST_PORT}
                    ]
                }
            }
        )


def test_parse_port_forwards_router_guest_port_raises():
    with pytest.raises(
        ConfigurationError, match="reserved for the agent-circus router"
    ):
        parse_config({"port_forwards": [{"guest_port": ROUTER_GUEST_PORT}]})


def test_parse_port_forwards_udp_router_guest_port_is_allowed():
    """Only TCP 7780 collides with the router; UDP is a different protocol."""
    config = parse_config(
        {
            "port_forwards": [
                {"guest_port": ROUTER_GUEST_PORT, "protocol": "udp"},
            ]
        }
    )
    assert config.port_forwards[0].protocol == "udp"


def test_exposed_routes_skips_daemons_without_port_or_expose():
    daemons = (
        DaemonConfig(name="web", command=("a",), port=4096),
        DaemonConfig(name="hidden", command=("a",), port=4097, expose=False),
        DaemonConfig(name="noport", command=("a",)),
    )
    assert exposed_routes(daemons) == {"web": 4096}


def test_resolve_proxy_port_precedence():
    assert resolve_proxy_port(8000, {}) == 8000
    assert resolve_proxy_port(None, {PROXY_PORT_ENV_VAR: "7701"}) == 7701
    assert resolve_proxy_port(None, {}) == DEFAULT_PROXY_PORT
    # CLI wins over the environment.
    assert resolve_proxy_port(8000, {PROXY_PORT_ENV_VAR: "7701"}) == 8000


@pytest.mark.parametrize("raw", ["abc", "0", "70000"])
def test_resolve_proxy_port_invalid_env_raises(raw):
    with pytest.raises(ConfigurationError, match=PROXY_PORT_ENV_VAR):
        resolve_proxy_port(None, {PROXY_PORT_ENV_VAR: raw})


def test_proxy_url_format():
    assert proxy_url("opencode-web", "agents-foo", 7700) == (
        "http://opencode-web.agents-foo.localhost:7700/"
    )
