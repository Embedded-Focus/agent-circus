"""Tests for the ``msb-circus skills`` command and its installer flow."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest
import typer

import msb_circus.cli as cli_module
from msb_circus.cli import _run_skills_installer
from msb_circus.config import ImageConfig, MsbConfig, SkillsConfig
from msb_circus.msb import MsbInvoker


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
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.fixture
def installer(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Set up a fake invoker, a store under XDG_DATA_HOME and fake exec.

    :param tmp_path: Per-test temporary directory.
    :param monkeypatch: Pytest environment patch helper.
    :returns: ``(FakeInvoker.behavior, config, store)`` tuple.
    """
    FakeInvoker.behavior = {}
    store = tmp_path / "xdg-data" / "msb-circus" / "system-skills"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setattr(cli_module, "MsbInvoker", FakeInvoker)
    config = MsbConfig(
        image=ImageConfig(tag="img:1"), skills=SkillsConfig(store=str(store))
    )
    calls = FakeInvoker.behavior["calls"] = []

    def fake_subprocess_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=FakeInvoker.behavior.get("exec_rc", 0))

    monkeypatch.setattr(cli_module.subprocess, "run", fake_subprocess_run)
    return FakeInvoker.behavior, config, store


def _kinds(calls: list[list]) -> list[str]:
    """Return the subcommand of each recorded call (binary argv aware).

    :param calls: Recorded argv lists (invoker calls and full exec argv).
    :returns: Subcommand names ("exec" for the full exec argv).
    """
    return [c[1] if str(c[0]) == "/fake" else c[0] for c in calls]


def test_installer_happy_path_boots_mounts_and_removes(installer) -> None:
    """inspect, list, run, exec and remove run in that order."""
    behavior, config, store = installer
    rc = _run_skills_installer(
        cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["add", "owner/repo"]
    )
    assert rc == 0
    kinds = _kinds(behavior["calls"])
    assert kinds == ["image", "list", "run", "exec", "remove"]
    run_call = behavior["calls"][2]
    assert run_call[run_call.index("--name") + 1] == "circus-skills"
    assert run_call[run_call.index("--volume") + 1] == (
        f"{store / '.agents'}:/root/.agents"
    )
    envs = [run_call[i + 1] for i, arg in enumerate(run_call) if arg == "--env"]
    assert sorted(envs) == [
        "DISABLE_TELEMETRY=1",
        "DO_NOT_TRACK=1",
    ]
    for flag in ("--mount-owned", "--mount-named", "--port", "--vsock"):
        assert flag not in run_call


def test_installer_exec_receives_built_command(installer) -> None:
    """The forwarded exec argv ends with the built skills command."""
    behavior, config, _store = installer
    _run_skills_installer(
        cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["add", "owner/repo"]
    )
    exec_argv = behavior["calls"][3]
    assert exec_argv[-5:] == ["--", "skills", "add", "-g", "owner/repo"]


def test_installer_propagates_skills_exit_code(installer) -> None:
    """A failing skills CLI exit code is returned; remove still runs."""
    behavior, config, _store = installer
    behavior["exec_rc"] = 3
    rc = _run_skills_installer(
        cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["list"]
    )
    assert rc == 3
    assert behavior["calls"][-1][0] == "remove"


def test_installer_removes_sandbox_on_keyboard_interrupt(
    installer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C during the skills run still removes the installer sandbox."""

    def raise_interrupt(argv, **kwargs):
        raise KeyboardInterrupt

    behavior, config, _store = installer
    monkeypatch.setattr(cli_module.subprocess, "run", raise_interrupt)
    with pytest.raises(KeyboardInterrupt):
        _run_skills_installer(
            cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["add", "owner/repo"]
        )
    assert behavior["calls"][-1][0] == "remove"


def test_installer_fails_while_another_installer_is_running(installer, capsys) -> None:
    """A running circus-skills sandbox in the list blocks with exit 1."""
    behavior, config, _store = installer
    behavior["installer_entry"] = {"name": "circus-skills", "status": "Running"}
    with pytest.raises(typer.Exit) as excinfo:
        _run_skills_installer(
            cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["add", "owner/repo"]
        )
    assert excinfo.value.exit_code == 1
    kinds = _kinds(behavior["calls"])
    assert "run" not in kinds
    assert "another skills command is running" in capsys.readouterr().err


def test_installer_removes_stopped_installer_before_boot(installer) -> None:
    """A stopped installer sandbox is removed, then the boot runs."""
    behavior, config, _store = installer
    behavior["installer_entry"] = {"name": "circus-skills", "status": "Stopped"}
    rc = _run_skills_installer(
        cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["add", "owner/repo"]
    )
    assert rc == 0
    kinds = _kinds(behavior["calls"])
    assert kinds == ["image", "list", "remove", "run", "exec", "remove"]


def test_installer_fails_without_cached_image(installer, capsys) -> None:
    """A missing image fails with exit 1 and never boots a sandbox."""
    behavior, config, _store = installer
    behavior["image_rc"] = 1
    with pytest.raises(typer.Exit) as excinfo:
        _run_skills_installer(
            cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["add", "owner/repo"]
        )
    assert excinfo.value.exit_code == 1
    kinds = _kinds(behavior["calls"])
    assert "run" not in kinds
    assert "msb-circus build first" in capsys.readouterr().err


def test_installer_rejects_unknown_subcommand(installer, capsys) -> None:
    """A project-scoped subcommand exits with code 2 and no msb call."""
    _behavior, config, _store = installer
    with pytest.raises(typer.Exit) as excinfo:
        _run_skills_installer(
            cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["init"]
        )
    assert excinfo.value.exit_code == 2
    assert "Error:" in capsys.readouterr().err


def test_skills_command_forwards_args_verbatim(
    installer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All extra CLI arguments arrive at the installer unchanged."""
    from typer.testing import CliRunner

    behavior, config, _store = installer
    monkeypatch.setattr(
        cli_module,
        "_resolve_invoker",
        lambda _cwd: (config, cast(MsbInvoker, FakeInvoker(Path("/fake")))),
    )
    result = CliRunner().invoke(
        cli_module.app, ["skills", "add", "owner/repo", "--skill", "x", "-y"]
    )
    assert result.exit_code == 0
    exec_argv = behavior["calls"][3]
    assert exec_argv[-8:] == [
        "--",
        "skills",
        "add",
        "-g",
        "owner/repo",
        "--skill",
        "x",
        "-y",
    ]


def test_skills_command_without_args_prints_usage_and_exits_2(
    installer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bare ``skills`` prints the store path and exits with code 2."""
    from typer.testing import CliRunner

    _behavior, config, store = installer
    monkeypatch.setattr(
        cli_module,
        "_resolve_invoker",
        lambda _cwd: (config, cast(MsbInvoker, FakeInvoker(Path("/fake")))),
    )
    result = CliRunner().invoke(cli_module.app, ["skills"])
    assert result.exit_code == 2
    assert "Usage: msb-circus skills" in result.output
    assert str(store) in result.output
