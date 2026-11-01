"""Tests for the msb-circus ps command and sandbox list parsing."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import msb_circus.cli as cli_module
from msb_circus.cli import app
from msb_circus.config import sandbox_name_for_workspace
from msb_circus.exceptions import ConfigurationError
from msb_circus.sandbox import parse_sandbox_list_output

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
    assert name in result.output
    assert "Running" in result.output
    assert "agents-other-project" not in result.output


def test_ps_reports_missing_workspace_sandbox(tmp_path, fake_msb):
    result = runner.invoke(app, ["ps", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "No sandbox" in result.output


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
