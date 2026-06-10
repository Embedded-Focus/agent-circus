"""Tests for config validation (unknown key warnings)."""

import logging

import pytest

from agent_circus.config import DEFAULT_CONFIG, validate_config
from agent_circus.exceptions import ConfigurationError


def test_validate_config_known_keys_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = DEFAULT_CONFIG.copy()
    with caplog.at_level(logging.WARNING, logger="agent_circus.config"):
        validate_config(config)
    assert caplog.records == []


def test_validate_config_unknown_key_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = {**DEFAULT_CONFIG, "typo_key": "oops"}
    with caplog.at_level(logging.WARNING, logger="agent_circus.config"):
        validate_config(config)
    assert any("typo_key" in msg for msg in caplog.messages)


def test_validate_config_multiple_unknown_keys_warns_for_each(
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = {**DEFAULT_CONFIG, "bad_a": 1, "bad_b": 2}
    with caplog.at_level(logging.WARNING, logger="agent_circus.config"):
        validate_config(config)
    messages = " ".join(caplog.messages)
    assert "bad_a" in messages
    assert "bad_b" in messages


def test_validate_config_accepts_all_mcp_server_kinds() -> None:
    validate_config(
        {
            **DEFAULT_CONFIG,
            "mcp_servers": [
                {"name": "managed", "image": "mcp/managed:latest"},
                {"name": "existing", "url": "https://mcp.example.com/mcp"},
                {
                    "name": "local",
                    "transport": "stdio",
                    "command": "local-mcp",
                    "args": ["serve"],
                    "env_vars": ["LOCAL_TOKEN"],
                },
            ],
        }
    )


@pytest.mark.parametrize(
    "server",
    [
        {"name": "missing"},
        {"name": "both", "image": "mcp/both:latest", "url": "http://x/mcp"},
    ],
)
def test_validate_config_requires_exactly_one_mcp_location(server: dict) -> None:
    with pytest.raises(ConfigurationError, match="exactly one"):
        validate_config({**DEFAULT_CONFIG, "mcp_servers": [server]})


def test_validate_config_rejects_non_network_external_url() -> None:
    with pytest.raises(ConfigurationError, match="http or https"):
        validate_config(
            {
                **DEFAULT_CONFIG,
                "mcp_servers": [{"name": "stdio", "url": "stdio://server"}],
            }
        )


@pytest.mark.parametrize(
    ("server", "message"),
    [
        ({"name": "stdio", "transport": "stdio"}, "command"),
        (
            {
                "name": "stdio",
                "transport": "stdio",
                "command": ["server"],
            },
            "command",
        ),
        (
            {
                "name": "stdio",
                "transport": "stdio",
                "command": "server",
                "image": "image",
            },
            "must not define",
        ),
        (
            {
                "name": "stdio",
                "transport": "stdio",
                "command": "server",
                "args": "serve",
            },
            "args",
        ),
        (
            {
                "name": "stdio",
                "transport": "stdio",
                "command": "server",
                "env_vars": "TOKEN",
            },
            "env_vars",
        ),
    ],
)
def test_validate_config_rejects_invalid_stdio_server(
    server: dict, message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        validate_config({**DEFAULT_CONFIG, "mcp_servers": [server]})
