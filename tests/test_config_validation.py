"""Tests for config validation (unknown key warnings)."""

import logging

import pytest

from agent_circus.config import DEFAULT_CONFIG, validate_config


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
