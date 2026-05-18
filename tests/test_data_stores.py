"""Tests for project-specific data store support."""

import json
from pathlib import Path

import pytest

from agent_circus.config import (
    AVAILABLE_SERVICES,
    DATA_STORE_DEFAULT_MOUNT_BASE,
    build_data_store_override,
)
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
