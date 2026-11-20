"""Tests for agent-circus logs: sources, guest scripts, transform, CLI."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

import agent_circus.cli as cli_module
import agent_circus.logs as logs_module
from agent_circus.cli import app
from agent_circus.config import sandbox_name_for_workspace
from agent_circus.logs import (
    GUEST_LOG_SOURCES,
    KERNEL_SOURCE_NAME,
    build_follow_logs_script,
    build_log_files_script,
    build_logs_script,
    build_plain_follow_script,
    log_label,
    normalize_log_line,
    parse_log_files_output,
    resolve_log_sources,
)

runner = CliRunner()


class FakeInvoker:
    """Records invocations; scripted behavior for the logs command."""

    behavior: ClassVar[dict] = {}

    def __init__(self, binary) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        self.behavior.setdefault("calls", []).append((args, kwargs))
        if args[0] == "list":
            running = self.behavior.get("running", True)
            payload = (
                [{"name": self.behavior["sandbox_name"], "status": "Running"}]
                if running
                else []
            )
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        cmd = args[args.index("--") + 1 :]
        if cmd[0] == "tail":
            self.behavior["tail_argv"] = cmd
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        script = cmd[2] if cmd[0] == "sh" else ""
        # Unique markers: the discovery script checks readability with
        # `[ -r`, the follow scripts run `tail -F`.
        if '[ -r "$f" ]' in script:
            return SimpleNamespace(
                returncode=0,
                stdout=self.behavior.get("files_stdout", ""),
                stderr="",
            )
        if "tail -F" in script:
            self.behavior["follow_script"] = script
            self.behavior["follow_args"] = cmd[4:]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=self.behavior.get("exec_rc", 0),
            stdout=self.behavior.get("exec_stdout", ""),
            stderr="",
        )


@pytest.fixture
def fake_msb(tmp_path, monkeypatch):
    """Patch the CLI invoker; provide a workspace with an empty config."""
    FakeInvoker.behavior = {"sandbox_name": sandbox_name_for_workspace(tmp_path)}
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(b"")
    monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: Path("/fake"))
    monkeypatch.setattr(cli_module, "check_version", lambda binary: "0.7.12")
    monkeypatch.setattr(cli_module, "MsbInvoker", FakeInvoker)
    return FakeInvoker.behavior


def test_resolve_log_sources_defaults_to_all_sources_in_presentation_order():
    sources = resolve_log_sources(None)
    assert [source.name for source in sources] == [
        "kernel",
        "bridge",
        "dockerd",
        "hooks",
        "daemons",
    ]


def test_resolve_log_sources_ignores_argument_order():
    sources = resolve_log_sources(["daemons", "dockerd"])
    assert [source.name for source in sources] == ["dockerd", "daemons"]


def test_resolve_log_sources_selects_named_sources_and_kernel():
    sources = resolve_log_sources(["dockerd", KERNEL_SOURCE_NAME])
    assert [source.name for source in sources] == ["kernel", "dockerd"]


def test_resolve_log_sources_kernel_alone_selects_only_the_kernel():
    sources = resolve_log_sources(["kernel"])
    assert [source.name for source in sources] == ["kernel"]
    assert sources[0].pattern is None


def test_resolve_log_sources_rejects_unknown_names():
    with pytest.raises(ValueError, match="nope"):
        resolve_log_sources(["dockerd", "nope"])


def test_normalize_log_line_moves_s6_stamp_in_front():
    line = "[daemon.web] 2026-11-15 10:43:39.777605362  server listening"
    assert normalize_log_line(line) == (
        "2026-11-15T10:43:39.777605362+00:00 [daemon.web] server listening"
    )


def test_normalize_log_line_moves_s6_stamp_without_label():
    line = "2026-11-15 10:43:39.777605362  server listening"
    assert normalize_log_line(line) == (
        "2026-11-15T10:43:39.777605362+00:00 server listening"
    )


def test_normalize_log_line_strips_logrus_time_prefix():
    line = '[dockerd] time="2026-11-15T10:43:26.178716430Z" level=info msg="up"'
    assert normalize_log_line(line) == (
        '2026-11-15T10:43:26.178716430Z [dockerd] level=info msg="up"'
    )


def test_normalize_log_line_leaves_kernel_and_unstamped_lines_alone():
    kernel = "[kernel] [    0.079527] Run /init.krun as init process"
    assert normalize_log_line(kernel) == kernel
    plain = "[bridge] --- circus-ssh-agent-bridge start 2026-11-15T10:43:25+00:00"
    assert normalize_log_line(plain) == plain


def test_normalize_log_line_leaves_ambiguous_partial_matches_alone():
    assert normalize_log_line("[daemon.web] 2026-11-15") == "[daemon.web] 2026-11-15"
    assert normalize_log_line("") == ""


def test_build_logs_script_labels_fixed_and_glob_sources():
    script = build_logs_script(GUEST_LOG_SOURCES, 50)
    assert "LINES=50" in script
    assert "prefix '[dockerd]' /tmp/dockerd.log" in script
    assert "prefix '[bridge]' /tmp/ssh-agent-bridge.log" in script
    assert "for f in /var/log/circus-daemons/*/current; do" in script
    assert 'prefix "[daemon.$n]" "$f"' in script
    assert "for f in /var/log/agent-circus/*.log; do" in script
    assert 'prefix "[hook.$n]" "$f"' in script
    # Labels replace the old section headers entirely; the kernel is not
    # part of the file-source tuple.
    assert "== " not in script
    assert "dmesg" not in script
    # No awk: attribution happens with shell read loops.
    assert "awk" not in script


def test_build_logs_script_prefix_helper_uses_a_read_loop():
    script = build_logs_script(GUEST_LOG_SOURCES, 10)
    assert 'tail -n "$LINES" -- "$2" 2>/dev/null | while IFS= read -r line' in script
    assert 'printf \'%s %s\\n\' "$1" "$line"' in script


def test_build_logs_script_defaults_include_the_kernel_first():
    script = build_logs_script(resolve_log_sources(None), 10)
    # The kernel block comes before the file sources (boot order).
    assert script.index("dmesg") < script.index("/tmp/dockerd.log")
    kernel_line = next(line for line in script.splitlines() if "dmesg 2>" in line)
    assert "'[kernel]' \"$line\"" in kernel_line
    assert "prefix '[dockerd]' /tmp/dockerd.log" in script


def test_build_logs_script_without_labels_prints_tail_style_headers():
    script = build_logs_script(GUEST_LOG_SOURCES, 10, labels=False)
    assert "awk" not in script
    assert 'echo "==> $1 <=="' in script
    assert "plain /tmp/dockerd.log" in script
    assert "plain /tmp/ssh-agent-bridge.log" in script
    assert 'plain "$f"' in script


def test_build_logs_script_without_labels_headers_the_kernel():
    script = build_logs_script(resolve_log_sources(["kernel"]), 10, labels=False)
    assert 'echo "==> kernel (dmesg) <=="' in script
    assert "awk" not in script


def test_build_logs_script_forces_minimum_one_line():
    assert "LINES=1" in build_logs_script(GUEST_LOG_SOURCES, 0)


def test_build_log_files_script_lists_patterns_in_order():
    script = build_log_files_script(GUEST_LOG_SOURCES)
    lines = script.splitlines()
    patterns = [line for line in lines if line.startswith("for f in")]
    assert patterns == [
        (
            "for f in /tmp/ssh-agent-bridge.log /tmp/dockerd.log"
            " /var/log/agent-circus/*.log /var/log/circus-daemons/*/current; do"
        )
    ]


def test_log_label_for_fixed_paths():
    assert log_label("/tmp/dockerd.log") == "[dockerd]"
    assert log_label("/tmp/ssh-agent-bridge.log") == "[bridge]"


def test_log_label_for_glob_paths_derives_the_wildcard_match():
    assert log_label("/var/log/circus-daemons/opencode-web/current") == (
        "[daemon.opencode-web]"
    )
    assert log_label("/var/log/agent-circus/provision.log") == "[hook.provision]"


def test_log_label_falls_back_to_the_basename_for_unknown_paths():
    assert log_label("/var/log/other/tool.log") == "[tool.log]"


def test_build_follow_logs_script_runs_one_labeled_read_loop_pipeline_per_pair():
    script = build_follow_logs_script(
        [("[dockerd]", "/tmp/dockerd.log"), ("[daemon.web]", "/var/log/w/current")],
        5,
    )
    assert "LINES=5" in script
    assert 'for x in "$@"; do' in script
    assert "l=${x%%|*}" in script
    assert "f=${x#*|}" in script
    assert 'tail -F -n "$LINES" -- "$f" 2>/dev/null | while IFS= read -r line' in script
    assert "dmesg" not in script
    assert "awk" not in script
    assert script.rstrip().endswith("wait")


def test_build_follow_logs_script_appends_a_kernel_pipeline_when_requested():
    script = build_follow_logs_script(
        [("[dockerd]", "/tmp/dockerd.log")], 5, kernel=True
    )
    # Two stages: a limited snapshot, then only new records.
    assert 'dmesg 2>/dev/null | tail -n "$LINES" | while IFS= read -r line' in script
    assert "dmesg --follow-new 2>/dev/null" in script
    assert "printf '%s %s\\n' '[kernel]' \"$line\"" in script
    # Never an endless stream into initial tail logic.
    assert "dmesg --follow | tail" not in script
    # The trap stops the recorded background jobs.
    assert "trap 'for p in $PIDS; do kill" in script
    assert 'PIDS="$PIDS $!"' in script
    assert script.rstrip().endswith("wait")


def test_build_plain_follow_script_tails_files_and_the_kernel():
    script = build_plain_follow_script(7)
    assert "LINES=7" in script
    assert 'if [ "$#" -gt 0 ]; then' in script
    assert 'tail -F -n "$LINES" -- "$@" &' in script
    # Two kernel stages, never an endless stream into tail logic.
    assert 'dmesg 2>/dev/null | tail -n "$LINES" &' in script
    assert "dmesg --follow-new 2>/dev/null &" in script
    assert "dmesg --follow | tail" not in script
    assert "trap 'for p in $PIDS; do kill" in script
    assert script.rstrip().endswith("wait")


def test_build_plain_follow_script_without_kernel_skips_dmesg():
    script = build_plain_follow_script(7, kernel=False)
    assert "dmesg" not in script
    assert script.rstrip().endswith("wait")


def test_parse_log_files_output_keeps_order_and_dedupes():
    text = "/tmp/a.log\n\n/tmp/b.log\n/tmp/a.log\n"
    assert parse_log_files_output(text) == ["/tmp/a.log", "/tmp/b.log"]


def test_parse_log_files_output_empty_text():
    assert parse_log_files_output("") == []


def test_stream_logs_transforms_lines_and_returns_exit_code(capsys):
    returncode = cli_module._stream_logs(
        ["echo", "[daemon.web] 2026-11-15 10:43:39.777605362  hi"],
        normalize_log_line,
    )
    assert returncode == 0
    assert capsys.readouterr().out == (
        "2026-11-15T10:43:39.777605362+00:00 [daemon.web] hi\n"
    )


def test_stream_logs_propagates_nonzero_exit_code(capsys):
    assert cli_module._stream_logs(["sh", "-c", "echo out; exit 3"], str) == 3
    assert capsys.readouterr().out == "out\n"


def test_stream_logs_reports_missing_binary_as_command_error():
    with pytest.raises(cli_module.MsbCommandError):
        cli_module._stream_logs(
            ["/definitely/not/a/binary"], logs_module.normalize_log_line
        )


def test_logs_list_prints_kernel_first_without_a_sandbox(tmp_path, fake_msb):
    result = runner.invoke(app, ["logs", "--list", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert result.output.index("kernel") < result.output.index("dockerd")
    assert "/tmp/dockerd.log" in result.output
    assert "--kernel" not in result.output
    assert "calls" not in FakeInvoker.behavior


def test_logs_prints_script_output(tmp_path, fake_msb):
    fake_msb["exec_stdout"] = '[dockerd] time="..." msg="Starting up"\n'
    result = runner.invoke(app, ["logs", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert '[dockerd] time="..." msg="Starting up"' in result.output
    (args, _kwargs) = FakeInvoker.behavior["calls"][-1]
    script = args[args.index("--") + 1 :][2]
    assert "LINES=50" in script


def test_logs_reports_empty_output(tmp_path, fake_msb):
    result = runner.invoke(app, ["logs", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "No log output found" in result.output


def test_logs_with_timestamps_transforms_captured_output(tmp_path, fake_msb):
    fake_msb["exec_stdout"] = "[daemon.web] 2026-11-15 10:43:39.777605362  hi\n"
    result = runner.invoke(app, ["logs", "-t", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "2026-11-15T10:43:39.777605362+00:00 [daemon.web] hi" in result.output


def test_logs_without_labels_runs_the_plain_script(tmp_path, fake_msb):
    fake_msb["exec_stdout"] = "==> /tmp/dockerd.log <==\nplain line\n"
    result = runner.invoke(app, ["logs", "--no-labels", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "==> /tmp/dockerd.log <==" in result.output
    (args, _kwargs) = FakeInvoker.behavior["calls"][-1]
    script = args[args.index("--") + 1 :][2]
    assert "awk" not in script
    assert "plain /tmp/dockerd.log" in script


def test_logs_includes_the_kernel_when_named_as_a_source(tmp_path, fake_msb):
    fake_msb["exec_stdout"] = "[kernel] virtio message\n"
    result = runner.invoke(app, ["logs", "kernel", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "[kernel] virtio message" in result.output
    (args, _kwargs) = FakeInvoker.behavior["calls"][-1]
    script = args[args.index("--") + 1 :][2]
    assert "dmesg" in script
    # The kernel selection must not pull in the file sources.
    assert "/tmp/dockerd.log" not in script


def test_logs_rejects_unknown_source(tmp_path, fake_msb):
    result = runner.invoke(app, ["logs", "nope", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "unknown log source" in result.output


def test_logs_requires_a_running_sandbox(tmp_path, fake_msb):
    fake_msb["running"] = False
    result = runner.invoke(app, ["logs", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "is not running" in result.output


def test_logs_exec_failure_exits_one(tmp_path, fake_msb):
    fake_msb["exec_rc"] = 7
    result = runner.invoke(app, ["logs", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "exit code 7" in result.output


def test_logs_follow_streams_labeled_pipelines_for_files_and_kernel(tmp_path, fake_msb):
    fake_msb["files_stdout"] = "/tmp/dockerd.log\n/var/log/circus-daemons/web/current\n"
    result = runner.invoke(app, ["logs", "-f", "-n", "5", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    discovery = [
        call for call in FakeInvoker.behavior["calls"] if '[ -r "$f" ]' in call[0][-1]
    ]
    assert len(discovery) == 1
    _follow_args, follow_kwargs = FakeInvoker.behavior["calls"][-1]
    assert FakeInvoker.behavior["follow_args"] == [
        "[dockerd]|/tmp/dockerd.log",
        "[daemon.web]|/var/log/circus-daemons/web/current",
    ]
    script = FakeInvoker.behavior["follow_script"]
    assert 'tail -F -n "$LINES" -- "$f" 2>/dev/null | while IFS= read -r line' in script
    # The kernel is part of the default selection and follows via the
    # two-stage snapshot plus dmesg --follow-new.
    assert "dmesg --follow-new" in script
    assert follow_kwargs["capture_output"] is False


def test_logs_follow_with_timestamps_pumps_through_the_transform(
    tmp_path, fake_msb, monkeypatch
):
    fake_msb["files_stdout"] = "/tmp/dockerd.log\n"
    recorded: dict = {}

    def fake_stream(argv, transform):
        recorded["argv"] = argv
        recorded["transform"] = transform
        return 0

    monkeypatch.setattr(cli_module, "_stream_logs", fake_stream)
    result = runner.invoke(app, ["logs", "-f", "-t", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert recorded["transform"] is logs_module.normalize_log_line
    argv_text = " ".join(recorded["argv"])
    assert "[dockerd]|/tmp/dockerd.log" in argv_text
    assert "dmesg --follow-new" in argv_text
    # The passthrough path must not have been used.
    assert "tail_argv" not in FakeInvoker.behavior


def test_logs_follow_without_labels_and_kernel_uses_the_plain_script(
    tmp_path, fake_msb
):
    fake_msb["files_stdout"] = "/tmp/dockerd.log\n"
    result = runner.invoke(
        app, ["logs", "-f", "--no-labels", "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 0
    script = FakeInvoker.behavior["follow_script"]
    assert 'tail -F -n "$LINES" -- "$@" &' in script
    assert 'dmesg 2>/dev/null | tail -n "$LINES" &' in script
    assert "dmesg --follow-new" in script
    follow_args, _kwargs = FakeInvoker.behavior["calls"][-1]
    assert follow_args[follow_args.index("--") + 1 :][4:] == ["/tmp/dockerd.log"]
    assert "tail_argv" not in FakeInvoker.behavior


def test_logs_follow_without_labels_and_without_kernel_runs_one_plain_tail(
    tmp_path, fake_msb
):
    fake_msb["files_stdout"] = "/tmp/dockerd.log\n/var/log/circus-daemons/web/current\n"
    result = runner.invoke(
        app,
        [
            "logs",
            "-f",
            "--no-labels",
            "dockerd",
            "daemons",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    assert FakeInvoker.behavior["tail_argv"] == [
        "tail",
        "-F",
        "-n",
        "50",
        "--",
        "/tmp/dockerd.log",
        "/var/log/circus-daemons/web/current",
    ]
    assert "follow_script" not in FakeInvoker.behavior


def test_logs_follow_kernel_only_follows_dmesg(tmp_path, fake_msb):
    result = runner.invoke(app, ["logs", "-f", "kernel", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "No log files exist yet" not in result.output
    script = FakeInvoker.behavior["follow_script"]
    assert "dmesg --follow-new" in script
    follow_args, _kwargs = FakeInvoker.behavior["calls"][-1]
    assert follow_args[follow_args.index("--") + 1 :][4:] == []


def test_logs_follow_without_files_but_kernel_still_follows(tmp_path, fake_msb):
    result = runner.invoke(app, ["logs", "-f", "kernel", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "follow_script" in FakeInvoker.behavior


def test_logs_follow_without_any_file_reports_and_skips_following(tmp_path, fake_msb):
    result = runner.invoke(
        app,
        [
            "logs",
            "-f",
            "dockerd",
            "bridge",
            "daemons",
            "hooks",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0
    assert "No log files exist yet" in result.output
    assert "follow_script" not in FakeInvoker.behavior


#: Seconds a cleanup poll waits before declaring a process un-stopped.
_REAP_DEADLINE_S = 5.0


def _processes_matching(needle: str) -> list[int]:
    """Return the PIDs whose cmdline contains *needle*.

    Scans ``/proc`` directly so the tests do not depend on ``pgrep``.

    :param needle: String matched against each process's cmdline.
    :returns: Matching PIDs, unsorted.
    """
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if needle.encode() in cmdline:
            pids.append(int(entry.name))
    return pids


def _wait_until_gone(needle: str, deadline_s: float = _REAP_DEADLINE_S) -> None:
    """Poll until no process cmdline contains *needle*.

    :param needle: String matched against each process's cmdline.
    :param deadline_s: Seconds to keep polling before failing.
    :raises AssertionError: If processes still match at the deadline.
    """
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if not _processes_matching(needle):
            return
        time.sleep(0.05)
    raise AssertionError(f"processes still running: {_processes_matching(needle)}")


def _write_fake_dmesg(bin_dir: Path) -> Path:
    """Write a fake ``dmesg`` that simulates snapshot and follow-new.

    With ``--follow-new`` it prints one line every 50 ms forever; the
    endless output lets a SIGPIPE reach it quickly once its reader is
    killed. Otherwise it prints five numbered snapshot lines.

    :param bin_dir: Directory for the fake command (put on ``PATH``).
    :returns: Path of the written executable.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake = bin_dir / "dmesg"
    fake.write_text(
        "#!/bin/sh\n"
        'case " $* " in\n'
        "  *--follow-new*)\n"
        "    while :; do echo followed; sleep 0.05; done\n"
        "    ;;\n"
        "  *)\n"
        "    echo line1\n"
        "    echo line2\n"
        "    echo line3\n"
        "    echo line4\n"
        "    echo line5\n"
        "    ;;\n"
        "esac\n"
    )
    fake.chmod(0o755)
    return fake


def test_logs_script_limits_labeled_kernel_output(tmp_path, monkeypatch, capsys):
    """--lines limits the labeled kernel branch, driven by a fake dmesg."""
    fake = _write_fake_dmesg(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    script = build_logs_script(resolve_log_sources(["kernel"]), 2)
    result = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout == "[kernel] line4\n[kernel] line5\n"


def test_logs_script_limits_unlabeled_kernel_output(tmp_path, monkeypatch, capsys):
    """--lines limits the unlabeled kernel branch too."""
    fake = _write_fake_dmesg(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    script = build_logs_script(resolve_log_sources(["kernel"]), 2, labels=False)
    result = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout == "==> kernel (dmesg) <==\nline4\nline5\n"


def test_follow_script_trap_stops_the_kernel_follower(tmp_path, monkeypatch):
    """SIGTERM to the follow script stops the fake dmesg followers."""
    fake = _write_fake_dmesg(tmp_path / "bin")
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    script = build_follow_logs_script([], 2, kernel=True)
    proc = subprocess.Popen(
        ["sh", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # The limited snapshot labels its last two records.
        assert proc.stdout is not None
        assert proc.stdout.readline().rstrip() == "[kernel] line4"
        assert proc.stdout.readline().rstrip() == "[kernel] line5"
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=_REAP_DEADLINE_S) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
    _wait_until_gone(str(fake))


def test_stream_logs_fast_children_repeatedly_return_real_status(capsys):
    """The EOF path must never terminate an exited child (no -15)."""
    for _ in range(20):
        assert cli_module._stream_logs(["echo", "hi"], str) == 0
    for _ in range(20):
        assert cli_module._stream_logs(["sh", "-c", "echo out; exit 3"], str) == 3
    assert capsys.readouterr().out.count("hi") == 20


class _InterruptingTransform:
    """Transform that raises KeyboardInterrupt after the first line."""

    def __init__(self) -> None:
        self.seen = 0

    def __call__(self, line: str) -> str:
        self.seen += 1
        if self.seen > 1:
            raise KeyboardInterrupt
        return line


class _BrokenPipeStdout:
    """stdout stand-in whose write raises BrokenPipeError once."""

    def __init__(self) -> None:
        self.writes = 0

    def write(self, text: str) -> int:
        self.writes += 1
        if self.writes > 1:
            raise BrokenPipeError
        return len(text)

    def flush(self) -> None:
        pass


def test_stream_logs_interrupt_stops_and_reaps_the_child(tmp_path, monkeypatch):
    """KeyboardInterrupt terminates the follower and re-raises."""
    marker = "stream-logs-interrupt-marker"
    proc_argv = [
        "sh",
        "-c",
        f"echo first; echo second; exec sleep 300 {marker}",
    ]
    transform = _InterruptingTransform()
    with pytest.raises(KeyboardInterrupt):
        cli_module._stream_logs(proc_argv, transform)
    assert transform.seen >= 2
    _wait_until_gone(marker)


def test_stream_logs_broken_pipe_stops_and_reaps_the_child(tmp_path, monkeypatch):
    """A closed viewer pipe stops the child and returns zero."""
    marker = "stream-logs-broken-pipe-marker"
    fake_stdout = _BrokenPipeStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    returncode = cli_module._stream_logs(
        ["sh", "-c", f"echo first; echo second; exec sleep 300 {marker}"], str
    )
    assert returncode == 0
    _wait_until_gone(marker)
