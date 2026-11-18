"""Tests for the ``msb-circus skills`` CLI command (typer layer)."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest
from typer.testing import CliRunner

import msb_circus.cli as cli_module
from msb_circus.config import ImageConfig, MsbConfig, SkillsConfig
from msb_circus.exceptions import MsbCommandError
from msb_circus.msb import MsbInvoker
from msb_circus.skills_installer import InstallerError


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
    store = tmp_path / "xdg-data" / "msb-circus" / "system-skills"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    config = MsbConfig(
        image=ImageConfig(tag="img:1"), skills=SkillsConfig(store=str(store))
    )
    behavior = FakeInvoker.behavior
    behavior["calls"] = []

    def fake_resolve_invoker(_cwd: Path):
        monkeypatch.setattr(cli_module, "MsbInvoker", FakeInvoker)
        return config, cast(MsbInvoker, FakeInvoker(Path("/fake")))

    def fake_run_skills_installer(invoker, config, args):
        behavior["installer_args"] = list(args)
        behavior["calls"].append(["run-skills-installer", *args])
        return behavior.get("installer_rc", 0)

    monkeypatch.setattr(cli_module, "_resolve_invoker", fake_resolve_invoker)
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


def test_skills_command_without_args_prints_usage_and_exits_2(installer) -> None:
    """Bare ``skills`` prints the store path and exits with code 2."""
    _behavior, _config, store = installer
    result = CliRunner().invoke(cli_module.app, ["skills"])
    assert result.exit_code == 2
    assert "Usage: msb-circus skills" in result.output
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
