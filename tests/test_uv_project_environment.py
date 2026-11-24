"""Tests for the injected ``UV_PROJECT_ENVIRONMENT`` (``[uv]`` table)."""

from agent_circus.config import UV_PROJECT_ENVIRONMENT_DEFAULT, parse_config
from agent_circus.envs import merged_env, uv_project_environment


def test_uv_project_environment_returns_default_by_default():
    config = parse_config({})
    assert uv_project_environment(config) == UV_PROJECT_ENVIRONMENT_DEFAULT


def test_uv_project_environment_returns_none_when_disabled():
    config = parse_config({"uv": {"project_environment": False}})
    assert uv_project_environment(config) is None


def test_uv_project_environment_returns_none_when_env_table_sets_it():
    config = parse_config({"env": {"UV_PROJECT_ENVIRONMENT": "/root/venvs/x"}})
    assert uv_project_environment(config) is None


def test_merged_env_injects_default_below_env_table():
    """The injected default is the lowest-precedence environment source."""
    config = parse_config({"env": {"A": "1"}})
    merged, shell_words = merged_env(config, env_passthrough=(), env={})
    assert merged["UV_PROJECT_ENVIRONMENT"] == UV_PROJECT_ENVIRONMENT_DEFAULT
    assert merged["A"] == "1"
    assert shell_words == set()


def test_merged_env_env_table_overrides_injected_default():
    config = parse_config({"env": {"UV_PROJECT_ENVIRONMENT": "/root/venvs/x"}})
    merged, _ = merged_env(config, env_passthrough=(), env={})
    assert merged["UV_PROJECT_ENVIRONMENT"] == "/root/venvs/x"


def test_merged_env_skips_injection_when_disabled():
    config = parse_config({"uv": {"project_environment": False}})
    merged, _ = merged_env(config, env_passthrough=(), env={})
    assert "UV_PROJECT_ENVIRONMENT" not in merged


def test_merged_env_injected_default_is_literal():
    """The default path is a literal value, not a guest shell word."""
    config = parse_config({})
    _, shell_words = merged_env(config, env_passthrough=(), env={})
    assert shell_words == set()
