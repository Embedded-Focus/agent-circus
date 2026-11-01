"""Tests for the msb-circus remove (rm) command."""

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

import msb_circus.cli as cli_module
from msb_circus.cli import app
from msb_circus.config import sandbox_name_for_workspace
from msb_circus.exceptions import MsbCommandError

runner = CliRunner()


class FakeInvoker:
    """Records invocations and replays canned stdout/stderr or failures."""

    behavior: ClassVar[dict] = {}

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        self.behavior.setdefault("calls", []).append(args)
        if self.behavior.get("fail_for") and any(
            fail in args for fail in self.behavior["fail_for"]
        ):
            raise MsbCommandError(
                f"msb failed: {self.behavior.get('stderr', '')}",
                returncode=self.behavior.get("fail_code", 1),
                stderr=self.behavior.get("stderr", ""),
            )
        return SimpleNamespace(
            returncode=0, stdout=self.behavior.get("stdout", "[]"), stderr=""
        )


@pytest.fixture
def fake_msb(monkeypatch):
    """Patch the CLI to use a FakeInvoker with per-test behavior."""
    FakeInvoker.behavior = {}
    monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: Path("/fake"))
    monkeypatch.setattr(cli_module, "check_version", lambda binary: "0.7.12")
    monkeypatch.setattr(cli_module, "MsbInvoker", FakeInvoker)
    return FakeInvoker.behavior


def test_remove_defaults_to_workspace_sandbox(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(app, ["rm", "-f", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert fake_msb["calls"] == [["remove", "--force", name]]
    assert f"✓ Removed {name}" in result.output


def test_remove_explicit_names_skip_workspace_default(tmp_path, fake_msb):
    result = runner.invoke(
        app, ["remove", "-f", "--workspace", str(tmp_path), "sandbox-a", "sandbox-b"]
    )
    assert result.exit_code == 0
    assert fake_msb["calls"] == [
        ["remove", "--force", "sandbox-a"],
        ["remove", "--force", "sandbox-b"],
    ]


def test_remove_without_force_uses_no_msb_force_flag(tmp_path, fake_msb):
    name = sandbox_name_for_workspace(tmp_path)
    result = runner.invoke(app, ["remove", "--workspace", str(tmp_path)], input="y\n")
    assert result.exit_code == 0
    assert fake_msb["calls"] == [["remove", name]]


def test_remove_without_force_prompts_and_aborts_on_no(tmp_path, fake_msb):
    result = runner.invoke(app, ["rm", "--workspace", str(tmp_path)], input="n\n")
    assert result.exit_code == 0
    assert "Aborted." in result.output
    assert "calls" not in fake_msb  # no msb invocation happened


def test_remove_failure_exits_nonzero(tmp_path, monkeypatch, fake_msb):
    fake_msb["fail_for"] = ["agents-doomed"]
    fake_msb["stderr"] = "error: sandbox agents-doomed is running"
    result = runner.invoke(
        app, ["rm", "-f", "--workspace", str(tmp_path), "agents-doomed"]
    )
    assert result.exit_code == 1
    assert "Error:" in result.output


def test_remove_failure_shows_remediation_hint(tmp_path, monkeypatch):
    """Full path: a real MsbInvoker surfaces the remediation hint."""
    fake = tmp_path / "msb"
    fake.write_text(
        "#!/bin/sh\necho 'error: sandbox agents-doomed is running' >&2\nexit 1\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: fake)
    monkeypatch.setattr(cli_module, "check_version", lambda binary: None)

    result = runner.invoke(
        app, ["rm", "-f", "--workspace", str(tmp_path), "agents-doomed"]
    )
    assert result.exit_code == 1
    assert "is running" in result.output
    assert "--force" in result.output  # remediation hint is shown


def test_rm_is_hidden_alias_of_remove(tmp_path, fake_msb):
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "rm" not in result.output
    assert "remove" in result.output
