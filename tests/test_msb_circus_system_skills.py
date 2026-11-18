"""Tests for the pure system-skills helpers (store, mounts, command build)."""

import pytest

from msb_circus.exceptions import ConfigurationError
from msb_circus.system_skills import (
    INSTALLER_GUEST_AGENTS_DIR,
    SYSTEM_SKILLS_GUEST_DIR,
    build_skills_command,
    ensure_store,
    installer_mounts,
    store_skills_dir,
    system_skill_mounts,
    system_skills_store,
)


def test_system_skills_store_defaults_to_home_when_no_xdg(tmp_path) -> None:
    """Without XDG_DATA_HOME, the store lives below $HOME/.local/share."""
    store = system_skills_store(
        None, environ={"HOME": str(tmp_path), "XDG_DATA_HOME": ""}
    )
    assert store == tmp_path / ".local/share/msb-circus/system-skills"


def test_system_skills_store_uses_xdg_data_home(tmp_path) -> None:
    """A set XDG_DATA_HOME wins over $HOME."""
    store = system_skills_store(None, environ={"XDG_DATA_HOME": str(tmp_path)})
    assert store == tmp_path / "msb-circus" / "system-skills"


def test_system_skills_store_ignores_empty_xdg_data_home(tmp_path) -> None:
    """An empty XDG_DATA_HOME counts as unset."""
    store = system_skills_store(
        None, environ={"XDG_DATA_HOME": "", "HOME": str(tmp_path)}
    )
    assert store == tmp_path / ".local/share/msb-circus" / "system-skills"


def test_system_skills_store_expands_configured_tilde(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured path with ``~`` expands against $HOME."""
    monkeypatch.setenv("HOME", str(tmp_path))
    store = system_skills_store("~/skills-store", environ={"HOME": str(tmp_path)})
    assert store == tmp_path / "skills-store"


def test_system_skills_store_rejects_relative_configured_path() -> None:
    """A relative configured store path is a configuration error."""
    with pytest.raises(ConfigurationError, match="absolute"):
        system_skills_store("relative/store", environ={})


def test_ensure_store_creates_skills_dir_and_is_idempotent(tmp_path) -> None:
    """ensure_store creates store/.agents/skills and can run twice."""
    store = tmp_path / "store"
    first = ensure_store(store)
    assert first == store / ".agents" / "skills"
    assert first.is_dir()
    assert ensure_store(store) == first


def test_installer_mounts_store_agents_read_write(tmp_path) -> None:
    """The installer mounts the store's .agents read-write at /root/.agents."""
    store = tmp_path / "store"
    assert installer_mounts(store) == [
        (str(store / ".agents"), INSTALLER_GUEST_AGENTS_DIR)
    ]
    _host, guest = installer_mounts(store)[0]
    assert not guest.endswith(":ro")


def test_system_skill_mounts_target_skills_dir_read_only(tmp_path) -> None:
    """Agent sandboxes mount the store's skills dir read-only."""
    store = tmp_path / "store"
    mounts = system_skill_mounts(store)
    assert mounts == [(str(store_skills_dir(store)), f"{SYSTEM_SKILLS_GUEST_DIR}:ro")]
    assert mounts[0][1].endswith(":ro")


def test_build_skills_command_adds_g_after_subcommand() -> None:
    """``add`` gets ``-g`` inserted after the subcommand."""
    assert build_skills_command(["add", "owner/repo", "--skill", "x"]) == [
        "skills",
        "add",
        "-g",
        "owner/repo",
        "--skill",
        "x",
    ]


@pytest.mark.parametrize("flag", ["-g", "--global"])
def test_build_skills_command_keeps_existing_global_flag(flag: str) -> None:
    """No second ``-g`` when -g or --global is already given."""
    assert build_skills_command(["add", flag, "owner/repo"]) == [
        "skills",
        "add",
        flag,
        "owner/repo",
    ]


def test_build_skills_command_ls_gets_g() -> None:
    """The ``ls`` alias gets ``-g`` too."""
    assert build_skills_command(["ls"]) == ["skills", "ls", "-g"]


@pytest.mark.parametrize("subcommand", ["check", "find"])
def test_build_skills_command_plain_subcommands_have_no_g(subcommand: str) -> None:
    """``check`` and ``find`` pass through without ``-g``."""
    assert build_skills_command([subcommand]) == ["skills", subcommand]


def test_build_skills_command_help_passes_through() -> None:
    """``--help`` reaches the skills CLI unchanged."""
    assert build_skills_command(["--help"]) == ["skills", "--help"]


def test_build_skills_command_empty_args_raise() -> None:
    """No arguments is a usage error."""
    with pytest.raises(ValueError, match="Usage"):
        build_skills_command([])


def test_build_skills_command_rejects_project_subcommands() -> None:
    """Project-scoped subcommands are rejected with the allowed list."""
    with pytest.raises(ValueError, match="init"):
        build_skills_command(["init"])
    with pytest.raises(ValueError, match="add"):
        build_skills_command(["use", "x"])
