"""Tests for the linked Git worktree guard of msb-circus."""

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from msb_circus import cli as cli_module
from msb_circus.cli import app
from msb_circus.exceptions import ConfigurationError
from msb_circus.gitworktree import ensure_not_linked_worktree, find_linked_worktree

runner = CliRunner()


def make_repo(root: Path) -> Path:
    """Create a minimal main repository layout and return its ``.git``."""
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    return git_dir


def make_linked_worktree(main: Path, worktree: Path, *, absolute: bool = True) -> None:
    """Create the files ``git worktree add`` writes for ``worktree``."""
    meta = main / ".git" / "worktrees" / worktree.name
    meta.mkdir(parents=True)
    (meta / "commondir").write_text("../..\n")
    worktree.mkdir(parents=True)
    target = (
        meta if absolute else Path("..") / main.name / ".git/worktrees" / worktree.name
    )
    (worktree / ".git").write_text(f"gitdir: {target}\n")


def make_submodule(main: Path, sub: Path) -> None:
    """Create the files ``git submodule add`` writes for ``sub``."""
    meta = main / ".git" / "modules" / sub.name
    meta.mkdir(parents=True)
    (meta / "HEAD").write_text("ref: refs/heads/main\n")
    sub.mkdir(parents=True)
    (sub / ".git").write_text(f"gitdir: ../.git/modules/{sub.name}\n")


def test_find_linked_worktree_detects_worktree_root(tmp_path):
    make_repo(tmp_path / "main")
    make_linked_worktree(tmp_path / "main", tmp_path / "wt")

    assert find_linked_worktree(tmp_path / "wt") == (
        tmp_path / "wt",
        (tmp_path / "main" / ".git").resolve(),
    )


def test_find_linked_worktree_resolves_relative_gitdir(tmp_path):
    make_repo(tmp_path / "main")
    make_linked_worktree(tmp_path / "main", tmp_path / "wt", absolute=False)

    found = find_linked_worktree(tmp_path / "wt")

    assert found is not None
    assert found[1] == (tmp_path / "main" / ".git").resolve()


def test_find_linked_worktree_walks_up_from_subdirectory(tmp_path):
    make_repo(tmp_path / "main")
    make_linked_worktree(tmp_path / "main", tmp_path / "wt")
    subdir = tmp_path / "wt" / "src" / "pkg"
    subdir.mkdir(parents=True)

    found = find_linked_worktree(subdir)

    assert found is not None
    assert found[0] == tmp_path / "wt"


def test_find_linked_worktree_ignores_main_checkout(tmp_path):
    make_repo(tmp_path / "main")
    make_linked_worktree(tmp_path / "main", tmp_path / "main" / ".worktrees" / "wt")

    assert find_linked_worktree(tmp_path / "main") is None


def test_find_linked_worktree_stops_at_nearest_repository(tmp_path):
    # A regular repository nested in a linked worktree wins.
    make_repo(tmp_path / "main")
    make_linked_worktree(tmp_path / "main", tmp_path / "wt")
    make_repo(tmp_path / "wt" / "nested")

    assert find_linked_worktree(tmp_path / "wt" / "nested") is None


def test_find_linked_worktree_ignores_submodule(tmp_path):
    make_repo(tmp_path / "main")
    make_submodule(tmp_path / "main", tmp_path / "main" / "sub")

    assert find_linked_worktree(tmp_path / "main" / "sub") is None


def test_find_linked_worktree_ignores_malformed_git_file(tmp_path):
    (tmp_path / ".git").write_text("not a gitdir line\n")

    assert find_linked_worktree(tmp_path) is None


def test_find_linked_worktree_ignores_dangling_gitdir(tmp_path):
    (tmp_path / ".git").write_text(f"gitdir: {tmp_path / 'missing'}\n")

    assert find_linked_worktree(tmp_path) is None


def test_find_linked_worktree_returns_none_outside_repository(tmp_path):
    assert find_linked_worktree(tmp_path) is None


def test_ensure_not_linked_worktree_raises_with_paths(tmp_path):
    make_repo(tmp_path / "main")
    make_linked_worktree(tmp_path / "main", tmp_path / "wt")

    with pytest.raises(ConfigurationError) as excinfo:
        ensure_not_linked_worktree(tmp_path / "wt")

    message = str(excinfo.value)
    assert str(tmp_path / "wt") in message
    assert str((tmp_path / "main" / ".git").resolve()) in message


class NeverInvoker:
    """Fails the test when the CLI talks to msb."""

    calls: ClassVar[list[list[str]]] = []

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        NeverInvoker.calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")


@pytest.fixture
def linked_worktree(tmp_path, monkeypatch):
    """Return a linked worktree path and stub out the msb binary."""
    NeverInvoker.calls = []
    monkeypatch.setattr(cli_module, "resolve_msb_binary", lambda b: Path("/fake"))
    monkeypatch.setattr(cli_module, "check_version", lambda binary: "0.7.12")
    monkeypatch.setattr(cli_module, "MsbInvoker", NeverInvoker)
    make_repo(tmp_path / "main")
    make_linked_worktree(tmp_path / "main", tmp_path / "wt")
    return tmp_path / "wt"


@pytest.mark.parametrize(
    ("command", "trailing"), [("up", []), ("start", []), ("exec", ["--", "true"])]
)
def test_sandbox_commands_fail_in_linked_worktree(linked_worktree, command, trailing):
    result = runner.invoke(
        app, [command, "--workspace", str(linked_worktree), *trailing]
    )

    assert result.exit_code == 1
    assert "linked Git worktree" in result.output
    assert NeverInvoker.calls == []


def test_stop_still_works_in_linked_worktree(linked_worktree):
    result = runner.invoke(app, ["stop", "--workspace", str(linked_worktree)])

    assert "linked Git worktree" not in result.output
