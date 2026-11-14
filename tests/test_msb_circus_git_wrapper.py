"""Tests for the guest git wrapper and its install script."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from msb_circus.exec_ import _INSTALL_EXECUTABLE_SCRIPT
from msb_circus.gitworktree import git_wrapper_script


def _realpath_works() -> bool:
    """Tell whether GNU ``realpath -m`` is available (False on errors)."""
    try:
        probe = subprocess.run(
            ["realpath", "-m", "/x/.."], check=False, capture_output=True
        )
        return probe.returncode == 0
    except FileNotFoundError:
        return False


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None
    or shutil.which("git") is None
    or not _realpath_works(),
    reason="needs bash, git, and GNU realpath",
)


#: Real Git binary. A guest sandbox has the wrapper first on PATH, and
#: pointing the wrapper at itself would make it exec itself forever.
REAL_GIT = "/usr/bin/git" if Path("/usr/bin/git").is_file() else shutil.which("git")


def git_env() -> dict[str, str]:
    """Return an environment that isolates Git from user config."""
    return {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "MSB_CIRCUS_REAL_GIT": REAL_GIT or "git",
    }


def wrapper(
    cwd: Path, *args: str, guest_root: str = "", mirror_root: str = ""
) -> subprocess.CompletedProcess[str]:
    """Run the rendered wrapper like ``git <args>`` in ``cwd``."""
    script = git_wrapper_script(guest_root, mirror_root)
    return subprocess.run(
        ["bash", "-c", script, "git", *args],
        check=False,
        cwd=cwd,
        env=git_env(),
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Create a repository with one commit in ``tmp_path/repo``."""
    path = tmp_path / "repo"
    path.mkdir()
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run([REAL_GIT or "git", *args], cwd=path, env=git_env(), check=True)
    return path


def test_git_wrapper_passes_other_commands_through(repo: Path):
    result = wrapper(repo, "status")
    assert result.returncode == 0


def test_git_wrapper_rejects_sibling_worktree(repo: Path):
    result = wrapper(repo, "worktree", "add", "../sib", "-b", "a")
    assert result.returncode == 128
    assert not (repo.parent / "sib").exists()
    assert ".agent-circus/worktrees" in result.stderr


def test_git_wrapper_allows_agent_circus_worktree(repo: Path):
    result = wrapper(repo, "worktree", "add", ".agent-circus/worktrees/x", "-b", "x")
    assert result.returncode == 0


def test_git_wrapper_allows_claude_worktree(repo: Path):
    result = wrapper(repo, "worktree", "add", ".claude/worktrees/c", "-b", "c")
    assert result.returncode == 0


def test_git_wrapper_rejects_worktrees_dir_itself(repo: Path):
    result = wrapper(repo, "worktree", "add", ".agent-circus/worktrees")
    assert result.returncode == 128
    assert ".agent-circus/worktrees" in result.stderr


def test_git_wrapper_rejects_dotdot_escape(repo: Path):
    result = wrapper(repo, "worktree", "add", ".agent-circus/worktrees/../../evil")
    assert result.returncode == 128
    assert not (repo.parent / "evil").exists()
    assert ".agent-circus/worktrees" in result.stderr


def test_git_wrapper_resolves_path_after_dash_c(tmp_path: Path, repo: Path):
    result = wrapper(
        tmp_path,
        "-C",
        "repo",
        "worktree",
        "add",
        ".agent-circus/worktrees/y",
        "-b",
        "y",
    )
    assert result.returncode == 0


def test_git_wrapper_rejects_sibling_after_dash_c(tmp_path: Path, repo: Path):
    result = wrapper(tmp_path, "-C", "repo", "worktree", "add", "../sib2")
    assert result.returncode == 128
    assert not (tmp_path / "sib2").exists()
    assert ".agent-circus/worktrees" in result.stderr


def test_git_wrapper_skips_branch_value(repo: Path):
    result = wrapper(
        repo, "worktree", "add", "-b", ".agent-circus/worktrees/b", "../sib3"
    )
    assert result.returncode == 128
    assert not (repo.parent / "sib3").exists()
    assert ".agent-circus/worktrees" in result.stderr


def test_git_wrapper_skips_lock_reason_value(repo: Path):
    result = wrapper(
        repo,
        "worktree",
        "add",
        "--lock",
        "--reason",
        "r",
        ".agent-circus/worktrees/r",
        "-b",
        "r",
    )
    assert result.returncode == 0


def _add_worktree(repo: Path, name: str) -> None:
    subprocess.run(
        [
            REAL_GIT or "git",
            "worktree",
            "add",
            f".agent-circus/worktrees/{name}",
            "-b",
            name,
        ],
        cwd=repo,
        env=git_env(),
        check=True,
        capture_output=True,
    )


def test_git_wrapper_rejects_move_outside(repo: Path):
    _add_worktree(repo, "x")
    result = wrapper(repo, "worktree", "move", ".agent-circus/worktrees/x", "../moved")
    assert result.returncode == 128
    assert not (repo.parent / "moved").exists()
    assert ".agent-circus/worktrees" in result.stderr


def test_git_wrapper_allows_move_inside(repo: Path):
    _add_worktree(repo, "x")
    result = wrapper(
        repo,
        "worktree",
        "move",
        ".agent-circus/worktrees/x",
        ".agent-circus/worktrees/x2",
    )
    assert result.returncode == 0


def test_git_wrapper_allows_add_from_linked_worktree(tmp_path: Path, repo: Path):
    _add_worktree(repo, "x")
    result = wrapper(
        repo / ".agent-circus" / "worktrees" / "x", "worktree", "add", "../w", "-b", "w"
    )
    assert result.returncode == 0


def test_install_executable_script_writes_executable_file(tmp_path: Path):
    dest = tmp_path / "file"
    result = subprocess.run(
        ["sh", "-c", _INSTALL_EXECUTABLE_SCRIPT, "sh", "echo hi\n", str(dest)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert dest.read_text() == "echo hi\n"
    assert os.access(dest, os.X_OK)


#: Stub for the real git: answers ``rev-parse`` with ``$STUB_COMMON`` and
#: writes the argv of every other call, one argument per line, to
#: ``$STUB_LOG``.
STUB_GIT = """#!/bin/bash
for a in "$@"; do
    if [ "$a" = rev-parse ]; then printf '%s\\n' "$STUB_COMMON"; exit 0; fi
done
printf '%s\\n' "$@" > "$STUB_LOG"
"""

#: First words of the note that the wrapper prints when it translates.
TRANSLATION_NOTE = 'msb-circus: running "git worktree'


def stub_wrapper(
    tmp_path: Path,
    cwd: Path,
    *args: str,
    guest_root: str,
    mirror_root: str,
    common: str,
) -> tuple[subprocess.CompletedProcess[str], list[str] | None]:
    """Run the wrapper with a stub real git.

    :returns: The result and the argv that the stub received for its
        non-``rev-parse`` call, or ``None`` if there was none.
    """
    stub = tmp_path / "stub-git"
    stub.write_text(STUB_GIT, encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "stub.log"
    log.unlink(missing_ok=True)
    script = git_wrapper_script(guest_root, mirror_root)
    result = subprocess.run(
        ["bash", "-c", script, "git", *args],
        check=False,
        cwd=cwd,
        env={
            **git_env(),
            "MSB_CIRCUS_REAL_GIT": str(stub),
            "STUB_COMMON": common,
            "STUB_LOG": str(log),
        },
        capture_output=True,
        text=True,
    )
    argv = log.read_text(encoding="utf-8").splitlines() if log.exists() else None
    return result, argv


@pytest.fixture
def mounts(tmp_path: Path) -> tuple[Path, str]:
    """Return a guest root directory (the cwd) and a mirror root path."""
    guest = tmp_path / "w"
    guest.mkdir()
    return guest, str(tmp_path / "h" / "my repo")


def test_git_wrapper_translates_add_from_guest_root(tmp_path: Path, mounts):
    guest, mirror = mounts
    result, argv = stub_wrapper(
        tmp_path,
        guest,
        "worktree",
        "add",
        ".agent-circus/worktrees/x",
        "-b",
        "x",
        guest_root=str(guest),
        mirror_root=mirror,
        common=f"{guest}/.git",
    )
    assert result.returncode == 0, result.stderr
    assert argv == [
        "-C",
        mirror,
        "worktree",
        "add",
        f"{mirror}/.agent-circus/worktrees/x",
        "-b",
        "x",
    ]
    assert TRANSLATION_NOTE in result.stderr
    assert f"through {mirror}, the host path of {guest}" in result.stderr


def test_git_wrapper_translates_claude_worktree_from_guest_root(tmp_path: Path, mounts):
    guest, mirror = mounts
    _, argv = stub_wrapper(
        tmp_path,
        guest,
        "worktree",
        "add",
        f"{guest}/.claude/worktrees/x",
        guest_root=str(guest),
        mirror_root=mirror,
        common=f"{guest}/.git",
    )
    assert argv == ["-C", mirror, "worktree", "add", f"{mirror}/.claude/worktrees/x"]


def test_git_wrapper_translates_guest_target_from_mirror(tmp_path: Path):
    guest = tmp_path / "w"
    mirror = tmp_path / "h"
    mirror.mkdir()
    result, argv = stub_wrapper(
        tmp_path,
        mirror,
        "worktree",
        "add",
        f"{guest}/.agent-circus/worktrees/x",
        "main",
        guest_root=str(guest),
        mirror_root=str(mirror),
        common=f"{mirror}/.git",
    )
    assert result.returncode == 0, result.stderr
    assert argv == [
        "-C",
        str(mirror),
        "worktree",
        "add",
        f"{mirror}/.agent-circus/worktrees/x",
        "main",
    ]


def test_git_wrapper_keeps_commit_ish_untranslated(tmp_path: Path, mounts):
    guest, mirror = mounts
    _, argv = stub_wrapper(
        tmp_path,
        guest,
        "worktree",
        "add",
        ".agent-circus/worktrees/x",
        "main",
        guest_root=str(guest),
        mirror_root=mirror,
        common=f"{guest}/.git",
    )
    assert argv is not None
    assert argv[-1] == "main"


def test_git_wrapper_rejects_sibling_from_guest_root_with_mirror(
    tmp_path: Path, mounts
):
    guest, mirror = mounts
    result, argv = stub_wrapper(
        tmp_path,
        guest,
        "worktree",
        "add",
        "../x",
        "-b",
        "x",
        guest_root=str(guest),
        mirror_root=mirror,
        common=f"{guest}/.git",
    )
    assert result.returncode == 128
    assert argv is None
    assert TRANSLATION_NOTE not in result.stderr
    assert suggestion(result) == (
        "git -C " + mirror.replace(" ", "\\ ") + " worktree add "
        ".agent-circus/worktrees/x -b x"
    )


@pytest.mark.parametrize(
    "action_args",
    [
        ["remove", ".agent-circus/worktrees/x"],
        ["remove", "--force", ".agent-circus/worktrees/x"],
        ["lock", "--reason", "a b", ".agent-circus/worktrees/x"],
        ["unlock", ".agent-circus/worktrees/x"],
    ],
)
def test_git_wrapper_translates_remove_lock_unlock(
    tmp_path: Path, mounts, action_args: list[str]
):
    guest, mirror = mounts
    result, argv = stub_wrapper(
        tmp_path,
        guest,
        "worktree",
        *action_args,
        guest_root=str(guest),
        mirror_root=mirror,
        common=f"{guest}/.git",
    )
    assert result.returncode == 0, result.stderr
    expected = [*action_args[:-1], f"{mirror}/.agent-circus/worktrees/x"]
    assert argv == ["-C", mirror, "worktree", *expected]
    assert TRANSLATION_NOTE in result.stderr


def test_git_wrapper_translates_repair_without_args(tmp_path: Path, mounts):
    guest, mirror = mounts
    _, argv = stub_wrapper(
        tmp_path,
        guest,
        "worktree",
        "repair",
        guest_root=str(guest),
        mirror_root=mirror,
        common=f"{guest}/.git",
    )
    assert argv == ["-C", mirror, "worktree", "repair"]


def test_git_wrapper_translates_repair_paths(tmp_path: Path, mounts):
    guest, mirror = mounts
    _, argv = stub_wrapper(
        tmp_path,
        guest,
        "worktree",
        "repair",
        ".agent-circus/worktrees/a",
        f"{guest}/.claude/worktrees/b",
        guest_root=str(guest),
        mirror_root=mirror,
        common=f"{guest}/.git",
    )
    assert argv == [
        "-C",
        mirror,
        "worktree",
        "repair",
        f"{mirror}/.agent-circus/worktrees/a",
        f"{mirror}/.claude/worktrees/b",
    ]


def test_git_wrapper_translates_move_inside(tmp_path: Path, mounts):
    guest, mirror = mounts
    _, argv = stub_wrapper(
        tmp_path,
        guest,
        "worktree",
        "move",
        ".agent-circus/worktrees/x",
        ".agent-circus/worktrees/y",
        guest_root=str(guest),
        mirror_root=mirror,
        common=f"{guest}/.git",
    )
    assert argv == [
        "-C",
        mirror,
        "worktree",
        "move",
        f"{mirror}/.agent-circus/worktrees/x",
        f"{mirror}/.agent-circus/worktrees/y",
    ]


def test_git_wrapper_translates_global_options_and_dash_c(tmp_path: Path, mounts):
    guest, mirror = mounts
    _, argv = stub_wrapper(
        tmp_path,
        guest.parent,
        "-c",
        "a.b=c",
        "-C",
        guest.name,
        "worktree",
        "add",
        ".agent-circus/worktrees/x",
        guest_root=str(guest),
        mirror_root=mirror,
        common=f"{guest}/.git",
    )
    assert argv == [
        "-c",
        "a.b=c",
        "-C",
        mirror,
        "worktree",
        "add",
        f"{mirror}/.agent-circus/worktrees/x",
    ]


@pytest.mark.parametrize(
    "dir_args", [["--git-dir=GUEST/.git"], ["--git-dir", "GUEST/.git"]]
)
def test_git_wrapper_skips_translation_with_git_dir(
    tmp_path: Path, mounts, dir_args: list[str]
):
    guest, mirror = mounts
    result, argv = stub_wrapper(
        tmp_path,
        guest,
        *[a.replace("GUEST", str(guest)) for a in dir_args],
        "worktree",
        "add",
        ".agent-circus/worktrees/x",
        guest_root=str(guest),
        mirror_root=mirror,
        common=f"{guest}/.git",
    )
    assert result.returncode == 128
    assert argv is None
    assert TRANSLATION_NOTE not in result.stderr


def test_git_wrapper_passes_remove_through_without_mirror(tmp_path: Path, mounts):
    guest, _ = mounts
    args = ["worktree", "remove", ".agent-circus/worktrees/x"]
    result, argv = stub_wrapper(
        tmp_path,
        guest,
        *args,
        guest_root=str(guest),
        mirror_root="",
        common=f"{guest}/.git",
    )
    assert argv == args
    assert TRANSLATION_NOTE not in result.stderr


def test_git_wrapper_passes_remove_of_other_repo_through(tmp_path: Path, mounts):
    guest, mirror = mounts
    other = tmp_path / "other"
    other.mkdir()
    args = ["worktree", "remove", f"{guest}/.agent-circus/worktrees/x"]
    result, argv = stub_wrapper(
        tmp_path,
        other,
        *args,
        guest_root=str(guest),
        mirror_root=mirror,
        common=f"{other}/.git",
    )
    assert argv == args
    assert TRANSLATION_NOTE not in result.stderr


def test_git_wrapper_creates_real_worktree_through_mirror(tmp_path: Path, repo: Path):
    guest = tmp_path / "guest"
    result = wrapper(
        repo,
        "worktree",
        "add",
        str(guest / ".agent-circus" / "worktrees" / "x"),
        "-b",
        "x",
        guest_root=str(guest),
        mirror_root=str(repo),
    )
    assert result.returncode == 0, result.stderr
    worktree = repo / ".agent-circus" / "worktrees" / "x"
    assert worktree.is_dir()
    dot_git = (worktree / ".git").read_text(encoding="utf-8")
    assert str(repo) in dot_git
    assert str(guest) not in dot_git


def test_git_wrapper_allows_mirror_worktree_with_mirror(tmp_path: Path, repo: Path):
    result = wrapper(
        repo,
        "worktree",
        "add",
        ".agent-circus/worktrees/x",
        "-b",
        "x",
        guest_root=str(tmp_path / "workspace"),
        mirror_root=str(repo),
    )
    assert result.returncode == 0


def test_git_wrapper_suggests_repo_root_without_mirror(repo: Path):
    result = wrapper(repo, "worktree", "add", "../sib", guest_root=str(repo))
    assert result.returncode == 128
    assert f"git -C {repo} worktree add .agent-circus/worktrees/" in result.stderr


def test_git_wrapper_script_quotes_roots():
    script = git_wrapper_script("/workspace", "/home/a b/it's")
    assert "MSB_CIRCUS_GUEST_ROOT-/workspace}" in script
    assert "@MIRROR_ROOT@" not in script
    result = subprocess.run(
        ["bash", "-n", "-c", script], check=False, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def suggestion(result: subprocess.CompletedProcess[str]) -> str:
    """Return the command that the wrapper suggests in its error message."""
    lines = result.stderr.splitlines()
    return lines[lines.index("Run this instead:") + 1].strip()


def test_git_wrapper_suggests_existing_branch_with_mirror(tmp_path: Path, repo: Path):
    subprocess.run(
        [REAL_GIT or "git", "branch", "develop"], cwd=repo, env=git_env(), check=True
    )
    mirror = tmp_path / "host"
    result = wrapper(
        repo,
        "worktree",
        "add",
        "../repo-develop",
        "develop",
        guest_root=str(repo),
        mirror_root=str(mirror),
    )
    assert result.returncode == 128
    assert suggestion(result) == (
        f"git -C {mirror} worktree add .agent-circus/worktrees/repo-develop develop"
    )


def test_git_wrapper_suggests_runnable_command(tmp_path: Path, repo: Path):
    result = wrapper(tmp_path, "-C", "repo", "worktree", "add", "../sib", "-b", "a")
    assert result.returncode == 128
    command = suggestion(result)
    assert command == f"git -C {repo} worktree add .agent-circus/worktrees/a -b a"
    run = subprocess.run(
        ["bash", "-c", command.replace("git", REAL_GIT or "git", 1)],
        check=False,
        env=git_env(),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert (repo / ".agent-circus" / "worktrees" / "a").is_dir()


def test_git_wrapper_suggests_last_component_without_branch(repo: Path):
    result = wrapper(repo, "worktree", "add", "../some/sib", "HEAD")
    assert suggestion(result) == (
        f"git -C {repo} worktree add .agent-circus/worktrees/sib HEAD"
    )


def test_git_wrapper_suggests_placeholder_for_worktrees_dir(repo: Path):
    result = wrapper(repo, "worktree", "add", ".agent-circus/worktrees")
    assert suggestion(result) == (
        f"git -C {repo} worktree add .agent-circus/worktrees/<name>"
    )


def test_git_wrapper_suggests_quoted_options(repo: Path):
    result = wrapper(
        repo, "-c", "a.b=c d", "worktree", "add", "--reason", "x y", "../s", "-b", "s"
    )
    assert suggestion(result) == (
        f"git -c a.b=c\\ d -C {repo} worktree add --reason x\\ y "
        ".agent-circus/worktrees/s -b s"
    )


def test_git_wrapper_suggests_mirrored_move_source(tmp_path: Path, repo: Path):
    mirror = tmp_path / "host"
    result = wrapper(
        repo,
        "worktree",
        "move",
        ".agent-circus/worktrees/x",
        "../moved",
        guest_root=str(repo),
        mirror_root=str(mirror),
    )
    assert result.returncode == 128
    assert suggestion(result) == (
        f"git -C {mirror} worktree move {mirror}/.agent-circus/worktrees/x "
        ".agent-circus/worktrees/moved"
    )
