"""Tests for the [git] forwarding: config parsing, helpers, guest script."""

import logging
import shutil
import subprocess
from pathlib import Path

import pytest

from msb_circus.config import parse_config
from msb_circus.exceptions import ConfigurationError
from msb_circus.exec_ import _gitconfig_script, build_write_gitconfig_args
from msb_circus.gitconfig import (
    GIT_GUEST_GLOBAL_CONFIG,
    GIT_GUEST_HOST_CONFIG,
    GIT_GUEST_MANAGED_CONFIG,
    GIT_GUEST_SIGNING_KEY,
    check_ssh_public_key,
    default_gitconfig_candidates,
    render_managed_gitconfig,
    resolve_host_gitconfig,
)

VALID_KEY = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleEx user@host"
)


def _git(config):
    """Return ``config.git``, asserting that the ``[git]`` table was set."""
    git = config.git
    assert git is not None
    return git


# ---------------------------------------------------------------- config


def test_parse_config_without_git_table_has_no_git_config():
    config = parse_config({})
    assert config.git is None
    assert config.git_worktree_mirror is False


def test_parse_config_empty_git_table_enables_git_config():
    config = parse_config({"git": {}})
    assert config.git is not None
    assert config.git.config_path is None
    assert config.git.signing_key_path is None
    assert config.git.worktree_mirror is False


def test_parse_config_expands_git_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = parse_config(
        {"git": {"config_path": "~/.gitconfig", "signing_key_path": "~/.ssh/id.pub"}}
    )
    assert config.git is not None
    assert config.git.config_path == str(tmp_path / ".gitconfig")
    assert config.git.signing_key_path == str(tmp_path / ".ssh" / "id.pub")


def test_parse_config_rejects_relative_git_config_path():
    with pytest.raises(ConfigurationError, match=r"git\.config_path"):
        parse_config({"git": {"config_path": "relative/gitconfig"}})


def test_parse_config_rejects_non_string_signing_key_path():
    with pytest.raises(ConfigurationError, match=r"git\.signing_key_path"):
        parse_config({"git": {"signing_key_path": 42}})


def test_parse_config_keeps_worktree_mirror_flag():
    config = parse_config({"git": {"worktree_mirror": True}})
    assert config.git_worktree_mirror is True
    assert config.git is not None
    assert config.git.worktree_mirror is True


# -------------------------------------------------------------- helpers


def test_default_gitconfig_candidates_order(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    candidates = default_gitconfig_candidates({})
    assert candidates[0] == tmp_path / ".gitconfig"
    assert candidates[1] == tmp_path / ".config" / "git" / "config"


def test_default_gitconfig_candidates_use_xdg_config_home(tmp_path):
    xdg = tmp_path / "xdg"
    candidates = default_gitconfig_candidates({"XDG_CONFIG_HOME": str(xdg)})
    assert candidates[1] == xdg / "git" / "config"


def test_resolve_host_gitconfig_explicit_path_found(tmp_path):
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Host\n")
    config = parse_config({"git": {"config_path": str(gitconfig)}})
    path, level, message = resolve_host_gitconfig(_git(config))
    assert path == gitconfig
    assert level is None and message is None


def test_resolve_host_gitconfig_explicit_path_missing_warns(tmp_path):
    config = parse_config({"git": {"config_path": str(tmp_path / "missing")}})
    path, level, message = resolve_host_gitconfig(_git(config))
    assert path is None
    assert level == logging.WARNING
    assert message is not None
    assert str(tmp_path / "missing") in message


def test_resolve_host_gitconfig_default_home_gitconfig_wins(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".gitconfig").write_text("[user]\n\temail = home@example.com\n")
    (home / ".config" / "git" / "config").write_text("[user]\n\temail = xdg\n")
    monkeypatch.setenv("HOME", str(home))
    config = parse_config({"git": {}})
    resolved = resolve_host_gitconfig(_git(config))
    assert resolved[0] == home / ".gitconfig"
    assert resolved[1] is None and resolved[2] is None


def test_resolve_host_gitconfig_xdg_used_when_no_home_gitconfig(tmp_path, monkeypatch):
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    (xdg / "git").mkdir(parents=True)
    (xdg / "git" / "config").write_text("[user]\n\temail = xdg\n")
    monkeypatch.setenv("HOME", str(home))
    config = parse_config({"git": {}})
    path, level, message = resolve_host_gitconfig(
        _git(config), {"XDG_CONFIG_HOME": str(xdg), "HOME": str(home)}
    )
    assert path == xdg / "git" / "config"
    assert level is None and message is None


def test_resolve_host_gitconfig_no_file_found_logs_info(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config = parse_config({"git": {}})
    path, level, message = resolve_host_gitconfig(_git(config), {"HOME": str(home)})
    assert path is None
    assert level == logging.INFO
    assert message is not None
    assert str(home / ".gitconfig") in message


def test_check_ssh_public_key_accepts_valid_key(tmp_path):
    key = tmp_path / "id.pub"
    key.write_text(VALID_KEY + "\n")
    assert check_ssh_public_key(key) is None


def test_check_ssh_public_key_accepts_security_key(tmp_path):
    key = tmp_path / "id.pub"
    key.write_text("sk-ssh-ed25519@openssh.com AAAAExample user@host\n")
    assert check_ssh_public_key(key) is None


def test_check_ssh_public_key_rejects_private_key(tmp_path):
    key = tmp_path / "id"
    key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END-----\n")
    reason = check_ssh_public_key(key)
    assert reason is not None
    assert "private" in reason


def test_check_ssh_public_key_rejects_two_keys(tmp_path):
    key = tmp_path / "id.pub"
    key.write_text(VALID_KEY + "\n" + VALID_KEY + "\n")
    reason = check_ssh_public_key(key)
    assert reason is not None
    assert "exactly one" in reason


def test_check_ssh_public_key_rejects_random_text(tmp_path):
    key = tmp_path / "id.pub"
    key.write_text("hello world\n")
    reason = check_ssh_public_key(key)
    assert reason is not None
    assert "OpenSSH" in reason


def test_check_ssh_public_key_rejects_missing_file(tmp_path):
    reason = check_ssh_public_key(tmp_path / "missing.pub")
    assert reason is not None
    assert "not found" in reason


def test_check_ssh_public_key_rejects_binary_bytes(tmp_path):
    key = tmp_path / "id.pub"
    key.write_bytes(b"\x00\xff\xfe\x00")
    reason = check_ssh_public_key(key)
    assert reason is not None
    assert "text" in reason


def test_render_managed_gitconfig_with_both_parts():
    text = render_managed_gitconfig(include_host_config=True, include_signing_key=True)
    assert text.startswith("# Managed by msb-circus")
    assert "[include]\n\tpath = /root/.config/msb-circus/git/host.gitconfig\n" in text
    assert (
        "[user]\n\tsigningkey = /root/.config/msb-circus/git/signingkey.pub\n" in text
    )
    assert text.endswith("\n")
    assert text.index("[include]") < text.index("[user]")


def test_render_managed_gitconfig_with_only_include():
    text = render_managed_gitconfig(include_host_config=True, include_signing_key=False)
    assert "[include]" in text
    assert "signingkey" not in text


def test_render_managed_gitconfig_with_only_signing_key():
    text = render_managed_gitconfig(include_host_config=False, include_signing_key=True)
    assert "[user]" in text
    assert "[include]" not in text


def test_render_managed_gitconfig_without_parts_raises():
    with pytest.raises(ValueError):
        render_managed_gitconfig(include_host_config=False, include_signing_key=False)


# --------------------------------------------------------- guest script


def _run_gitconfig_script(content: str, managed: Path, global_config: Path) -> None:
    """Run the guest gitconfig script locally against temporary paths.

    The script is plain POSIX sh and takes its paths as arguments, so it
    runs unchanged on the test host.

    :param content: Managed file content passed as ``$1``.
    :param managed: File the script treats as the managed gitconfig.
    :param global_config: File the script treats as the global gitconfig.
    """
    subprocess.run(
        [
            "sh",
            "-c",
            _gitconfig_script(),
            "sh",
            content,
            str(managed),
            str(global_config),
        ],
        check=True,
    )


def test_gitconfig_script_creates_global_config_with_include(tmp_path):
    managed = tmp_path / "managed" / "gitconfig"
    global_config = tmp_path / "global" / ".gitconfig"
    content = render_managed_gitconfig(
        include_host_config=True, include_signing_key=True
    )
    _run_gitconfig_script(content, managed, global_config)
    assert managed.read_text() == content
    text = global_config.read_text()
    assert text.startswith("# msb-circus: include the managed git config")
    assert f"\tpath = {managed}\n" in text


def test_gitconfig_script_prepends_include_and_keeps_content(tmp_path):
    managed = tmp_path / "managed" / "gitconfig"
    global_config = tmp_path / ".gitconfig"
    global_config.write_text("[user]\n\tname = Agent\n")
    content = render_managed_gitconfig(
        include_host_config=True, include_signing_key=True
    )
    _run_gitconfig_script(content, managed, global_config)
    text = global_config.read_text()
    assert text.startswith("# msb-circus: include the managed git config")
    assert text.index("msb-circus") < text.index("[user]\n\tname = Agent")
    assert "[user]\n\tname = Agent\n" in text


def test_gitconfig_script_is_idempotent(tmp_path):
    managed = tmp_path / "managed" / "gitconfig"
    global_config = tmp_path / ".gitconfig"
    content = render_managed_gitconfig(
        include_host_config=True, include_signing_key=True
    )
    _run_gitconfig_script(content, managed, global_config)
    before = global_config.read_text()
    _run_gitconfig_script(content, managed, global_config)
    assert global_config.read_text() == before
    assert before.count("[include]") == 1


def test_gitconfig_script_overwrites_managed_file(tmp_path):
    managed = tmp_path / "managed" / "gitconfig"
    global_config = tmp_path / ".gitconfig"
    _run_gitconfig_script("old content\n", managed, global_config)
    _run_gitconfig_script("new content\n", managed, global_config)
    assert managed.read_text() == "new content\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not on the test host")
def test_gitconfig_script_result_is_read_by_git(tmp_path):
    host_config = tmp_path / "host.gitconfig"
    host_config.write_text("[user]\n\temail = host@example.com\n")
    managed = tmp_path / "managed" / "gitconfig"
    global_config = tmp_path / ".gitconfig"
    content = (
        "# Managed by msb-circus\n"
        f"[include]\n\tpath = {host_config}\n"
        f"[user]\n\tsigningkey = {tmp_path / 'signingkey.pub'}\n"
    )
    _run_gitconfig_script(content, managed, global_config)
    result = subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(global_config),
            "--includes",
            "user.email",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "host@example.com"


# -------------------------------------------------------------- builder


def test_build_write_gitconfig_args_passes_paths_as_arguments():
    args = build_write_gitconfig_args("agents-x", "content\n")
    assert args[0] == "exec"
    assert "--tty" not in args and "--no-tty" not in args
    assert args[args.index("--") - 1] == "agents-x"
    after = args[args.index("--") + 1 :]
    assert after[0] == "sh" and after[1] == "-c"
    assert after[3:] == [
        "sh",
        "content\n",
        GIT_GUEST_MANAGED_CONFIG,
        GIT_GUEST_GLOBAL_CONFIG,
    ]


def test_builder_uses_gitconfig_constants():
    assert GIT_GUEST_HOST_CONFIG == "/root/.config/msb-circus/git/host.gitconfig"
    assert GIT_GUEST_SIGNING_KEY == "/root/.config/msb-circus/git/signingkey.pub"
