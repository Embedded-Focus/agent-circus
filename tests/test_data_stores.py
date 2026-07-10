"""Tests for project-specific data store support."""

import json
from pathlib import Path

import pytest

from agent_circus.config import (
    AVAILABLE_SERVICES,
    DATA_STORE_DEFAULT_MOUNT_BASE,
    build_agent_config_mounts_override,
    build_data_store_override,
    build_podman_runtime_override,
    get_agent_config_data_stores,
    get_claimed_agent_config_mounts,
)
from agent_circus.context import _DATA_STORE_SEED_MARKER, _seed_data_stores
from agent_circus.exceptions import ConfigurationError
from agent_circus.state import get_data_store_dir, get_data_store_override_path

# ---------------------------------------------------------------------------
# builder
# ---------------------------------------------------------------------------


def test_build_data_store_override_default_mount_path(tmp_path: Path) -> None:
    data_base_dir = tmp_path / "data"
    stores = [{"name": "memory"}]
    result = json.loads(build_data_store_override(stores, data_base_dir))
    expected_volume = (
        f"{data_base_dir / 'memory'}:{DATA_STORE_DEFAULT_MOUNT_BASE}/memory:cached"
    )
    for svc in AVAILABLE_SERVICES:
        assert expected_volume in result["services"][svc]["volumes"]


def test_build_data_store_override_custom_mount_path(tmp_path: Path) -> None:
    data_base_dir = tmp_path / "data"
    stores = [{"name": "bashhistory", "mount_path": "/commandhistory"}]
    result = json.loads(build_data_store_override(stores, data_base_dir))
    expected_volume = f"{data_base_dir / 'bashhistory'}:/commandhistory:cached"
    for svc in AVAILABLE_SERVICES:
        assert expected_volume in result["services"][svc]["volumes"]


def test_build_data_store_override_keeps_portable_mount_options_for_podman(
    tmp_path: Path,
) -> None:
    data_base_dir = tmp_path / "data"
    stores = [{"name": "memory"}]
    result = json.loads(
        build_data_store_override(stores, data_base_dir, runtime="podman")
    )
    expected_volume = (
        f"{data_base_dir / 'memory'}:{DATA_STORE_DEFAULT_MOUNT_BASE}/memory:cached"
    )
    for svc in AVAILABLE_SERVICES:
        assert expected_volume in result["services"][svc]["volumes"]


def test_build_data_store_override_multiple_stores(tmp_path: Path) -> None:
    data_base_dir = tmp_path / "data"
    stores = [
        {"name": "bashhistory", "mount_path": "/commandhistory"},
        {"name": "memory"},
    ]
    result = json.loads(build_data_store_override(stores, data_base_dir))
    volumes = result["services"]["claude-code"]["volumes"]
    assert f"{data_base_dir / 'bashhistory'}:/commandhistory:cached" in volumes
    assert (
        f"{data_base_dir / 'memory'}:{DATA_STORE_DEFAULT_MOUNT_BASE}/memory:cached"
        in volumes
    )


def test_build_data_store_override_all_services(tmp_path: Path) -> None:
    stores = [{"name": "index"}]
    result = json.loads(build_data_store_override(stores, tmp_path))
    assert set(result["services"].keys()) == set(AVAILABLE_SERVICES)


def test_build_data_store_override_empty(tmp_path: Path) -> None:
    result = json.loads(build_data_store_override([], tmp_path))
    for svc in AVAILABLE_SERVICES:
        assert result["services"][svc]["volumes"] == []


def test_build_data_store_override_scopes_to_services(tmp_path: Path) -> None:
    stores = [
        {
            "name": "codex-config",
            "mount_path": "/home/node/.codex",
            "services": ["codex"],
        }
    ]
    result = json.loads(build_data_store_override(stores, tmp_path))

    assert (
        f"{tmp_path / 'codex-config'}:/home/node/.codex:cached"
        in result["services"]["codex"]["volumes"]
    )
    for svc in set(AVAILABLE_SERVICES) - {"codex"}:
        assert result["services"][svc]["volumes"] == []


def test_build_data_store_override_excludes_seed_mounts(
    tmp_path: Path,
) -> None:
    stores = [
        {
            "name": "codex-config",
            "mount_path": "/home/node/.codex",
            "seed_from": "${HOME}/.codex",
            "services": ["codex"],
        }
    ]

    result = json.loads(build_data_store_override(stores, tmp_path))
    volumes = result["services"]["codex"]["volumes"]

    assert volumes == [f"{tmp_path / 'codex-config'}:/home/node/.codex:cached"]


def test_build_data_store_override_rejects_invalid_service(tmp_path: Path) -> None:
    stores = [{"name": "bad", "services": ["missing"]}]

    with pytest.raises(ConfigurationError):
        build_data_store_override(stores, tmp_path)


def test_build_data_store_override_rejects_invalid_seed_mode(tmp_path: Path) -> None:
    stores = [{"name": "bad", "seed_from": "/tmp/source", "seed_mode": "always"}]

    with pytest.raises(ConfigurationError):
        build_data_store_override(stores, tmp_path)


def test_build_data_store_override_rejects_tilde_seed_from(tmp_path: Path) -> None:
    stores = [{"name": "bad", "seed_from": "~/.codex"}]

    with pytest.raises(ConfigurationError, match="~"):
        build_data_store_override(stores, tmp_path)


def test_build_agent_config_mounts_override_includes_writable_stores(
    tmp_path: Path,
) -> None:
    store_dirs = {service: tmp_path / service for service in AVAILABLE_SERVICES}
    result = json.loads(build_agent_config_mounts_override(store_dirs))

    assert (
        f"{tmp_path / 'claude-code'}:/home/node/.claude:cached"
        in result["services"]["claude-code"]["volumes"]
    )
    assert (
        f"{tmp_path / 'codex'}:/home/node/.codex:cached"
        in result["services"]["codex"]["volumes"]
    )


def test_build_agent_config_mounts_override_omits_claimed_mount(
    tmp_path: Path,
) -> None:
    claimed = {("codex", "/home/node/.codex")}
    store_dirs = {service: tmp_path / service for service in AVAILABLE_SERVICES}
    result = json.loads(build_agent_config_mounts_override(store_dirs, claimed))

    assert result["services"]["codex"]["volumes"] == []
    assert (
        f"{tmp_path / 'claude-code'}:/home/node/.claude:cached"
        in result["services"]["claude-code"]["volumes"]
    )


def test_build_agent_config_mounts_override_keeps_portable_mount_options_for_podman(
    tmp_path: Path,
) -> None:
    store_dirs = {service: tmp_path / service for service in AVAILABLE_SERVICES}
    result = json.loads(
        build_agent_config_mounts_override(store_dirs, runtime="podman")
    )

    assert (
        f"{tmp_path / 'claude-code'}:/home/node/.claude:cached"
        in result["services"]["claude-code"]["volumes"]
    )
    assert (
        f"{tmp_path / 'codex'}:/home/node/.codex:cached"
        in result["services"]["codex"]["volumes"]
    )


def test_build_podman_runtime_override_uses_keep_id_user_namespace() -> None:
    result = json.loads(build_podman_runtime_override())

    assert set(result["services"]) == set(AVAILABLE_SERVICES)
    for service in AVAILABLE_SERVICES:
        assert result["services"][service]["userns_mode"] == "keep-id"


def test_get_claimed_agent_config_mounts() -> None:
    stores = [
        {
            "name": "codex-config",
            "mount_path": "/home/node/.codex",
            "services": ["codex"],
        },
        {
            "name": "other",
            "mount_path": "/tmp/other",
            "services": ["claude-code"],
        },
    ]

    assert get_claimed_agent_config_mounts(stores) == {("codex", "/home/node/.codex")}


def test_get_agent_config_data_stores() -> None:
    stores = [
        {
            "name": "codex-config",
            "mount_path": "/home/node/.codex",
            "services": ["codex"],
        },
        {"name": "memory"},
    ]

    assert get_agent_config_data_stores(stores) == {"codex": "codex-config"}


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------


def test_seed_data_stores_copies_seed_contents_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_root = tmp_path / "seed-home"
    seed_dir = seed_root / ".codex"
    seed_dir.mkdir(parents=True)
    (seed_dir / "config.toml").write_text("model = 'gpt-5'\n")
    (seed_dir / "nested").mkdir()
    (seed_dir / "nested" / "state.json").write_text("{}\n")
    data_base_dir = tmp_path / "data"
    monkeypatch.setenv("SEED_HOME", str(seed_root))
    stores = [{"name": "codex-config", "seed_from": "${SEED_HOME}/.codex"}]

    _seed_data_stores(stores, data_base_dir)

    target = data_base_dir / "codex-config"
    assert (target / "config.toml").read_text() == "model = 'gpt-5'\n"
    assert (target / "nested" / "state.json").read_text() == "{}\n"
    assert (target / _DATA_STORE_SEED_MARKER).exists()

    (seed_dir / "config.toml").write_text("model = 'changed'\n")
    (target / "config.toml").write_text("model = 'local'\n")

    _seed_data_stores(stores, data_base_dir)

    assert (target / "config.toml").read_text() == "model = 'local'\n"


def test_seed_data_stores_rejects_unresolved_relative_source(tmp_path: Path) -> None:
    stores = [{"name": "bad", "seed_from": "${MISSING_HOME}/.codex"}]

    with pytest.raises(ConfigurationError, match="absolute"):
        _seed_data_stores(stores, tmp_path / "data")


def test_seed_data_stores_marks_missing_source_as_seeded(tmp_path: Path) -> None:
    data_base_dir = tmp_path / "data"
    stores = [{"name": "missing", "seed_from": str(tmp_path / "does-not-exist")}]

    _seed_data_stores(stores, data_base_dir)

    target = data_base_dir / "missing"
    assert (target / _DATA_STORE_SEED_MARKER).exists()
    assert [path.name for path in target.iterdir()] == [_DATA_STORE_SEED_MARKER]


# ---------------------------------------------------------------------------
# state helpers
# ---------------------------------------------------------------------------


def test_get_data_store_dir_creates_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    workspace = tmp_path / "my-project"
    store_dir = get_data_store_dir(workspace, "memory")
    assert (
        store_dir
        == tmp_path / "state" / "agent-circus" / "my-project" / "data" / "memory"
    )
    assert store_dir.is_dir()


def test_get_data_store_dir_parent_is_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    workspace = tmp_path / "my-project"
    dir_a = get_data_store_dir(workspace, "alpha")
    dir_b = get_data_store_dir(workspace, "beta")
    assert dir_a.parent == dir_b.parent


def test_get_data_store_override_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    path = get_data_store_override_path(tmp_path / "my-project")
    assert path.name == "compose.data-store.json"
    assert path.parent == tmp_path / "state" / "agent-circus" / "my-project"
