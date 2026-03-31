"""Tests for host environment variable pass-through."""

import json

from agent_circus.config import (
    AVAILABLE_SERVICES,
    build_env_passthrough_override,
    filter_env,
)

# ---------------------------------------------------------------------------
# filter_env
# ---------------------------------------------------------------------------


def test_filter_env_glob_match() -> None:
    result = filter_env(
        {"MY_CORP_TOKEN": "secret", "HOME": "/home/user"}, ["MY_CORP_*"]
    )
    assert "MY_CORP_TOKEN" in result
    assert "HOME" not in result


def test_filter_env_glob_no_match() -> None:
    result = filter_env({"HOME": "/home/user"}, ["MY_CORP_*"])
    assert result == []


def test_filter_env_regex_prefix() -> None:
    result = filter_env({"VAULT_TOKEN": "x", "OTHER": "y"}, ["re:^VAULT_"])
    assert "VAULT_TOKEN" in result
    assert "OTHER" not in result


def test_filter_env_case_insensitive() -> None:
    result = filter_env({"my_corp_token": "x"}, ["MY_CORP_*"])
    assert "my_corp_token" in result


def test_filter_env_empty_patterns() -> None:
    result = filter_env({"FOO": "bar"}, [])
    assert result == []


def test_filter_env_empty_environ() -> None:
    result = filter_env({}, ["FOO_*"])
    assert result == []


def test_filter_env_returns_sorted() -> None:
    result = filter_env({"Z_VAR": "1", "A_VAR": "2", "M_VAR": "3"}, ["*_VAR"])
    assert result == sorted(result)


def test_filter_env_values_not_in_result() -> None:
    # Result must be names only, not "NAME=value" strings
    result = filter_env({"SECRET_KEY": "topsecret"}, ["SECRET_*"])
    assert all("=" not in name for name in result)


# ---------------------------------------------------------------------------
# build_env_passthrough_override
# ---------------------------------------------------------------------------


def test_build_env_passthrough_override_structure() -> None:
    result = json.loads(build_env_passthrough_override(["FOO", "BAR"]))
    for svc in AVAILABLE_SERVICES:
        env = result["services"][svc]["environment"]
        assert isinstance(env, dict)
        assert env.get("FOO") is None
        assert env.get("BAR") is None


def test_build_env_passthrough_override_all_services() -> None:
    result = json.loads(build_env_passthrough_override(["FOO"]))
    assert set(result["services"].keys()) == set(AVAILABLE_SERVICES)


def test_build_env_passthrough_override_valid_json() -> None:
    output = build_env_passthrough_override(["A", "B"])
    assert isinstance(json.loads(output), dict)
