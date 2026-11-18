"""Tests for the skills installer module (sandbox boot, exec, cleanup)."""

import fcntl
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, cast

import pytest

from msb_circus import skills_installer
from msb_circus.config import ImageConfig, MsbConfig, SkillsConfig
from msb_circus.exceptions import MsbCommandError
from msb_circus.msb import MsbInvoker
from msb_circus.skills_installer import InstallerError, run_skills_installer


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
    """Set up a fake invoker, a store under XDG_DATA_HOME and fake exec.

    :param tmp_path: Per-test temporary directory.
    :param monkeypatch: Pytest environment patch helper.
    :returns: ``(FakeInvoker.behavior, config, store)`` tuple.
    """
    FakeInvoker.behavior = {}
    store = tmp_path / "xdg-data" / "msb-circus" / "system-skills"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setattr(skills_installer, "MsbInvoker", FakeInvoker)
    config = MsbConfig(
        image=ImageConfig(tag="img:1"), skills=SkillsConfig(store=str(store))
    )
    calls = FakeInvoker.behavior["calls"] = []

    def fake_subprocess_run(argv, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=FakeInvoker.behavior.get("exec_rc", 0))

    monkeypatch.setattr(skills_installer.subprocess, "run", fake_subprocess_run)
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
    rc = run_skills_installer(
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
    run_skills_installer(
        cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["add", "owner/repo"]
    )
    exec_argv = behavior["calls"][3]
    assert exec_argv[-5:] == ["--", "skills", "add", "-g", "owner/repo"]


def test_installer_propagates_skills_exit_code(installer) -> None:
    """A failing skills CLI exit code is returned; remove still runs."""
    behavior, config, _store = installer
    behavior["exec_rc"] = 3
    rc = run_skills_installer(
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
    monkeypatch.setattr(skills_installer.subprocess, "run", raise_interrupt)
    with pytest.raises(KeyboardInterrupt):
        run_skills_installer(
            cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["add", "owner/repo"]
        )
    assert behavior["calls"][-1][0] == "remove"


def test_installer_removes_stopped_installer_before_boot(installer) -> None:
    """A stopped installer sandbox is removed, then the boot runs."""
    behavior, config, _store = installer
    behavior["installer_entry"] = {"name": "circus-skills", "status": "Stopped"}
    rc = run_skills_installer(
        cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["add", "owner/repo"]
    )
    assert rc == 0
    kinds = _kinds(behavior["calls"])
    assert kinds == ["image", "list", "remove", "run", "exec", "remove"]


def test_installer_removes_running_leftover_before_boot(installer) -> None:
    """With the lock held, even a running leftover is removed, then booted."""
    behavior, config, _store = installer
    behavior["installer_entry"] = {"name": "circus-skills", "status": "Running"}
    rc = run_skills_installer(
        cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["add", "owner/repo"]
    )
    assert rc == 0
    kinds = _kinds(behavior["calls"])
    assert kinds == ["image", "list", "remove", "run", "exec", "remove"]


def test_installer_fails_cleanly_when_lock_cannot_be_opened(
    installer, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unusable state directory gives an InstallerError, no traceback."""
    behavior, config, _store = installer
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))
    with pytest.raises(InstallerError) as excinfo:
        run_skills_installer(
            cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["add", "owner/repo"]
        )
    assert excinfo.value.exit_code == 1
    assert "cannot open the installer lock" in str(excinfo.value)
    assert not [c for c in behavior["calls"] if c[0] in ("list", "run")]


def test_installer_fails_without_cached_image(installer) -> None:
    """A missing image fails with exit 1 and never boots a sandbox."""
    behavior, config, _store = installer
    behavior["image_rc"] = 1
    with pytest.raises(InstallerError) as excinfo:
        run_skills_installer(
            cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["add", "owner/repo"]
        )
    assert excinfo.value.exit_code == 1
    assert "run" not in _kinds(behavior["calls"])
    assert "msb-circus build first" in str(excinfo.value)


def test_installer_rejects_unknown_subcommand(installer) -> None:
    """A project-scoped subcommand fails with exit code 2 and no msb call."""
    _behavior, config, _store = installer
    with pytest.raises(InstallerError) as excinfo:
        run_skills_installer(
            cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["init"]
        )
    assert excinfo.value.exit_code == 2
    assert not _behavior["calls"]


def test_installer_removes_sandbox_when_boot_fails(installer) -> None:
    """A failed boot exits with 1 and still removes."""
    behavior, config, _store = installer
    behavior["run_error"] = MsbCommandError("boot failed", returncode=1, stderr="")
    with pytest.raises(InstallerError) as excinfo:
        run_skills_installer(
            cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["add", "owner/repo"]
        )
    assert excinfo.value.exit_code == 1
    assert _kinds(behavior["calls"]) == ["image", "list", "run", "remove"]
    assert "cannot boot the installer sandbox" in str(excinfo.value)


def test_installer_removes_sandbox_on_interrupt_during_boot(installer) -> None:
    """Ctrl-C during the boot still removes the installer sandbox."""
    behavior, config, _store = installer
    behavior["run_error"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        run_skills_installer(
            cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["add", "owner/repo"]
        )
    assert _kinds(behavior["calls"])[-1] == "remove"


def test_installer_fails_cleanly_when_leftover_removal_fails(installer) -> None:
    """A failing removal of a stopped leftover exits with 1, no boot."""
    behavior, config, _store = installer
    behavior["installer_entry"] = {"name": "circus-skills", "status": "Stopped"}
    behavior["remove_fails"] = True
    with pytest.raises(InstallerError) as excinfo:
        run_skills_installer(
            cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["add", "owner/repo"]
        )
    assert excinfo.value.exit_code == 1
    assert "run" not in _kinds(behavior["calls"])
    assert "cannot remove the leftover installer sandbox" in str(excinfo.value)


class _TtyIO(io.StringIO):
    """String stream that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


class _PipeIO(io.StringIO):
    """String stream that claims not to be a terminal."""

    def isatty(self) -> bool:
        return False


@pytest.mark.parametrize(
    ("stdin", "stdout", "expected"),
    [
        (_TtyIO(), _TtyIO(), True),
        (_PipeIO(), _TtyIO(), False),
        (_TtyIO(), _PipeIO(), False),
    ],
)
def test_installer_uses_tty_only_on_a_terminal(
    installer, monkeypatch: pytest.MonkeyPatch, stdin, stdout, expected
) -> None:
    """``--tty`` is sent only when both stdin and stdout are terminals."""
    behavior, config, _store = installer
    monkeypatch.setattr(skills_installer.sys, "stdin", stdin)
    monkeypatch.setattr(skills_installer.sys, "stdout", stdout)
    run_skills_installer(cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["list"])
    exec_argv = behavior["calls"][3]
    options = exec_argv[: exec_argv.index("--")]
    assert ("--tty" in options) is expected
    assert "--no-tty" not in options


def test_installer_fails_when_lock_is_held_elsewhere(installer) -> None:
    """A lock held by another process fails the run before any msb call."""
    import os

    behavior, config, _store = installer
    lock_path = Path(os.environ["XDG_STATE_HOME"]) / "msb-circus/skills-installer.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(InstallerError) as excinfo:
            run_skills_installer(
                cast(MsbInvoker, FakeInvoker(Path("/fake"))),
                config,
                ["add", "owner/repo"],
            )
    assert excinfo.value.exit_code == 1
    assert "another msb-circus skills command" in str(excinfo.value)
    # The lock is taken after the image check: no list/boot/exec happens.
    assert not [c for c in behavior["calls"] if c[0] in ("list", "run", "remove")]


def test_installer_releases_the_lock_after_a_successful_run(installer) -> None:
    """A second run succeeds right after a first one (lock is free)."""
    _behavior, config, _store = installer
    first = run_skills_installer(
        cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["list"]
    )
    second = run_skills_installer(
        cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["list"]
    )
    assert (first, second) == (0, 0)


def test_installer_releases_the_lock_after_a_failing_run(installer) -> None:
    """A failed skills CLI run still releases the lock."""
    behavior, config, _store = installer
    behavior["exec_rc"] = 3
    first = run_skills_installer(
        cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["list"]
    )
    behavior["exec_rc"] = 0
    second = run_skills_installer(
        cast(MsbInvoker, FakeInvoker(Path("/fake"))), config, ["list"]
    )
    assert (first, second) == (3, 0)
