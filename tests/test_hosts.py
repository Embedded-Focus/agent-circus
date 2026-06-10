"""Tests for /etc/hosts filtering and compose override."""

import json
import textwrap
from pathlib import Path

from agent_circus.config import (
    AVAILABLE_SERVICES,
    build_hosts_override,
    filter_hosts,
    parse_hosts_file,
)

# ---------------------------------------------------------------------------
# parse_hosts_file
# ---------------------------------------------------------------------------


def test_parse_hosts_file_skips_comments(tmp_path: Path) -> None:
    hosts = tmp_path / "hosts"
    hosts.write_text(
        textwrap.dedent("""\
            # This is a comment
            127.0.0.1  localhost

            # Another comment
            10.0.0.1  myserver
        """)
    )
    entries = parse_hosts_file(str(hosts))
    assert len(entries) == 2
    assert entries[0] == ("127.0.0.1", ["localhost"])
    assert entries[1] == ("10.0.0.1", ["myserver"])


def test_parse_hosts_file_includes_aliases(tmp_path: Path) -> None:
    hosts = tmp_path / "hosts"
    hosts.write_text("10.0.0.5  myserver myserver.corp.internal\n")
    entries = parse_hosts_file(str(hosts))
    assert entries == [("10.0.0.5", ["myserver", "myserver.corp.internal"])]


def test_parse_hosts_file_strips_inline_comments(tmp_path: Path) -> None:
    hosts = tmp_path / "hosts"
    hosts.write_text("10.0.0.2  server  # inline comment\n")
    entries = parse_hosts_file(str(hosts))
    assert entries == [("10.0.0.2", ["server"])]


def test_parse_hosts_file_missing_file_returns_empty() -> None:
    entries = parse_hosts_file("/nonexistent/hosts")
    assert entries == []


# ---------------------------------------------------------------------------
# filter_hosts
# ---------------------------------------------------------------------------


def test_filter_hosts_glob_match() -> None:
    entries = [("10.0.0.1", ["foo.corp"])]
    result = filter_hosts(entries, ["*.corp"])
    assert "foo.corp:10.0.0.1" in result


def test_filter_hosts_glob_no_match() -> None:
    entries = [("10.0.0.1", ["foo.example.com"])]
    result = filter_hosts(entries, ["*.corp"])
    assert result == []


def test_filter_hosts_regex_prefix() -> None:
    entries = [("10.0.0.2", ["myhost.local"])]
    result = filter_hosts(entries, ["re:\\.local$"])
    assert "myhost.local:10.0.0.2" in result


def test_filter_hosts_regex_no_match() -> None:
    entries = [("10.0.0.2", ["myhost.example"])]
    result = filter_hosts(entries, ["re:\\.local$"])
    assert result == []


def test_filter_hosts_alias_match_forwards_all_names() -> None:
    entries = [("10.0.0.5", ["myserver", "myserver.corp.internal"])]
    result = filter_hosts(entries, ["*.corp.internal"])
    assert "myserver.corp.internal:10.0.0.5" in result
    assert "myserver:10.0.0.5" in result


def test_filter_hosts_empty_patterns_returns_nothing() -> None:
    entries = [("10.0.0.1", ["foo.corp"])]
    result = filter_hosts(entries, [])
    assert result == []


def test_filter_hosts_case_insensitive() -> None:
    entries = [("10.0.0.1", ["MyServer.Corp"])]
    result = filter_hosts(entries, ["*.corp"])
    assert len(result) == 1


def test_filter_hosts_deduplicates() -> None:
    entries = [("10.0.0.1", ["foo"]), ("10.0.0.1", ["foo"])]
    result = filter_hosts(entries, ["foo"])
    assert result.count("foo:10.0.0.1") == 1


# ---------------------------------------------------------------------------
# build_hosts_override
# ---------------------------------------------------------------------------


def test_build_hosts_override_structure() -> None:
    extra_hosts = ["myserver:10.0.0.1"]
    result = json.loads(build_hosts_override(extra_hosts))
    assert "services" in result
    for svc in AVAILABLE_SERVICES:
        assert "extra_hosts" in result["services"][svc]
        assert "myserver:10.0.0.1" in result["services"][svc]["extra_hosts"]


def test_build_hosts_override_all_services() -> None:
    result = json.loads(build_hosts_override(["h:1.2.3.4"]))
    assert set(result["services"].keys()) == set(AVAILABLE_SERVICES)


def test_build_hosts_override_additional_services() -> None:
    result = json.loads(build_hosts_override(["h:1.2.3.4"], ["mcp-filesystem"]))
    assert result["services"]["mcp-filesystem"]["extra_hosts"] == ["h:1.2.3.4"]


def test_build_hosts_override_returns_valid_json() -> None:
    output = build_hosts_override(["a:1.1.1.1", "b:2.2.2.2"])
    parsed = json.loads(output)
    assert isinstance(parsed, dict)
