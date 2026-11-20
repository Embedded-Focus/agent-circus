"""Tests for warnings about agent-circus 1.x settings."""

import logging
from pathlib import Path

import pytest

from agent_circus.config import _WARNED_KEYS, parse_config, warn_legacy_deploy_mode


@pytest.fixture(autouse=True)
def reset_warning_state() -> None:
    """Reset the once-per-process warning state before every test.

    :returns: None.
    """
    _WARNED_KEYS.clear()
    import agent_circus.config as config_module

    config_module._DEPLOY_MODE_WARNED = False


def test_legacy_key_warns_once(caplog: pytest.LogCaptureFixture) -> None:
    """A 1.x key is reported at warning level, exactly once per process."""
    with caplog.at_level(logging.WARNING, logger="agent_circus.config"):
        parse_config({"runtime": {"engine": "docker"}})
        parse_config({"runtime": {"engine": "docker"}})
    records = [r for r in caplog.records if "runtime" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "agent-circus 1.x" in records[0].getMessage()
    assert "MIGRATING.md" in records[0].getMessage()


def test_supported_keys_do_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    """Supported top-level keys produce no warning."""
    with caplog.at_level(logging.WARNING, logger="agent_circus.config"):
        parse_config({"msb": {}, "logging": {}, "env_passthrough": ["X"]})
    assert not [r for r in caplog.records if "MIGRATING.md" in r.getMessage()]


def test_ca_certs_gets_special_warning(caplog: pytest.LogCaptureFixture) -> None:
    """The ca_certs key keeps its dedicated message, now at warning level."""
    with caplog.at_level(logging.WARNING, logger="agent_circus.config"):
        parse_config({"ca_certs": {"patterns": ["x.crt"]}})
    records = [r for r in caplog.records if "trust_host_cas" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "MIGRATING.md" in records[0].getMessage()


def test_deploy_mode_file_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A 1.x deploy-mode compose.yaml is reported exactly once per process."""
    (tmp_path / ".agent-circus").mkdir()
    (tmp_path / ".agent-circus" / "compose.yaml").write_text("services: {}\n")
    with caplog.at_level(logging.WARNING, logger="agent_circus.config"):
        warn_legacy_deploy_mode(tmp_path)
        warn_legacy_deploy_mode(tmp_path)
    records = [r for r in caplog.records if "deploy mode" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert "MIGRATING.md" in records[0].getMessage()


def test_no_deploy_mode_file_no_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A workspace without the deploy-mode file produces no warning."""
    with caplog.at_level(logging.WARNING, logger="agent_circus.config"):
        warn_legacy_deploy_mode(tmp_path)
    assert not [r for r in caplog.records if "deploy mode" in r.getMessage()]
