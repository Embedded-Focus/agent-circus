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


def test_git_wrapper_rejects_guest_root_worktree_with_mirror(repo: Path):
    mirror = "/home/user/my repo"
    result = wrapper(
        repo,
        "worktree",
        "add",
        ".agent-circus/worktrees/x",
        "-b",
        "x",
        guest_root=str(repo),
        mirror_root=mirror,
    )
    assert result.returncode == 128
    assert not (repo / ".agent-circus" / "worktrees" / "x").exists()
    assert f"below {mirror}/.agent-circus/worktrees/" in result.stderr
    assert f"{repo} and {mirror} are the same checkout" in result.stderr
    assert (
        "  git -C /home/user/my\\ repo worktree add .agent-circus/worktrees/x -b x\n"
        in result.stderr
    )
    assert f"below {repo}" not in result.stderr


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


def test_git_wrapper_rejects_guest_root_target_from_mirror(tmp_path: Path, repo: Path):
    guest_root = tmp_path / "workspace"
    result = wrapper(
        repo,
        "worktree",
        "add",
        str(guest_root / ".agent-circus" / "worktrees" / "x"),
        guest_root=str(guest_root),
        mirror_root=str(repo),
    )
    assert result.returncode == 128
    assert f"git -C {repo} worktree add" in result.stderr


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
