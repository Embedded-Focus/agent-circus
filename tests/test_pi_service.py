"""Tests for the ``pi`` agent service integration.

Pi runs natively inside the container and is driven over ACP by
``pi-acp``, which spawns ``pi --mode rpc`` as a child process. These tests
cover the template wiring, the config-directory and credential mounts, the
restricted seeding rule that keeps host Pi packages and sessions out of the
container, and the settings merge path.
"""

import json
import re
from pathlib import Path
from typing import Any

from agent_circus import dependencies
from agent_circus.agent_config import (
    HANDLERS,
    PiConfigHandler,
    merge_agent_config_store,
)
from agent_circus.config import (
    AGENT_CONFIG_SEED_PATHS,
    AGENT_CREDENTIAL_PATHS,
    AVAILABLE_SERVICES,
    DEFAULT_AGENT_CONFIG_MOUNTS,
    build_agent_config_mounts_override,
)
from agent_circus.context import _seed_data_stores
from agent_circus.templates import template_dir_context

PI_SERVICE = "pi"


def _load_template_files() -> dict[str, str]:
    """Read the shipped agent template files as text.

    :returns: File name mapped to file content.
    """
    with template_dir_context() as template_dir:
        return {
            name: (template_dir / name).read_text()
            for name in ("Dockerfile", "compose.yaml")
        }


def test_pi_is_available_service() -> None:
    assert PI_SERVICE in AVAILABLE_SERVICES


def test_pi_config_mount_covers_pi_acp_state() -> None:
    # The adapter writes ~/.pi/pi-acp/session-map.json, so the project store
    # must be mounted one level above ~/.pi/agent to persist both.
    mount = DEFAULT_AGENT_CONFIG_MOUNTS[PI_SERVICE]
    assert mount["container"] == "/home/node/.pi"
    assert mount["host"] == "${HOME}/.pi"


def test_pi_credential_path_points_at_auth_file() -> None:
    paths = AGENT_CREDENTIAL_PATHS[PI_SERVICE]
    assert paths["host"] == "${HOME}/.pi/agent/auth.json"
    assert paths["container"] == "/home/node/.pi/agent/auth.json"


def test_agent_config_mounts_override_mounts_pi_store(tmp_path: Path) -> None:
    store_dirs = {service: tmp_path / service for service in AVAILABLE_SERVICES}
    override = json.loads(build_agent_config_mounts_override(store_dirs))
    volumes = override["services"][PI_SERVICE]["volumes"]
    assert f"{store_dirs[PI_SERVICE]}:/home/node/.pi:cached" in volumes


def test_compose_template_declares_pi_service() -> None:
    compose = _load_template_files()["compose.yaml"]
    tail = compose[compose.rindex(f"  {PI_SERVICE}:") :]
    assert f"target: {PI_SERVICE}" in tail
    assert "AGENT_TYPE: pi" in tail
    assert re.search(r"PI_VERSION:\s*\S+", tail)
    assert re.search(r"PI_ACP_VERSION:\s*\S+", tail)
    assert "${AGENT_CIRCUS_WORKSPACE:-..}:/workspace:cached" in tail


def test_dockerfile_pi_target_installs_agent_and_adapter() -> None:
    dockerfile = _load_template_files()["Dockerfile"]
    target = dockerfile.split("FROM base AS pi\n", 1)[1]
    assert "@earendil-works/pi-coding-agent@${PI_VERSION}" in target
    assert "pi-acp@${PI_ACP_VERSION}" in target


def test_pi_version_pins_are_discoverable() -> None:
    with template_dir_context() as template_dir:
        pinned = dependencies.versions(template_dir)
    assert pinned["pi"]
    assert pinned["pi-acp"]


def test_seed_paths_only_cover_settings() -> None:
    assert AGENT_CONFIG_SEED_PATHS[PI_SERVICE] == [
        "agent/settings.json",
        "agent/models.json",
    ]


def test_seeding_pi_store_skips_packages_sessions_and_releases(
    tmp_path: Path,
) -> None:
    """A host ``~/.pi`` seeds settings and models only.

    ``agent/npm`` and ``agent/install`` hold code that would run inside the
    container, and ``agent/sessions`` holds host transcripts, so none of them
    may reach the project store.
    """
    host = tmp_path / "host-pi"
    _write_pi_tree(host)
    store = tmp_path / "stores"
    store.mkdir()

    _seed_data_stores(
        [
            {
                "name": PI_SERVICE,
                "seed_from": str(host),
                "seed_paths": AGENT_CONFIG_SEED_PATHS[PI_SERVICE],
            }
        ],
        store,
    )

    seeded = store / PI_SERVICE
    assert (
        seeded / "agent/settings.json"
    ).read_text() == '{"defaultModel": "host/demo"}'
    assert (seeded / "agent/models.json").is_file()
    assert not (seeded / "agent/npm").exists()
    assert not (seeded / "agent/install").exists()
    assert not (seeded / "agent/sessions").exists()


def test_seeding_pi_store_tolerates_missing_optional_files(tmp_path: Path) -> None:
    """A host without ``models.json`` still seeds successfully."""
    host = tmp_path / "host-pi"
    (host / "agent").mkdir(parents=True)
    (host / "agent/settings.json").write_text('{"defaultModel": "x"}')
    store = tmp_path / "stores"
    store.mkdir()

    _seed_data_stores(
        [
            {
                "name": PI_SERVICE,
                "seed_from": str(host),
                "seed_paths": AGENT_CONFIG_SEED_PATHS[PI_SERVICE],
            }
        ],
        store,
    )

    assert not (store / PI_SERVICE / "agent/models.json").exists()
    assert (store / PI_SERVICE / "agent/settings.json").is_file()


def test_pi_config_handler_is_registered() -> None:
    assert PiConfigHandler in HANDLERS
    assert PiConfigHandler.agent_name == PI_SERVICE


def test_pi_config_handler_merges_without_dropping_keys() -> None:
    handler = PiConfigHandler()
    handler.host_config_path = Path("/nonexistent/settings.json")

    merged = handler.merge(
        {"defaultProvider": "x", "defaultModel": "y", "thinkingLevel": "high"},
        {"mcpServers": {"docs": {"url": "http://mcp-docs:8080/mcp"}}},
    )

    assert merged["defaultProvider"] == "x"
    assert merged["defaultModel"] == "y"
    assert merged["thinkingLevel"] == "high"
    assert merged["mcpServers"] == {"docs": {"url": "http://mcp-docs:8080/mcp"}}


def test_merge_agent_config_store_writes_under_agent_subdir(
    tmp_path: Path,
) -> None:
    """The store layout mirrors ``~/.pi`` so nested config lands in ``agent/``."""
    store = tmp_path / "store"
    (store / "agent").mkdir(parents=True)
    (store / "agent/settings.json").write_text(
        json.dumps({"defaultModel": "local/demo-model"})
    )

    written = merge_agent_config_store(PI_SERVICE, {"thinkingLevel": "medium"}, store)

    assert written == store / "agent/settings.json"
    settings = json.loads(written.read_text())
    assert settings == {"defaultModel": "local/demo-model", "thinkingLevel": "medium"}
    # Nothing may land in the store root: that is not a location Pi reads.
    assert not (store / "settings.json").exists()


def _write_pi_tree(root: Path) -> None:
    """Materialize a representative host ``~/.pi`` directory.

    :param root: Directory to populate.
    """
    agent = root / "agent"
    for name in ("npm/packages", "install/releases", "sessions/dummy"):
        (agent / name).mkdir(parents=True)
    (agent / "settings.json").write_text('{"defaultModel": "host/demo"}')
    (agent / "models.json").write_text('{"models": []}')
    (agent / "npm/packages/extension.js").write_text("console.log('host code')")
    (agent / "install/releases/pi.js").write_text("console.log('host release')")
    (agent / "sessions/dummy/session.jsonl").write_text("{}\n")


def test_pi_service_list_has_no_duplicate_handler(tmp_path: Path) -> None:
    handlers: dict[str, Any] = {}
    for handler_cls in HANDLERS:
        handler = handler_cls()
        assert handler.agent_name not in handlers
        handlers[handler.agent_name] = handler
    assert handlers[PI_SERVICE].container_config_path == (
        "/home/node/.pi/agent/settings.json"
    )
