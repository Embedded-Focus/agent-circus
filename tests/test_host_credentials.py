"""Tests for the ``[host_credentials]`` config.toml setting.

Covers validation, the per-agent Compose override shape, and the
host-side file auto-creation helper. Integration with
:func:`build_compose_context` (auth-file-only mounting, coexistence with
MCP merging and Claude-Mem) is covered in ``test_context.py`` and
``test_claude_mem.py``.
"""

import json
from pathlib import Path

import pytest

from agent_circus.config import (
    AGENT_CREDENTIAL_PATHS,
    build_host_credentials_override,
    get_host_credentials_services,
    validate_config,
)
from agent_circus.context import _ensure_host_credential_file
from agent_circus.exceptions import ConfigurationError


@pytest.mark.parametrize(
    ("host_credentials", "message"),
    [
        (True, "host_credentials must be a table"),
        ({"services": "codex"}, "must be a list of strings"),
        ({"services": [1]}, "must be a list of strings"),
        ({"services": ["not-a-service"]}, "unsupported service"),
    ],
)
def test_validate_host_credentials_rejects_unsupported_shapes(
    host_credentials: object,
    message: str,
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        validate_config({"mcp_servers": [], "host_credentials": host_credentials})


@pytest.mark.parametrize(
    "host_credentials",
    [None, {"services": []}, {"services": ["claude-code", "codex"]}],
)
def test_validate_host_credentials_accepts_valid_shapes(
    host_credentials: object,
) -> None:
    validate_config({"mcp_servers": [], "host_credentials": host_credentials})


def test_get_host_credentials_services_returns_empty_when_absent() -> None:
    assert get_host_credentials_services({}) == []
    assert get_host_credentials_services({"host_credentials": None}) == []


def test_get_host_credentials_services_returns_configured_list() -> None:
    config = {"host_credentials": {"services": ["codex", "opencode"]}}
    assert get_host_credentials_services(config) == ["codex", "opencode"]


@pytest.mark.parametrize(
    ("service", "expected_volume"),
    [
        (
            "claude-code",
            (
                "${HOME}/.claude/.credentials.json:"
                "/home/node/.claude/.credentials.json:cached"
            ),
        ),
        ("codex", "${HOME}/.codex/auth.json:/home/node/.codex/auth.json:cached"),
        ("mistral-vibe", "${HOME}/.vibe/.env:/home/node/.vibe/.env:cached"),
        (
            "opencode",
            (
                "${HOME}/.local/share/opencode/auth.json:"
                "/home/node/.local/share/opencode/auth.json:cached"
            ),
        ),
        (
            "pi",
            ("${HOME}/.pi/agent/auth.json:/home/node/.pi/agent/auth.json:cached"),
        ),
    ],
)
def test_build_host_credentials_override_mounts_single_auth_file(
    service: str,
    expected_volume: str,
) -> None:
    result = json.loads(build_host_credentials_override([service]))
    assert result["services"][service]["volumes"] == [expected_volume]


def test_build_host_credentials_override_covers_all_known_agents() -> None:
    assert set(AGENT_CREDENTIAL_PATHS) == {
        "claude-code",
        "codex",
        "mistral-vibe",
        "opencode",
        "pi",
    }


def test_build_host_credentials_override_handles_multiple_services() -> None:
    result = json.loads(build_host_credentials_override(["claude-code", "codex"]))
    assert set(result["services"]) == {"claude-code", "codex"}


def test_ensure_host_credential_file_creates_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    _ensure_host_credential_file("codex")

    auth_file = tmp_path / "home" / ".codex" / "auth.json"
    assert auth_file.is_file()
    assert auth_file.read_text() == ""
    assert oct(auth_file.stat().st_mode)[-3:] == "600"


def test_ensure_host_credential_file_does_not_clobber_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    auth_file = codex_dir / "auth.json"
    auth_file.write_text('{"OPENAI_API_KEY": "sk-existing"}')
    monkeypatch.setenv("HOME", str(home))

    _ensure_host_credential_file("codex")

    assert auth_file.read_text() == '{"OPENAI_API_KEY": "sk-existing"}'
