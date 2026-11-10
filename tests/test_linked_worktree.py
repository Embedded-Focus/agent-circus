"""Tests for the linked Git worktree guard of agent-circus."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from agent_circus.cli import app
from agent_circus.exceptions import ConfigurationError
from agent_circus.gitworktree import ensure_not_linked_worktree, find_linked_worktree

runner = CliRunner()


def make_repo(root: Path) -> None:
    """Create a minimal main repository layout."""
    (root / ".git").mkdir(parents=True)


def make_linked_worktree(main: Path, worktree: Path) -> None:
    """Create the files ``git worktree add`` writes for ``worktree``."""
    meta = main / ".git" / "worktrees" / worktree.name
    meta.mkdir(parents=True)
    (meta / "commondir").write_text("../..\n")
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {meta}\n")


def make_submodule(main: Path, sub: Path) -> None:
    """Create the files ``git submodule add`` writes for ``sub``."""
    meta = main / ".git" / "modules" / sub.name
    meta.mkdir(parents=True)
    sub.mkdir(parents=True)
    (sub / ".git").write_text(f"gitdir: ../.git/modules/{sub.name}\n")


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """Return a linked worktree of a main repository in ``tmp_path``."""
    make_repo(tmp_path / "main")
    make_linked_worktree(tmp_path / "main", tmp_path / "wt")
    return tmp_path / "wt"


def test_find_linked_worktree_detects_worktree_root(worktree: Path) -> None:
    main_git = (worktree.parent / "main" / ".git").resolve()
    assert find_linked_worktree(worktree) == (worktree, main_git)


def test_find_linked_worktree_walks_up_from_subdirectory(worktree: Path) -> None:
    subdir = worktree / "src"
    subdir.mkdir()

    found = find_linked_worktree(subdir)

    assert found is not None
    assert found[0] == worktree


def test_find_linked_worktree_ignores_main_checkout(worktree: Path) -> None:
    assert find_linked_worktree(worktree.parent / "main") is None


def test_find_linked_worktree_ignores_submodule(tmp_path: Path) -> None:
    make_repo(tmp_path / "main")
    make_submodule(tmp_path / "main", tmp_path / "main" / "sub")

    assert find_linked_worktree(tmp_path / "main" / "sub") is None


def test_find_linked_worktree_ignores_malformed_git_file(tmp_path: Path) -> None:
    (tmp_path / ".git").write_text("not a gitdir line\n")

    assert find_linked_worktree(tmp_path) is None


def test_ensure_not_linked_worktree_raises(worktree: Path) -> None:
    with pytest.raises(ConfigurationError, match="linked Git worktree"):
        ensure_not_linked_worktree(worktree)


@patch("agent_circus.commands.up.build_compose_context")
def test_up_fails_in_linked_worktree(mock_build_ctx: MagicMock, worktree: Path) -> None:
    result = runner.invoke(app, ["up", "--workspace", str(worktree)])

    assert result.exit_code == 1
    assert "linked Git worktree" in result.output
    mock_build_ctx.assert_not_called()


@patch("agent_circus.commands.exec_.build_compose_context")
def test_exec_fails_in_linked_worktree(
    mock_build_ctx: MagicMock, worktree: Path
) -> None:
    result = runner.invoke(
        app, ["exec", "--workspace", str(worktree), "claude-code", "--", "bash"]
    )

    assert result.exit_code == 1
    assert "linked Git worktree" in result.output
    mock_build_ctx.assert_not_called()
