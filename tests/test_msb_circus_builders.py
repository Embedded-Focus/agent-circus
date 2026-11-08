"""Tests for msb-circus argv builders (sandbox, exec, images, envs)."""

import pytest

from msb_circus.envs import redact_env, resolve_passthrough
from msb_circus.exceptions import ConfigurationError
from msb_circus.exec_ import build_exec_args
from msb_circus.images import (
    build_image_list_args,
    build_load_args,
    build_podman_build_args,
    build_podman_save_args,
)
from msb_circus.sandbox import (
    build_list_args,
    build_remove_args,
    build_run_args,
    build_start_args,
    build_stop_args,
)


def test_build_run_args_matches_documented_workflow():
    args = build_run_args(
        name="agents-agent-circus",
        image="docker.io/library/msb-agents:26",
        memory="4G",
        mounts=[(".", "/workspace")],
        net="public,private",
        workdir="/workspace",
    )
    assert args == [
        "run",
        "--name",
        "agents-agent-circus",
        "--memory",
        "4G",
        "--volume",
        ".:/workspace",
        "--net",
        "public,private",
        "--workdir",
        "/workspace",
        "--detach",
        "docker.io/library/msb-agents:26",
    ]


def test_build_run_args_includes_cpus_env_and_mount_options():
    args = build_run_args(
        name="agents-x",
        image="img:1",
        cpus=2,
        mounts=[("/data", "/data:ro")],
        env={"FOO": "bar"},
        detach=False,
    )
    assert args == [
        "run",
        "--name",
        "agents-x",
        "--memory",
        "4G",
        "--cpus",
        "2",
        "--volume",
        "/data:/data:ro",
        "--env",
        "FOO=bar",
        "img:1",
    ]


def test_build_list_args_variants():
    assert build_list_args() == ["list", "--running", "--format", "json"]
    assert build_list_args(running_only=False, json_format=False) == ["list"]


def test_build_stop_remove_start_args():
    assert build_stop_args("agents-x") == ["stop", "agents-x"]
    assert build_stop_args("agents-x", force=True) == ["stop", "--force", "agents-x"]
    assert build_start_args("agents-x") == ["start", "agents-x"]
    assert build_remove_args("agents-x") == ["remove", "agents-x"]
    assert build_remove_args("agents-x", force=True) == [
        "remove",
        "--force",
        "agents-x",
    ]


def test_build_exec_args_interactive_default():
    args = build_exec_args("agents-x", ["pi"])
    assert args == ["exec", "--tty", "agents-x", "--", "pi"]


def test_build_exec_args_acp_mode_uses_stream_quiet_without_tty():
    args = build_exec_args(
        "agents-x",
        ["opencode", "acp"],
        acp=True,
        env={"OPENAI_API_KEY": "sk"},
        workdir="/workspace",
    )
    assert args == [
        "exec",
        "--stream",
        "--quiet",
        "--env",
        "OPENAI_API_KEY=sk",
        "--workdir",
        "/workspace",
        "agents-x",
        "--",
        "opencode",
        "acp",
    ]
    assert "--tty" not in args
    assert "--no-stdin" not in args


def test_build_exec_args_rejects_empty_command():
    with pytest.raises(ValueError, match="must not be empty"):
        build_exec_args("agents-x", [])


def test_build_podman_build_args():
    args = build_podman_build_args(
        dockerfile="Dockerfile.msb-agents",
        tag="docker.io/library/msb-agents:26",
        platform="linux/amd64",
    )
    assert args == [
        "podman",
        "build",
        "--file",
        "Dockerfile.msb-agents",
        "--target",
        "agents",
        "--platform",
        "linux/amd64",
        "--tag",
        "docker.io/library/msb-agents:26",
        ".",
    ]


def test_build_podman_save_args_engines():
    podman = build_podman_save_args("img:1")
    assert podman == [
        "podman",
        "save",
        "--format",
        "docker-archive",
        "--output",
        "msb-agents.tar",
        "img:1",
    ]
    assert build_podman_save_args("img:1", "out.tar", engine="docker")[0] == "docker"
    with pytest.raises(ValueError, match="Unsupported engine"):
        build_podman_save_args("img:1", engine="nerdctl")


def test_build_load_args():
    assert build_load_args("msb-agents.tar") == ["load", "--input", "msb-agents.tar"]
    assert build_image_list_args() == ["image", "list", "--format", "json"]


def test_resolve_passthrough_collects_values_in_order():
    result = resolve_passthrough(("B", "A"), environ={"A": "1", "B": "2"})
    assert result == {"B": "2", "A": "1"}


def test_resolve_passthrough_fails_fast_on_missing():
    with pytest.raises(ConfigurationError, match="MISSING_ONE, MISSING_TWO"):
        resolve_passthrough(("MISSING_ONE", "MISSING_TWO"), environ={"PRESENT": "x"})


def test_resolve_passthrough_regex_prefix_expands_matches():
    result = resolve_passthrough(
        ("re:^LG_",), environ={"LG_APIKEY": "123", "OTHER": "x"}
    )
    assert result == {"LG_APIKEY": "123"}


def test_resolve_passthrough_regex_is_case_insensitive():
    result = resolve_passthrough(("re:^github_",), environ={"GITHUB_HOST": "x"})
    assert result == {"GITHUB_HOST": "x"}


def test_resolve_passthrough_regex_no_match_is_not_an_error():
    assert resolve_passthrough(("re:^NOMATCH_",), environ={"A": "1"}) == {}


def test_resolve_passthrough_glob_expands_matches():
    result = resolve_passthrough(("LG_*",), environ={"LG_APIKEY": "1", "HOME": "/h"})
    assert result == {"LG_APIKEY": "1"}


def test_resolve_passthrough_glob_no_match_is_not_an_error():
    assert resolve_passthrough(("NOMATCH_*",), environ={"A": "1"}) == {}


def test_resolve_passthrough_literal_names_still_required():
    with pytest.raises(ConfigurationError, match="TOKEN"):
        resolve_passthrough(("TOKEN",), environ={"TOKENX": "1"})


def test_resolve_passthrough_mixed_literals_and_patterns():
    result = resolve_passthrough(
        ("FIXED", "re:^LG_"), environ={"FIXED": "f", "LG_A": "1", "LG_B": "2"}
    )
    assert result == {"FIXED": "f", "LG_A": "1", "LG_B": "2"}


def test_redact_env_hides_values():
    assert redact_env({"KEY": "secret"}) == {"KEY": "<redacted>"}


def test_build_run_args_emits_vsock_routes():
    args = build_run_args(
        name="agents-x",
        image="img:1",
        vsock=[("/x/ssh-agent.sock", 22022)],
    )
    assert args[args.index("--vsock") + 1] == "/x/ssh-agent.sock:22022"
    assert args.count("--vsock") == 1


def test_build_run_args_without_vsock_has_no_vsock_flag():
    args = build_run_args(name="agents-x", image="img:1")
    assert "--vsock" not in args


def test_build_ssh_agent_probe_args_sets_guest_socket():
    from msb_circus.exec_ import build_ssh_agent_probe_args

    args = build_ssh_agent_probe_args("agents-x")
    assert args[0] == "exec"
    assert "--tty" not in args and "--no-tty" not in args
    assert args[args.index("--env") + 1] == "SSH_AUTH_SOCK=/run/ssh-agent.sock"
    assert args[-3:] == ["--", "ssh-add", "-l"]


def test_build_prepare_ssh_dir_args_creates_private_guest_dir():
    from msb_circus.exec_ import build_prepare_ssh_dir_args

    args = build_prepare_ssh_dir_args("agents-x")
    assert args[0] == "exec"
    assert args[3:5] == ["sh", "-c"]
    assert "mkdir -p /root/.ssh" in args[5]
    assert "chmod 700 /root/.ssh" in args[5]
