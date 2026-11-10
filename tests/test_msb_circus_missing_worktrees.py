"""Worktree tests: worktrees missing on the host and the sandbox worktrees dir."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from msb_circus.gitworktree import (
    ensure_sandbox_worktrees_dir,
    find_missing_worktrees,
    has_sandbox_worktrees,
)


def register_worktree(main: Path, worktree: Path) -> None:
    """Write the registry entry ``git worktree add`` creates in ``main``."""
    meta = main / ".git" / "worktrees" / worktree.name
    meta.mkdir(parents=True)
    (meta / "gitdir").write_text(f"{worktree / '.git'}\n")


def test_find_missing_worktrees_returns_empty_without_git_dir(tmp_path):
    assert find_missing_worktrees(tmp_path) == []


def test_find_missing_worktrees_returns_empty_without_registry(tmp_path):
    (tmp_path / ".git").mkdir()
    assert find_missing_worktrees(tmp_path) == []


def test_find_missing_worktrees_ignores_existing_worktree(tmp_path):
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "repo-feature"
    register_worktree(tmp_path, worktree)
    worktree.mkdir()
    assert find_missing_worktrees(tmp_path) == []


def test_find_missing_worktrees_reports_missing_worktree(tmp_path):
    (tmp_path / ".git").mkdir()
    register_worktree(tmp_path, tmp_path / "repo-feature")
    assert find_missing_worktrees(tmp_path) == [tmp_path / "repo-feature"]


def test_find_missing_worktrees_skips_entry_without_gitdir_file(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "worktrees" / "broken").mkdir(parents=True)
    assert find_missing_worktrees(tmp_path) == []


def test_find_missing_worktrees_resolves_relative_gitdir(tmp_path):
    (tmp_path / "repo" / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (tmp_path / "repo" / ".git" / "worktrees" / "wt" / "gitdir").write_text(
        "../../../../wt/.git\n"
    )
    assert find_missing_worktrees(tmp_path / "repo") == [tmp_path / "wt"]


def test_ensure_sandbox_worktrees_dir_creates_dir_and_gitignore(tmp_path):
    (tmp_path / ".git").mkdir()
    directory = ensure_sandbox_worktrees_dir(tmp_path)
    assert directory == tmp_path / ".agent-circus" / "worktrees"
    assert directory is not None
    assert (directory / ".gitignore").read_text() == "*\n"


def test_ensure_sandbox_worktrees_dir_keeps_existing_gitignore(tmp_path):
    (tmp_path / ".git").mkdir()
    ignore = tmp_path / ".agent-circus" / "worktrees" / ".gitignore"
    ignore.parent.mkdir(parents=True)
    ignore.write_text("custom\n")
    ensure_sandbox_worktrees_dir(tmp_path)
    assert ignore.read_text() == "custom\n"


def test_ensure_sandbox_worktrees_dir_skips_non_repository(tmp_path):
    assert ensure_sandbox_worktrees_dir(tmp_path) is None
    assert not (tmp_path / ".agent-circus").exists()


def test_ensure_sandbox_worktrees_dir_skips_linked_worktree(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /somewhere\n")
    assert ensure_sandbox_worktrees_dir(tmp_path) is None
    assert not (tmp_path / ".agent-circus").exists()


def test_has_sandbox_worktrees_false_for_only_gitignore(tmp_path):
    (tmp_path / ".agent-circus" / "worktrees").mkdir(parents=True)
    (tmp_path / ".agent-circus" / "worktrees" / ".gitignore").write_text("*\n")
    assert not has_sandbox_worktrees(tmp_path)


def test_has_sandbox_worktrees_true_with_subdirectory(tmp_path):
    (tmp_path / ".agent-circus" / "worktrees" / "x").mkdir(parents=True)
    assert has_sandbox_worktrees(tmp_path)


def test_has_sandbox_worktrees_false_without_directory(tmp_path):
    assert not has_sandbox_worktrees(tmp_path)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_sandbox_worktrees_dir_is_ignored_by_git(tmp_path):
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, env=env, check=True)
    ensure_sandbox_worktrees_dir(tmp_path)
    tracked = tmp_path / ".agent-circus" / "worktrees" / "x"
    tracked.mkdir()
    (tracked / "file").write_text("content\n")
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == ""
