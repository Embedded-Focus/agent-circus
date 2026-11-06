"""Tests for the msb-circus ps command and sandbox list parsing."""

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import msb_circus.cli as cli_module
from msb_circus.cli import app
from msb_circus.config import sandbox_name_for_workspace
from msb_circus.exceptions import ConfigurationError
from msb_circus.ps import format_created, human_duration
from msb_circus.sandbox import SandboxInfo, parse_sandbox_list_output

runner = CliRunner()


def test_parse_empty_list():
    assert parse_sandbox_list_output("[]") == []


def test_parse_entries_with_status_and_extras():
    output = json.dumps(
        [
            {
                "name": "agents-demo",
                "status": "Running",
                "id": "abc123",
                "created_at": 1730000000,
                "updated_at": 1730000100,
                "unknown_future_field": 1,
            },
            {"name": "agents-other", "state": "Stopped"},
        ]
    )
    sandboxes = parse_sandbox_list_output(output)
    assert len(sandboxes) == 2
    assert sandboxes[0].name == "agents-demo"
    assert sandboxes[0].status == "Running"
    assert sandboxes[0].id == "abc123"
    assert sandboxes[0].created_at == 1730000000
    assert sandboxes[0].raw["unknown_future_field"] == 1
    # 'state' is accepted as a status fallback.
    assert sandboxes[1].status == "Stopped"


def test_parse_paginated_envelope():
    output = json.dumps({"sandboxes": [{"name": "agents-x"}], "next_cursor": None})
    sandboxes = parse_sandbox_list_output(output)
    assert [s.name for s in sandboxes] == ["agents-x"]


def test_parse_rfc3339_created_at():
    """Current msb emits created_at as an RFC 3339 string, not epoch."""
    output = json.dumps(
        [
            {
                "name": "agents-demo",
                "status": "Running",
                "created_at": "2026-11-06T23:41:00+00:00",
            }
        ]
    )
    sandboxes = parse_sandbox_list_output(output)
    assert sandboxes[0].created_at == pytest.approx(1794008460.0)


def test_parse_rfc3339_created_at_with_z_suffix_and_naive():
    output = json.dumps(
        [
            {"name": "a", "created_at": "2026-11-06T23:41:00Z"},
            {"name": "b", "created_at": "2026-11-06T23:41:00"},
        ]
    )
    sandboxes = parse_sandbox_list_output(output)
    assert sandboxes[0].created_at == sandboxes[1].created_at
    assert sandboxes[0].created_at == pytest.approx(1794008460.0)


def test_parse_unparseable_timestamp_yields_none():
    output = json.dumps([{"name": "a", "created_at": "yesterday"}])
    sandboxes = parse_sandbox_list_output(output)
    assert sandboxes[0].created_at is None


@pytest.mark.parametrize(
    "output",
    [
        "not json",
        '{"unexpected": "object"}',
        '[{"no_name": true}]',
        "[42]",
    ],
)
def test_parse_rejects_malformed_output(output):
    with pytest.raises(ConfigurationError):
        parse_sandbox_list_output(output)


@pytest.fixture
def fake_msb(monkeypatch):
    """Patch the CLI to use a fake msb binary returning canned JSON."""
    calls = {}

    class FakeInvoker:
        def __init__(self, binary: Path) -> None:
            calls["binary"] = binary

        def run(self, args, **kwargs):
            calls["args"] = args
            return SimpleNamespace(returncode=0, stdout=calls["stdout"], stderr="")

    def setup(stdout: str) -> None:
        calls["stdout"] = stdout
        monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: Path("/fake"))
        monkeypatch.setattr(cli_module, "check_version", lambda binary: "0.7.12")
        monkeypatch.setattr(cli_module, "MsbInvoker", FakeInvoker)

    setup("[]")
    return calls


def test_ps_reports_workspace_sandbox(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    fake_msb["stdout"] = json.dumps(
        [
            {"name": "agents-other-project", "status": "Running"},
            {"name": name, "status": "Running", "id": "deadbeef"},
        ]
    )
    result = runner.invoke(app, ["ps", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    # docker compose ps-style table with a header row.
    assert result.output.startswith("NAME")
    assert name in result.output
    assert "Running" in result.output
    assert "deadbeef" in result.output
    assert "agents-other-project" not in result.output


def test_ps_all_flag_lists_every_sandbox(tmp_path, fake_msb):
    fake_msb["stdout"] = json.dumps(
        [
            {"name": "agents-msb-circus", "status": "Running"},
            {"name": "scratch", "status": "Stopped"},
        ]
    )
    result = runner.invoke(app, ["ps", "--workspace", str(tmp_path), "--all"])
    assert result.exit_code == 0
    assert "agents-msb-circus" in result.output
    assert "scratch" in result.output


def test_ps_running_filter_passes_flag(tmp_path, fake_msb):
    result = runner.invoke(
        app, ["ps", "--workspace", str(tmp_path), "--all", "--running"]
    )
    assert result.exit_code == 0
    assert fake_msb["args"][:2] == ["list", "--running"]


def test_ps_json_format_is_parseable(tmp_path, fake_msb):
    fake_msb["stdout"] = json.dumps(
        [
            {
                "name": "agents-demo",
                "status": "Running",
                "id": "deadbeef",
                "created_at": 1730000000,
                "updated_at": 1730000100,
            }
        ]
    )
    result = runner.invoke(
        app, ["ps", "--workspace", str(tmp_path), "--all", "--format", "json"]
    )
    assert result.exit_code == 0
    entries = json.loads(result.output)
    assert entries == [
        {
            "name": "agents-demo",
            "id": "deadbeef",
            "status": "Running",
            "created_at": 1730000000,
            "updated_at": 1730000100,
        }
    ]


def test_ps_json_format_is_silent_without_sandbox(tmp_path, fake_msb):
    result = runner.invoke(
        app, ["ps", "--workspace", str(tmp_path), "--format", "json"]
    )
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_ps_json_format_rejects_unknown_format(tmp_path, fake_msb):
    result = runner.invoke(
        app, ["ps", "--workspace", str(tmp_path), "--format", "yaml"]
    )
    assert result.exit_code == 1
    assert "yaml" in result.output


def test_ps_quiet_prints_names_only(tmp_path, fake_msb):
    fake_msb["stdout"] = json.dumps(
        [
            {"name": "agents-demo", "status": "Running"},
            {"name": "scratch", "status": "Stopped"},
        ]
    )
    result = runner.invoke(
        app, ["ps", "--workspace", str(tmp_path), "--all", "--quiet"]
    )
    assert result.exit_code == 0
    assert result.output.splitlines() == ["agents-demo", "scratch"]


def test_ps_quiet_is_silent_without_sandbox(tmp_path, fake_msb):
    result = runner.invoke(app, ["ps", "--workspace", str(tmp_path), "--quiet"])
    assert result.exit_code == 0
    assert result.output == ""


def test_ps_missing_workspace_sandbox_reports_name(tmp_path, fake_msb):
    result = runner.invoke(app, ["ps", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "No sandbox" in result.output


def test_human_duration_wording_matches_docker():
    assert human_duration(0) == "0 seconds"
    assert human_duration(1) == "1 second"
    assert human_duration(42) == "42 seconds"
    assert human_duration(60) == "About a minute"
    assert human_duration(120) == "2 minutes"
    assert human_duration(3600) == "About an hour"
    assert human_duration(7200) == "2 hours"
    assert human_duration(24 * 3600) == "24 hours"  # docker switches to days at 48h
    assert human_duration(3 * 86400) == "3 days"
    assert human_duration(20 * 86400) == "2 weeks"
    assert human_duration(70 * 86400) == "2 months"
    assert human_duration(800 * 86400) == "2 years"


def test_format_created_uses_relative_age_or_placeholder():
    now = time.time()
    assert format_created(None, now=now) == "-"
    assert format_created(now - 120, now=now) == "2 minutes ago"


def test_format_sandbox_table_is_column_aligned():
    from msb_circus.ps import format_sandbox_table

    sandboxes = [
        SandboxInfo(name="a", status="Running"),
        SandboxInfo(name="much-longer-name", id="deadbeefdeadbeef", created_at=100),
    ]
    table = format_sandbox_table(sandboxes, now=160)
    lines = table.splitlines()
    assert lines[0].split() == ["NAME", "ID", "CREATED", "STATUS"]
    # Columns align across rows and the ID is truncated like docker ps does.
    assert lines[2].startswith("much-longer-name  deadbeefdead")
    assert lines[2].split() == [
        "much-longer-name",
        "deadbeefdead",
        "About",
        "a",
        "minute",
        "ago",
        "-",
    ]
    assert lines[1].split() == ["a", "-", "-", "Running"]


def test_ps_fails_on_msb_error(tmp_path, monkeypatch):
    from msb_circus.exceptions import MsbCommandError

    class FailingInvoker:
        def __init__(self, binary: Path) -> None: ...

        def run(self, args, **kwargs):
            raise MsbCommandError("boom", returncode=1, stderr="boom")

    monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: Path("/fake"))
    monkeypatch.setattr(cli_module, "check_version", lambda binary: None)
    monkeypatch.setattr(cli_module, "MsbInvoker", FailingInvoker)

    result = runner.invoke(app, ["ps", "--workspace", str(tmp_path)])
    assert result.exit_code == 1
    assert "boom" in result.output
