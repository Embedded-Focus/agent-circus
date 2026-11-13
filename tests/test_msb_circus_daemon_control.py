"""Tests for msb-circus daemon control scripts and status parsing."""

from __future__ import annotations

import subprocess

from msb_circus.daemons import (
    build_logs_command,
    build_port_wait_script,
    build_restart_script,
    build_start_script,
    build_status_script,
    build_stop_all_script,
    build_stop_script,
    format_duration,
    parse_status_output,
)


def test_build_start_script_matches_expected_text() -> None:
    assert build_start_script("web") == (
        "s=/run/service/circus-daemon-web\n"
        'if [ ! -d "$s" ]; then\n'
        '    echo "daemon web is not registered in this sandbox" >&2\n'
        "    exit 4\n"
        "fi\n"
        "rm -f /run/circus-daemons/web/exits /run/circus-daemons/web/failed\n"
        '/command/s6-svc -u "$s"\n'
    )


def test_build_stop_script_matches_expected_text() -> None:
    assert build_stop_script("web") == (
        "s=/run/service/circus-daemon-web\n"
        'if [ ! -d "$s" ]; then\n'
        '    echo "daemon web is not registered in this sandbox" >&2\n'
        "    exit 4\n"
        "fi\n"
        '/command/s6-svc -wD -T 15000 -d "$s"\n'
    )


def test_build_restart_script_matches_expected_text() -> None:
    assert build_restart_script("web") == (
        "s=/run/service/circus-daemon-web\n"
        'if [ ! -d "$s" ]; then\n'
        '    echo "daemon web is not registered in this sandbox" >&2\n'
        "    exit 4\n"
        "fi\n"
        '/command/s6-svc -wD -T 15000 -d "$s" || true\n'
        "rm -f /run/circus-daemons/web/exits /run/circus-daemons/web/failed\n"
        '/command/s6-svc -u "$s"\n'
    )


def test_build_stop_all_script_matches_expected_text() -> None:
    assert build_stop_all_script() == (
        "for d in /run/service/circus-daemon-*; do\n"
        '    [ -d "$d" ] || continue\n'
        '    /command/s6-svc -wD -T 15000 -d "$d" &\n'
        "done\n"
        "wait\n"
    )


def test_build_status_script_matches_expected_text() -> None:
    assert build_status_script() == (
        "for d in /run/service/circus-daemon-*; do\n"
        '    [ -d "$d" ] || continue\n'
        "    n=${d##*/circus-daemon-}\n"
        "    f=0\n"
        '    [ -e "/run/circus-daemons/$n/failed" ] && f=1\n'
        '    st=$(/command/s6-svstat -o up,wantedup,pid,updownfor "$d" '
        "2>/dev/null) || st=unknown\n"
        '    echo "$n $f $st"\n'
        "done\n"
    )


def test_build_port_wait_script_matches_expected_text() -> None:
    assert build_port_wait_script(4096, 30) == (
        "i=0\n"
        'while [ "$i" -lt 30 ]; do\n'
        "    if socat -u OPEN:/dev/null TCP:127.0.0.1:4096,connect-timeout=1 "
        ">/dev/null 2>&1; then\n"
        "        exit 0\n"
        "    fi\n"
        "    sleep 1\n"
        "    i=$((i + 1))\n"
        "done\n"
        "exit 1\n"
    )


def test_control_scripts_pass_shell_syntax_check() -> None:
    scripts = [
        build_start_script("web"),
        build_stop_script("web"),
        build_restart_script("web"),
        build_stop_all_script(),
        build_status_script(),
        build_port_wait_script(4096, 30),
    ]
    for script in scripts:
        assert subprocess.run(["sh", "-n", "-c", script], check=False).returncode == 0


def test_build_logs_command_returns_expected_lists() -> None:
    assert build_logs_command("web", 50, True) == [
        "tail",
        "-n",
        "50",
        "-F",
        "/var/log/circus-daemons/web/current",
    ]
    assert build_logs_command("web", 50, False) == [
        "tail",
        "-n",
        "50",
        "/var/log/circus-daemons/web/current",
    ]


def test_parse_status_output_classifies_known_lines() -> None:
    text = (
        "web 0 true true 2230 45\n"
        "\n"
        "db 0 true false 2231 1\n"
        "api 1 false false -1 3\n"
        "job 0 false true -1 0\n"
        "old 0 false false -1 600\n"
        "bad 0 unknown\n"
        "x\n"
    )
    statuses = parse_status_output(text)
    assert set(statuses) == {"web", "db", "api", "job", "old", "bad"}

    web = statuses["web"]
    assert web.state == "running"
    assert web.pid == 2230
    assert web.seconds == 45

    db = statuses["db"]
    assert db.state == "stopping"
    assert db.pid == 2231
    assert db.seconds == 1

    api = statuses["api"]
    assert api.state == "failed"
    assert api.pid is None
    assert api.seconds == 3

    job = statuses["job"]
    assert job.state == "starting"
    assert job.pid is None
    assert job.seconds == 0

    old = statuses["old"]
    assert old.state == "stopped"
    assert old.pid is None
    assert old.seconds == 600

    bad = statuses["bad"]
    assert bad.state == "unknown"
    assert bad.pid is None
    assert bad.seconds is None


def test_format_duration_returns_compact_strings() -> None:
    assert format_duration(None) == "-"
    assert format_duration(0) == "0s"
    assert format_duration(59) == "59s"
    assert format_duration(60) == "1m"
    assert format_duration(3599) == "59m"
    assert format_duration(3600) == "1h"
    assert format_duration(86399) == "23h"
    assert format_duration(86400) == "1d"
