"""Tests for the host entry parser, filter, and rendering."""

import logging

from msb_circus.exec_ import (
    GUEST_HOSTS_FILE,
    HOSTS_BLOCK_BEGIN,
    HOSTS_BLOCK_END,
    build_write_hosts_args,
)
from msb_circus.hosts import (
    HostLine,
    loopback_names,
    parse_hosts_text,
    read_hosts_file,
    render_host_block,
    select_host_lines,
    validate_host_lines,
)


def test_parse_hosts_text_skips_comments_and_blank_lines():
    text = "# comment\n127.0.0.1 localhost\n\n   \n10.0.0.1 myserver\n"
    assert parse_hosts_text(text) == [
        ("127.0.0.1", ("localhost",)),
        ("10.0.0.1", ("myserver",)),
    ]


def test_parse_hosts_text_strips_inline_comments():
    assert parse_hosts_text("10.0.0.2 server # inline comment\n") == [
        ("10.0.0.2", ("server",))
    ]


def test_parse_hosts_text_skips_lines_without_names():
    assert parse_hosts_text("10.0.0.3\njusttext\n") == []


def test_select_host_lines_matches_glob_case_insensitively():
    lines: list[HostLine] = [
        ("10.0.0.1", ("MyServer.Corp.Internal",)),
        ("10.0.0.2", ("other.example.com",)),
    ]
    assert select_host_lines(lines, ["*.corp.internal"]) == lines[:1]


def test_select_host_lines_matches_regex_unanchored():
    lines: list[HostLine] = [
        ("10.0.0.1", ("my.vpn.example.com",)),
        ("10.0.0.2", ("example.org",)),
    ]
    assert select_host_lines(lines, [r"re:\.vpn\.example\.com$"]) == lines[:1]


def test_select_host_lines_keeps_all_aliases_of_matching_line():
    lines: list[HostLine] = [("10.0.0.1", ("db.corp.internal", "db"))]
    assert select_host_lines(lines, ["db"]) == lines


def test_select_host_lines_drops_duplicate_lines():
    lines: list[HostLine] = [
        ("10.0.0.1", ("myserver",)),
        ("10.0.0.1", ("myserver",)),
    ]
    assert select_host_lines(lines, ["myserver"]) == lines[:1]


def test_select_host_lines_returns_nothing_without_patterns():
    lines: list[HostLine] = [("10.0.0.1", ("myserver",))]
    assert select_host_lines(lines, []) == []


def test_read_hosts_file_warns_and_returns_empty_on_missing_file(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        assert read_hosts_file(str(tmp_path / "missing")) == []
    assert "Cannot read hosts file" in caplog.text


def test_validate_host_lines_drops_invalid_ip(caplog):
    with caplog.at_level(logging.WARNING):
        result = validate_host_lines([("999.1.2.3", ("myserver",))])
    assert result == []
    assert "invalid IP address" in caplog.text


def test_validate_host_lines_drops_invalid_name(caplog):
    with caplog.at_level(logging.WARNING):
        result = validate_host_lines([("10.0.0.1", ("my server",))])
    assert result == []
    assert "invalid name(s)" in caplog.text


def test_loopback_names_lists_ipv4_and_ipv6_loopback():
    lines: list[HostLine] = [
        ("127.0.0.1", ("myapp.local", "app")),
        ("10.0.0.1", ("real.corp.internal",)),
        ("::1", ("v6local",)),
    ]
    assert loopback_names(lines) == ["myapp.local", "app", "v6local"]


def test_render_host_block_formats_one_line_per_entry():
    lines: list[HostLine] = [
        ("10.0.0.1", ("db.corp.internal", "db")),
        ("10.0.0.2", ("server",)),
    ]
    assert render_host_block(lines) == ("10.0.0.1 db.corp.internal db\n10.0.0.2 server")


def test_build_write_hosts_args_passes_block_as_argument():
    args = build_write_hosts_args("agents-x", "10.0.0.1 myserver")
    assert args[0] == "exec"
    assert args[3:5] == ["sh", "-c"]
    script = args[5]
    assert HOSTS_BLOCK_BEGIN in script
    assert "10.0.0.1 myserver" not in script  # block is the argument, not the script
    assert args[-2:] == ["sh", "10.0.0.1 myserver"]


def test_hosts_script_appends_block_to_existing_file(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n")
    _run_hosts_script(hosts, "10.0.0.1 myserver")
    content = hosts.read_text()
    assert HOSTS_BLOCK_BEGIN in content and HOSTS_BLOCK_END in content
    assert "10.0.0.1 myserver" in content
    assert content.startswith("127.0.0.1 localhost\n")


def test_hosts_script_replaces_existing_block_idempotently(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n")
    _run_hosts_script(hosts, "10.0.0.1 oldserver")
    _run_hosts_script(hosts, "10.0.0.2 newserver")
    content = hosts.read_text()
    assert content.count(HOSTS_BLOCK_BEGIN) == 1
    assert "10.0.0.2 newserver" in content
    assert "10.0.0.1 oldserver" not in content
    assert content.splitlines()[0] == "127.0.0.1 localhost"


def test_hosts_script_removes_block_when_empty(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1 localhost\n")
    _run_hosts_script(hosts, "10.0.0.1 myserver")
    _run_hosts_script(hosts, "")
    content = hosts.read_text()
    assert HOSTS_BLOCK_BEGIN not in content
    assert "10.0.0.1 myserver" not in content
    assert content == "127.0.0.1 localhost\n"


def test_hosts_script_preserves_lines_outside_markers(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text(
        "127.0.0.1 localhost\n"
        f"{HOSTS_BLOCK_BEGIN}\n10.0.0.9 stale\n{HOSTS_BLOCK_END}\n"
        "192.168.1.1 gateway\n"
    )
    _run_hosts_script(hosts, "10.0.0.1 myserver")
    content = hosts.read_text()
    assert "127.0.0.1 localhost" in content
    assert "192.168.1.1 gateway" in content
    assert "10.0.0.9 stale" not in content
    assert "10.0.0.1 myserver" in content


def _run_hosts_script(hosts_path, block: str) -> None:
    """Run the guest hosts script locally against *hosts_path*.

    The script is plain POSIX sh, so it runs on the test host with the
    guest file path replaced by a temporary file. ``GUEST_HOSTS_FILE`` is
    replaced textually, mirroring what the builder bakes in for the real
    guest path.

    :param hosts_path: File the script treats as the guest hosts file.
    :param block: Rendered host lines passed as the script argument.
    """
    import subprocess

    from msb_circus.exec_ import _hosts_script

    script = _hosts_script(GUEST_HOSTS_FILE).replace(GUEST_HOSTS_FILE, str(hosts_path))
    subprocess.run(["sh", "-c", script, "sh", block], check=True)
