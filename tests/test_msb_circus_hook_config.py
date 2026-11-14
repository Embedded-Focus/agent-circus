"""Tests for ``[msb.hooks]`` parsing in msb-circus configuration."""

import logging
import re
from pathlib import Path

import pytest

from msb_circus.config import (
    MAX_HOOK_SCRIPT_BYTES,
    HookConfig,
    HooksConfig,
    load_hook_script,
    parse_config,
)
from msb_circus.exceptions import ConfigurationError

EXAMPLE_HOOK = {
    "script": "uv sync",
    "required": False,
    "timeout": 600,
    "workdir": "/workspace",
    "env": {"FOO": "bar"},
    "env_passthrough": ["NPM_TOKEN"],
}


def test_parse_hooks_absent_returns_empty():
    config = parse_config({})
    assert config.hooks == HooksConfig()


def test_parse_hooks_string_shorthand_sets_required_defaults():
    config = parse_config(
        {
            "msb": {
                "hooks": {
                    "provision": "apt-get install -y ripgrep",
                    "startup": "uv sync",
                }
            }
        }
    )
    assert config.hooks.provision == HookConfig(
        name="provision", script="apt-get install -y ripgrep", required=True
    )
    assert config.hooks.startup == HookConfig(
        name="startup", script="uv sync", required=False
    )


def test_parse_hooks_table_accepts_all_keys():
    config = parse_config({"msb": {"hooks": {"provision": EXAMPLE_HOOK}}})
    assert config.hooks.provision == HookConfig(
        name="provision",
        script="uv sync",
        file=None,
        required=False,
        timeout=600,
        workdir="/workspace",
        env={"FOO": "bar"},
        env_passthrough=("NPM_TOKEN",),
    )


def test_parse_hooks_table_required_overrides_default():
    config = parse_config(
        {"msb": {"hooks": {"startup": {"script": "uv sync", "required": True}}}}
    )
    assert config.hooks.startup is not None
    assert config.hooks.startup.required is True


def test_parse_hooks_script_and_file_together_raise():
    with pytest.raises(
        ConfigurationError,
        match=re.escape("msb.hooks.provision: set either script or file, not both"),
    ):
        parse_config(
            {
                "msb": {
                    "hooks": {
                        "provision": {"script": "a", "file": "hooks/provision.sh"}
                    }
                }
            }
        )


def test_parse_hooks_neither_script_nor_file_raises():
    with pytest.raises(
        ConfigurationError, match=re.escape("msb.hooks.provision needs script or file")
    ):
        parse_config({"msb": {"hooks": {"provision": {"required": True}}}})


def test_parse_hooks_unknown_hook_name_raises():
    with pytest.raises(
        ConfigurationError, match=re.escape("msb.hooks has unknown key(s): teardown")
    ):
        parse_config({"msb": {"hooks": {"teardown": "echo bye"}}})


def test_parse_hooks_unknown_key_raises():
    with pytest.raises(
        ConfigurationError,
        match=re.escape("msb.hooks.provision has unknown key(s): bogus, extra"),
    ):
        parse_config(
            {"msb": {"hooks": {"provision": {"script": "a", "bogus": 1, "extra": 2}}}}
        )


def test_parse_hooks_not_a_table_raises():
    with pytest.raises(
        ConfigurationError, match=re.escape("msb.hooks must be a table")
    ):
        parse_config({"msb": {"hooks": "nope"}})


def test_parse_hooks_wrong_value_type_raises():
    with pytest.raises(
        ConfigurationError,
        match=re.escape("msb.hooks.provision must be a string or a table"),
    ):
        parse_config({"msb": {"hooks": {"provision": ["echo", "hi"]}}})


@pytest.mark.parametrize("script", ["", "   \n"])
@pytest.mark.parametrize("form", ["string", "table"])
def test_parse_hooks_empty_script_raises(script, form):
    value = script if form == "string" else {"script": script}
    if form == "string":
        expected = "msb.hooks.startup must not be empty"
    else:
        expected = "msb.hooks.startup.script must be a non-empty string"
    with pytest.raises(ConfigurationError, match=re.escape(expected)):
        parse_config({"msb": {"hooks": {"startup": value}}})


@pytest.mark.parametrize("timeout", [0, -1, True, "10", 1.5])
def test_parse_hooks_invalid_timeout_raises(timeout):
    with pytest.raises(
        ConfigurationError,
        match=re.escape(
            "msb.hooks.provision.timeout must be a positive integer (seconds)"
        ),
    ):
        parse_config(
            {"msb": {"hooks": {"provision": {"script": "a", "timeout": timeout}}}}
        )


def test_parse_hooks_invalid_required_raises():
    with pytest.raises(
        ConfigurationError,
        match=re.escape("msb.hooks.provision.required must be true or false"),
    ):
        parse_config(
            {"msb": {"hooks": {"provision": {"script": "a", "required": "yes"}}}}
        )


def test_parse_hooks_invalid_env_name_raises():
    with pytest.raises(
        ConfigurationError,
        match=re.escape(
            "msb.hooks.provision.env key '1BAD' is not a valid environment variable name"
        ),
    ):
        parse_config(
            {"msb": {"hooks": {"provision": {"script": "a", "env": {"1BAD": "x"}}}}}
        )


@pytest.mark.parametrize("passthrough", ["FOO", [""]])
def test_parse_hooks_invalid_env_passthrough_raises(passthrough):
    with pytest.raises(
        ConfigurationError,
        match=re.escape(
            "msb.hooks.provision.env_passthrough must be a list of non-empty strings"
        ),
    ):
        parse_config(
            {
                "msb": {
                    "hooks": {
                        "provision": {"script": "a", "env_passthrough": passthrough}
                    }
                }
            }
        )


def test_parse_hooks_oversized_inline_script_raises():
    script = "x" * (MAX_HOOK_SCRIPT_BYTES + 1)
    with pytest.raises(
        ConfigurationError,
        match=re.escape("msb.hooks.provision script is larger than 64 KiB"),
    ):
        parse_config({"msb": {"hooks": {"provision": script}}})


def test_parse_hooks_exactly_max_inline_script_accepted():
    script = "x" * MAX_HOOK_SCRIPT_BYTES
    config = parse_config({"msb": {"hooks": {"provision": script}}})
    assert config.hooks.provision is not None
    assert config.hooks.provision.script == script


def test_load_hook_script_returns_inline_script():
    hook = HookConfig(name="startup", script="uv sync")
    assert load_hook_script(hook, Path(".")) == "uv sync"


def test_load_hook_script_reads_relative_file(tmp_path):
    hook_file = tmp_path / "hooks" / "provision.sh"
    hook_file.parent.mkdir()
    hook_file.write_text("#!/bin/sh\necho hi\n")
    hook = HookConfig(name="provision", file="hooks/provision.sh")
    assert load_hook_script(hook, tmp_path) == "#!/bin/sh\necho hi\n"


def test_load_hook_script_reads_absolute_file(tmp_path):
    hook_file = tmp_path / "absolute.sh"
    hook_file.write_text("echo abs\n")
    hook = HookConfig(name="startup", file=str(hook_file))
    assert load_hook_script(hook, tmp_path) == "echo abs\n"


def test_load_hook_script_missing_file_raises(tmp_path):
    hook = HookConfig(name="provision", file="hooks/missing.sh")
    with pytest.raises(
        ConfigurationError,
        match=re.escape(
            f"msb.hooks.provision.file '{tmp_path / 'hooks' / 'missing.sh'}' cannot be read"
        ),
    ):
        load_hook_script(hook, tmp_path)


def test_load_hook_script_oversized_file_raises(tmp_path):
    hook_file = tmp_path / "big.sh"
    hook_file.write_bytes(b"x" * (MAX_HOOK_SCRIPT_BYTES + 1))
    hook = HookConfig(name="startup", file="big.sh")
    with pytest.raises(
        ConfigurationError,
        match=re.escape(
            f"msb.hooks.startup.file '{tmp_path / 'big.sh'}' is larger than 64 KiB"
        ),
    ):
        load_hook_script(hook, tmp_path)


def test_load_hook_script_non_utf8_file_raises(tmp_path):
    hook_file = tmp_path / "binary.sh"
    hook_file.write_bytes(b"\xff\xfe")
    hook = HookConfig(name="provision", file="binary.sh")
    with pytest.raises(
        ConfigurationError,
        match=re.escape(
            f"msb.hooks.provision.file '{tmp_path / 'binary.sh'}' is not UTF-8 text"
        ),
    ):
        load_hook_script(hook, tmp_path)


def test_load_hook_script_empty_file_raises(tmp_path):
    hook_file = tmp_path / "empty.sh"
    hook_file.write_text("   \n")
    hook = HookConfig(name="startup", file="empty.sh")
    with pytest.raises(
        ConfigurationError,
        match=re.escape(f"msb.hooks.startup.file '{tmp_path / 'empty.sh'}' is empty"),
    ):
        load_hook_script(hook, tmp_path)


def test_parse_config_agent_circus_hooks_logs_hint(caplog):
    with caplog.at_level(logging.INFO):
        parse_config({"hooks": {"startup": "x"}})
    assert any(
        "[hooks] is an agent-circus setting" in record.message
        for record in caplog.records
    )


def test_parse_config_hooks_hint_not_logged_with_msb_hooks(caplog):
    with caplog.at_level(logging.INFO):
        parse_config({"hooks": {"startup": "x"}, "msb": {"hooks": {"startup": "y"}}})
    assert not any(
        "[hooks] is an agent-circus setting" in record.message
        for record in caplog.records
    )
