"""Tests for the ``agent-circus skills`` CLI command (typer layer)."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

import agent_circus.cli as cli_module
from agent_circus.config import AgentCircusConfig, ImageConfig, SkillsConfig
from agent_circus.exceptions import MsbCommandError
from agent_circus.skills_installer import InstallerError


class FakeInvoker:
    """Records invocations; scripted per-subcommand behavior for skills."""

    behavior: ClassVar[dict] = {}

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        self.behavior.setdefault("calls", []).append(args)
        script = self.behavior
        if args[0] == "image" and args[1] == "inspect":
            returncode = script.get("image_rc", 0)
            return SimpleNamespace(returncode=returncode, stdout="", stderr="")
        if args[0] == "list":
            entry = script.get("installer_entry")
            payload = [entry] if entry else []
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        if args[0] == "run" and "run_error" in script:
            raise script["run_error"]
        if (
            args[0] == "remove"
            and kwargs.get("check", True)
            and script.get("remove_fails")
        ):
            raise MsbCommandError("remove failed", returncode=1, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def installer(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Set up a fake invoker, a store under XDG_DATA_HOME and a fake run.

    :param tmp_path: Per-test temporary directory.
    :param monkeypatch: Pytest environment patch helper.
    :returns: ``(FakeInvoker.behavior, config, store)`` tuple.
    """
    FakeInvoker.behavior = {}
    store = tmp_path / "xdg-data" / "agent-circus" / "system-skills"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    config = AgentCircusConfig(
        image=ImageConfig(tag="img:1"), skills=SkillsConfig(store=str(store))
    )
    behavior = FakeInvoker.behavior
    behavior["calls"] = []

    def fake_run_skills_installer(invoker, config, args):
        behavior["installer_args"] = list(args)
        behavior["calls"].append(["run-skills-installer", *args])
        return behavior.get("installer_rc", 0)

    monkeypatch.setattr(cli_module, "load_config", lambda _cwd: config)
    monkeypatch.setattr(
        cli_module, "resolve_msb_binary", lambda _config_binary: Path("/fake")
    )
    monkeypatch.setattr(cli_module, "check_version", lambda _binary: "0.7.12")
    monkeypatch.setattr(cli_module, "MsbInvoker", FakeInvoker)
    monkeypatch.setattr(cli_module, "run_skills_installer", fake_run_skills_installer)
    return behavior, config, store


def test_skills_command_forwards_args_verbatim(installer) -> None:
    """All extra CLI arguments arrive at the installer unchanged."""
    behavior, _config, _store = installer
    result = CliRunner().invoke(
        cli_module.app, ["skills", "add", "owner/repo", "--skill", "x", "-y"]
    )
    assert result.exit_code == 0
    assert behavior["installer_args"] == ["add", "owner/repo", "--skill", "x", "-y"]


def test_skills_command_without_args_prints_usage_and_exits_2(
    installer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare ``skills`` prints the store path, exits 2, and never resolves msb."""
    _behavior, _config, store = installer

    def fail_resolve(_config_binary: str | None) -> Path:
        raise AssertionError("resolve_msb_binary must not be called")

    monkeypatch.setattr(cli_module, "resolve_msb_binary", fail_resolve)
    result = CliRunner().invoke(cli_module.app, ["skills"])
    assert result.exit_code == 2
    assert "Usage: agent-circus skills" in result.output
    assert str(store) in result.output


def test_skills_command_maps_installer_error_to_exit_code(
    installer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An InstallerError's exit code and text reach the CLI output."""

    def raise_installer_error(invoker, config, args):
        raise InstallerError("x", exit_code=2)

    _behavior, _config, _store = installer
    monkeypatch.setattr(cli_module, "run_skills_installer", raise_installer_error)
    result = CliRunner().invoke(cli_module.app, ["skills", "add", "owner/repo"])
    assert result.exit_code == 2
    assert "Error: x" in result.output
