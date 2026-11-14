"""Tests for the guest hook shell scripts (``msb_circus.hooks``).

The generated scripts are really executed with the local ``sh`` against
a fake guest root under ``tmp_path`` (state dir, log dir, boot id file).
"""

from __future__ import annotations

import os
import subprocess

import pytest

from msb_circus.hooks import (
    ENV_PROVISION_HASH,
    ENV_STARTUP,
    EXIT_RUNNER_FAILED,
    EXIT_TIMEOUT,
    HookStatus,
    build_check_env,
    build_check_script,
    build_logs_command,
    build_run_args,
    build_run_env,
    build_run_script,
    build_status_script,
    hook_key_hash,
    log_path,
    parse_check_output,
    parse_status_output,
)


@pytest.fixture
def guest(tmp_path):
    """Return ``tmp_path`` with a fake boot id file in place."""
    (tmp_path / "boot_id").write_text("boot-1\n")
    return tmp_path


def run_hook(
    tmp_path,
    hook,
    content,
    *,
    key=None,
    timeout=None,
    force=False,
    extra_env=None,
) -> subprocess.CompletedProcess[str]:
    """Run the generated run script for one hook with the local ``sh``."""
    script = build_run_script(
        state_dir=str(tmp_path / "state"),
        log_dir=str(tmp_path / "log"),
        boot_id_file=str(tmp_path / "boot_id"),
    )
    env = {**os.environ, **build_run_env(content, key, timeout), **(extra_env or {})}
    return subprocess.run(
        ["sh", "-c", script, "sh", hook, "force" if force else "ensure"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def run_check(tmp_path, provision_hash, startup) -> subprocess.CompletedProcess[str]:
    """Run the generated check script with the local ``sh``."""
    script = build_check_script(
        state_dir=str(tmp_path / "state"),
        boot_id_file=str(tmp_path / "boot_id"),
    )
    env = {**os.environ, **build_check_env(provision_hash, startup)}
    return subprocess.run(
        ["sh", "-c", script], env=env, capture_output=True, text=True, check=False
    )


def run_status(tmp_path) -> subprocess.CompletedProcess[str]:
    """Run the generated status script with the local ``sh``."""
    script = build_status_script(
        state_dir=str(tmp_path / "state"),
        boot_id_file=str(tmp_path / "boot_id"),
    )
    return subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, check=False
    )


def state_file(tmp_path, hook):
    """Return the path of a hook's state file in the fake guest root."""
    return tmp_path / "state" / f"{hook}.state"


def test_hook_key_hash_is_stable_and_content_sensitive():
    a = hook_key_hash("echo one")
    assert a == hook_key_hash("echo one")
    assert a != hook_key_hash("echo two")
    assert len(a) == 64
    int(a, 16)  # hex digest


def test_build_check_env_includes_only_configured_hooks():
    assert build_check_env("abc", True) == {
        ENV_PROVISION_HASH: "abc",
        ENV_STARTUP: "1",
    }
    assert build_check_env(None, True) == {ENV_STARTUP: "1"}
    assert build_check_env("abc", False) == {ENV_PROVISION_HASH: "abc"}
    assert build_check_env(None, False) == {}


def test_check_script_reports_provision_new_without_state(guest):
    result = run_check(guest, hook_key_hash("echo hi"), startup=False)
    assert result.returncode == 0
    assert result.stdout.strip() == "provision new"


def test_check_script_reports_provision_changed_on_new_hash(guest):
    assert (
        run_hook(guest, "provision", "echo hi", key=hook_key_hash("echo hi")).returncode
        == 0
    )
    result = run_check(guest, hook_key_hash("echo new"), startup=False)
    assert result.stdout.strip() == "provision changed"


def test_check_script_reports_nothing_when_up_to_date(guest):
    hash_a = hook_key_hash("echo hi")
    assert run_hook(guest, "provision", "echo hi", key=hash_a).returncode == 0
    assert run_hook(guest, "startup", "echo hi").returncode == 0
    result = run_check(guest, hash_a, startup=True)
    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_check_script_reports_startup_due_on_new_boot_id(guest):
    hash_a = hook_key_hash("echo hi")
    assert run_hook(guest, "provision", "echo hi", key=hash_a).returncode == 0
    assert run_hook(guest, "startup", "echo hi").returncode == 0
    (guest / "boot_id").write_text("boot-2\n")
    result = run_check(guest, hash_a, startup=True)
    assert result.stdout.strip() == "startup due"


def test_parse_check_output_reads_known_lines_and_ignores_others():
    text = "\nprovision new\nnoise\nstartup due\nprovision bogus\nmore words here\nstartup changed\nprovision changed\n"
    assert parse_check_output(text) == {
        "provision": "changed",
        "startup": "due",
    }
    assert parse_check_output("") == {}
    assert parse_check_output("provision") == {}
    assert parse_check_output("boot due") == {}


def test_run_script_stores_hash_on_success(guest):
    hash_a = hook_key_hash("echo hi")
    result = run_hook(guest, "provision", "echo hi", key=hash_a)
    assert result.returncode == 0
    content = state_file(guest, "provision").read_text()
    assert f"key={hash_a}\n" in content
    assert "exit=0\n" in content
    assert "finished=" in content


def test_run_script_keeps_old_hash_on_failure(guest):
    hash_a = hook_key_hash("echo ok")
    hash_b = hook_key_hash("exit 3")
    assert run_hook(guest, "provision", "echo ok", key=hash_a).returncode == 0
    result = run_hook(guest, "provision", "echo ok\nexit 3", key=hash_b)
    assert result.returncode == 3
    content = state_file(guest, "provision").read_text()
    assert f"key={hash_a}\n" in content
    assert "exit=3\n" in content


def test_run_script_startup_stores_boot_id_on_failure(guest):
    result = run_hook(guest, "startup", "exit 7")
    assert result.returncode == 7
    content = state_file(guest, "startup").read_text()
    assert "key=boot-1\n" in content
    assert "exit=7\n" in content


def test_run_script_skips_when_not_due_and_not_forced(guest):
    marker = guest / "runs"
    hash_a = hook_key_hash(f"echo run >> {marker}")
    content = f"echo run >> {marker}"
    assert run_hook(guest, "provision", content, key=hash_a).returncode == 0
    assert marker.read_text().splitlines() == ["run"]
    result = run_hook(guest, "provision", content, key=hash_a)
    assert result.returncode == 0
    assert marker.read_text().splitlines() == ["run"]


def test_run_script_force_runs_when_up_to_date(guest):
    marker = guest / "runs"
    content = f"echo run >> {marker}"
    hash_a = hook_key_hash(content)
    assert run_hook(guest, "provision", content, key=hash_a).returncode == 0
    result = run_hook(guest, "provision", content, key=hash_a, force=True)
    assert result.returncode == 0
    assert marker.read_text().splitlines() == ["run", "run"]


def test_run_script_runs_shebang_scripts_directly(guest):
    result = run_hook(guest, "provision", "#!/bin/sh\necho $0\n", key="k")
    assert result.returncode == 0
    assert "provision.sh" in result.stderr


def test_run_script_uses_bash_without_shebang(guest):
    result = run_hook(guest, "provision", "[[ 1 == 1 ]] && echo bash-ok\n", key="k")
    assert result.returncode == 0
    assert "bash-ok" in result.stderr


def test_run_script_unsets_transport_variables(guest):
    result = run_hook(guest, "provision", 'echo "${CIRCUS_HOOK_B64:-unset}"\n', key="k")
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr.strip() == "unset"


def test_run_script_passes_user_environment(guest):
    result = run_hook(
        guest, "provision", 'echo "FOO=$FOO"\n', key="k", extra_env={"FOO": "bar"}
    )
    assert result.returncode == 0
    assert "FOO=bar" in result.stderr


def test_run_script_writes_output_to_log_and_stderr_only(guest):
    result = run_hook(guest, "provision", "echo out\necho err >&2\n", key="k")
    assert result.returncode == 0
    assert result.stdout == ""
    assert "out" in result.stderr
    assert "err" in result.stderr
    log = guest / "log" / "provision.log"
    assert log.read_text().splitlines() == ["out", "err"]


def test_run_script_timeout_exits_124(guest):
    result = run_hook(guest, "provision", "sleep 5\n", key="k", timeout=1)
    assert result.returncode == EXIT_TIMEOUT


def test_run_script_rejects_unknown_hook(guest):
    result = run_hook(guest, "bogus", "echo hi")
    assert result.returncode == EXIT_RUNNER_FAILED


def test_run_script_provision_without_key_fails(guest):
    result = run_hook(guest, "provision", "echo hi", key=None)
    assert result.returncode == EXIT_RUNNER_FAILED
    assert not state_file(guest, "provision").exists()


def test_build_run_args_rejects_unknown_hook():
    with pytest.raises(ValueError):
        build_run_args("bogus", force=False)


def test_build_run_args_uses_force_mode():
    ensured = build_run_args("startup", force=False)
    assert ensured[:4] == ["sh", "-c", build_run_script(), "sh"]
    assert ensured[4] == "startup"
    assert ensured[5] == "ensure"
    assert ensured[2] == build_run_script()
    forced = build_run_args("provision", force=True)
    assert forced[4] == "provision"
    assert forced[5] == "force"


def test_status_script_and_parser_round_trip(guest):
    hash_a = hook_key_hash("echo hi")
    assert run_hook(guest, "provision", "echo hi", key=hash_a).returncode == 0
    result = run_status(guest)
    assert result.returncode == 0
    boot_id, statuses = parse_status_output(result.stdout)
    assert boot_id == "boot-1"
    assert set(statuses) == {"provision"}
    status = statuses["provision"]
    assert status.name == "provision"
    assert status.key == hash_a
    assert status.exit_code == 0
    assert status.finished is not None


def test_parse_status_output_handles_empty_and_bad_values():
    assert parse_status_output("") == (None, {})
    boot_id, statuses = parse_status_output(
        "boot=\n"
        "provision key=ab12 exit=abc finished=\n"
        "bogus key=x exit=0 finished=1\n"
        "not enough fields\n"
        "startup key= exit=3 finished=1700000100\n"
    )
    assert boot_id is None
    assert set(statuses) == {"provision", "startup"}
    assert statuses["provision"].key == "ab12"
    assert statuses["provision"].exit_code is None
    assert statuses["provision"].finished is None
    assert statuses["startup"] == HookStatus("startup", "", 3, 1700000100)


def test_build_logs_command_with_and_without_follow():
    assert build_logs_command("provision", 10, False) == [
        "tail",
        "-n",
        "10",
        log_path("provision"),
    ]
    assert build_logs_command("startup", 5, True) == [
        "tail",
        "-n",
        "5",
        "-F",
        log_path("startup"),
    ]


def test_scripts_pass_sh_syntax_check():
    for build in (build_check_script, build_run_script, build_status_script):
        script = build()
        result = subprocess.run(
            ["sh", "-n", "-c", script], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr


def test_runner_env_names_avoid_reserved_msb_prefix():
    # msb rejects ``--env`` values whose names start with ``MSB_``.
    names = {
        *build_check_env("h", True),
        *build_run_env("echo hi", "k", 5),
    }
    assert names
    assert not [n for n in names if n.startswith("MSB_")]
