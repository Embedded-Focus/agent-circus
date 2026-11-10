"""Tests for worktrees registered in the sandbox but missing on the host."""

from pathlib import Path

from msb_circus.gitworktree import find_missing_worktrees


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
