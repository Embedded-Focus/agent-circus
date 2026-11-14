"""Worktree repair tests: worktrees that Git recorded via the sandbox path."""

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from msb_circus import cli
from msb_circus.config import parse_config
from msb_circus.gitworktree import (
    find_guest_path_worktrees,
    repair_guest_path_worktrees,
)

REAL_GIT = "/usr/bin/git" if Path("/usr/bin/git").is_file() else shutil.which("git")
#: Always a string; tests are skipped when Git is unavailable anyway.
REAL_GIT_BIN = REAL_GIT or "git"

pytestmark = pytest.mark.skipif(REAL_GIT is None, reason="needs git")


def run_git(args: list[str], *, cwd: Path) -> None:
    """Run the real Git binary with the given arguments.

    :param args: Git arguments (without the executable).
    :param cwd: Directory to run Git in.
    :raises subprocess.CalledProcessError: If Git fails.
    """
    subprocess.run([REAL_GIT_BIN, *args], cwd=cwd, check=True)


def make_repo(tmp_path: Path) -> Path:
    """Create a Git repository with one initial commit in ``tmp_path``.

    :param tmp_path: Directory to create ``repo`` in.
    :returns: The repository directory.
    :raises subprocess.CalledProcessError: If Git fails.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(["init", "-q"], cwd=repo)
    run_git(["commit", "-q", "--allow-empty", "-m", "init"], cwd=repo)
    return repo


def simulate_guest_recording(repo: Path) -> None:
    """Rewrite the worktree links as if Git had run below ``/guest-root``.

    :param repo: Repository with a registered worktree ``x``.
    """
    (repo / ".git" / "worktrees" / "x" / "gitdir").write_text(
        "/guest-root/.agent-circus/worktrees/x/.git\n"
    )
    (repo / ".agent-circus" / "worktrees" / "x" / ".git").write_text(
        "gitdir: /guest-root/.git/worktrees/x\n"
    )


@pytest.fixture
def guest_worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a repository whose linked worktree ``x`` records a guest path.

    Isolates Git from user config and creates ``repo`` with one commit and
    a linked worktree at ``.agent-circus/worktrees/x``. Both worktree
    links are then rewritten to point below the fake guest root
    ``/guest-root``.

    :param tmp_path: Directory to create the repository in.
    :param monkeypatch: Fixture to isolate Git's configuration.
    :returns: The repository directory.
    :raises subprocess.CalledProcessError: If Git fails.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    repo = make_repo(tmp_path)
    run_git(["worktree", "add", "-q", ".agent-circus/worktrees/x", "-b", "x"], cwd=repo)
    simulate_guest_recording(repo)
    return repo


def test_find_guest_path_worktrees_maps_to_host_path(tmp_path, guest_worktree):
    assert find_guest_path_worktrees(guest_worktree, "/guest-root") == [
        guest_worktree / ".agent-circus/worktrees/x"
    ]


def test_find_guest_path_worktrees_ignores_host_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    repo = make_repo(tmp_path)
    run_git(["worktree", "add", "-q", ".agent-circus/worktrees/x", "-b", "x"], cwd=repo)
    assert find_guest_path_worktrees(repo, "/guest-root") == []


def test_find_guest_path_worktrees_ignores_missing_host_dir(guest_worktree):
    (guest_worktree / ".git" / "worktrees" / "x" / "gitdir").write_text(
        "/guest-root/elsewhere/y/.git\n"
    )
    assert find_guest_path_worktrees(guest_worktree, "/guest-root") == []


def test_find_guest_path_worktrees_requires_path_boundary(guest_worktree):
    assert find_guest_path_worktrees(guest_worktree, "/guest") == []


def test_find_guest_path_worktrees_without_registry_returns_empty(tmp_path):
    (tmp_path / ".git").mkdir()
    assert find_guest_path_worktrees(tmp_path, "/guest-root") == []


def test_repair_guest_path_worktrees_fixes_both_links(guest_worktree):
    worktree = guest_worktree / ".agent-circus" / "worktrees" / "x"
    repaired = repair_guest_path_worktrees(
        guest_worktree, "/guest-root", git=REAL_GIT_BIN
    )
    assert repaired == [worktree]
    gitdir = (guest_worktree / ".git" / "worktrees" / "x" / "gitdir").read_text()
    assert str(guest_worktree) in gitdir
    assert "/guest-root" not in gitdir
    dot_git = (worktree / ".git").read_text()
    assert str(guest_worktree) in dot_git
    result = subprocess.run(
        [REAL_GIT_BIN, "-C", str(worktree), "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0


def test_repair_guest_path_worktrees_noop_starts_no_process(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    repo = make_repo(tmp_path)
    assert (
        repair_guest_path_worktrees(repo, "/guest-root", git="/nonexistent/git") == []
    )


def test_repair_guest_path_worktrees_raises_when_git_missing(guest_worktree):
    with pytest.raises(OSError):
        repair_guest_path_worktrees(
            guest_worktree, "/guest-root", git="/nonexistent/git"
        )


def test_repair_skips_when_guest_root_equals_workspace(guest_worktree):
    assert (
        repair_guest_path_worktrees(
            guest_worktree, str(guest_worktree), git="/nonexistent/git"
        )
        == []
    )


def _patch_repair_recorder(
    monkeypatch: pytest.MonkeyPatch, result: Any
) -> list[tuple[Path, str]]:
    """Patch ``cli.repair_guest_path_worktrees`` with a recording fake.

    :param monkeypatch: Fixture to patch the CLI module.
    :param result: Paths the fake returns, or an exception it raises.
    :returns: Recorded ``(workspace, guest_root)`` calls.
    """
    calls: list[tuple[Path, str]] = []

    def fake(workspace: Path, guest_root: str, **kwargs: Any) -> list[Path]:
        calls.append((workspace, guest_root))
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(cli, "repair_guest_path_worktrees", fake)
    return calls


def test_cli_repair_runs_with_mirror(tmp_path, monkeypatch, caplog):
    repaired = [tmp_path / ".agent-circus" / "worktrees" / "x"]
    calls = _patch_repair_recorder(monkeypatch, repaired)
    config = parse_config({"git": {"worktree_mirror": True}})
    with caplog.at_level(logging.INFO):
        cli._repair_guest_path_worktrees(config, tmp_path)
    assert calls == [(tmp_path, "/workspace")]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any("Repaired Git worktree" in m and str(repaired[0]) in m for m in infos)


def test_cli_repair_skipped_without_mirror(tmp_path, monkeypatch):
    calls = _patch_repair_recorder(monkeypatch, [])
    config = parse_config({"git": {"worktree_mirror": False}})
    cli._repair_guest_path_worktrees(config, tmp_path)
    assert calls == []


def test_cli_repair_warns_on_failure(tmp_path, monkeypatch, caplog):
    calls = _patch_repair_recorder(
        monkeypatch, subprocess.CalledProcessError(128, ["git"], stderr="boom")
    )
    config = parse_config({"git": {"worktree_mirror": True}})
    with caplog.at_level(logging.WARNING):
        cli._repair_guest_path_worktrees(config, tmp_path)
    assert calls == [(tmp_path, "/workspace")]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("boom" in m for m in warnings)

    calls = _patch_repair_recorder(monkeypatch, OSError("no git"))
    with caplog.at_level(logging.WARNING):
        cli._repair_guest_path_worktrees(config, tmp_path)
    assert calls[-1] == (tmp_path, "/workspace")
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("no git" in m for m in warnings)


def test_find_guest_path_worktrees_ignores_guest_root_itself(guest_worktree):
    gitdir = guest_worktree / ".git" / "worktrees" / "x" / "gitdir"
    gitdir.write_text("/guest-root/.git\n", encoding="utf-8")
    assert find_guest_path_worktrees(guest_worktree, "/guest-root") == []


def test_repair_guest_path_worktrees_retries_stale_dot_git(guest_worktree, tmp_path):
    # A stub git that fixes the worktree's .git file only on its second call.
    worktree = guest_worktree / ".agent-circus" / "worktrees" / "x"
    calls = tmp_path / "calls"
    stub = tmp_path / "stub-git"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo call >> "{calls}"\n'
        f'[ "$(wc -l < "{calls}")" -ge 2 ] || exit 0\n'
        f'printf "gitdir: {guest_worktree}/.git/worktrees/x\\n" > "{worktree}/.git"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    repaired = repair_guest_path_worktrees(guest_worktree, "/guest-root", git=str(stub))
    assert repaired == [worktree]
    assert calls.read_text(encoding="utf-8").count("call") == 2
    assert str(guest_worktree) in (worktree / ".git").read_text(encoding="utf-8")


def test_repair_guest_path_worktrees_stops_after_two_passes(guest_worktree, tmp_path):
    calls = tmp_path / "calls"
    stub = tmp_path / "stub-git"
    stub.write_text(f'#!/bin/sh\necho call >> "{calls}"\n', encoding="utf-8")
    stub.chmod(0o755)
    repair_guest_path_worktrees(guest_worktree, "/guest-root", git=str(stub))
    assert calls.read_text(encoding="utf-8").count("call") == 2
