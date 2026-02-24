"""Tests for agent configuration templating."""

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest

from agent_circus.agent_config import (
    ClaudeCodeConfigHandler,
    CodexConfigHandler,
    VibeConfigHandler,
    _merge_named_arrays,
    build_agent_configs_override,
    build_handler,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def claude_handler(tmp_path: Path) -> ClaudeCodeConfigHandler:
    handler = ClaudeCodeConfigHandler()
    handler.host_config_path = tmp_path / ".claude.json"
    return handler


@pytest.fixture()
def codex_handler(tmp_path: Path) -> CodexConfigHandler:
    handler = CodexConfigHandler()
    handler.host_config_path = tmp_path / ".codex" / "config.toml"
    return handler


@pytest.fixture()
def vibe_handler(tmp_path: Path) -> VibeConfigHandler:
    handler = VibeConfigHandler()
    handler.host_config_path = tmp_path / ".vibe" / "config.toml"
    return handler


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def _write_toml(path: Path, data: dict) -> None:
    import tomli_w

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        tomli_w.dump(data, f)


# ---------------------------------------------------------------------------
# ClaudeCodeConfigHandler
# ---------------------------------------------------------------------------


class TestClaudeCodeRead:
    def test_read_existing_file(self, claude_handler: ClaudeCodeConfigHandler) -> None:
        _write_json(claude_handler.host_config_path, {"existing": "value"})
        result = claude_handler.read()
        assert result == {"existing": "value"}

    def test_read_missing_file(self, claude_handler: ClaudeCodeConfigHandler) -> None:
        result = claude_handler.read()
        assert result == {}


class TestClaudeCodeMerge:
    def test_merge_new_key(self, claude_handler: ClaudeCodeConfigHandler) -> None:
        base = {"existing": "value"}
        additions = {"mcpServers": {"fs": {"type": "http", "url": "http://x"}}}
        result = claude_handler.merge(base, additions)
        assert result["existing"] == "value"
        assert "fs" in result["mcpServers"]

    def test_merge_dict_values(self, claude_handler: ClaudeCodeConfigHandler) -> None:
        base = {
            "mcpServers": {
                "existing": {"type": "stdio", "command": "npx"},
            }
        }
        additions = {
            "mcpServers": {
                "new-server": {"type": "http", "url": "http://x"},
            }
        }
        result = claude_handler.merge(base, additions)
        # Both servers should be present.
        assert "existing" in result["mcpServers"]
        assert "new-server" in result["mcpServers"]

    def test_merge_dict_conflict_additions_win(
        self, claude_handler: ClaudeCodeConfigHandler
    ) -> None:
        base = {
            "mcpServers": {
                "server": {"type": "stdio", "command": "old"},
            }
        }
        additions = {
            "mcpServers": {
                "server": {"type": "http", "url": "http://new"},
            }
        }
        result = claude_handler.merge(base, additions)
        assert result["mcpServers"]["server"] == {"type": "http", "url": "http://new"}

    def test_merge_empty_additions(
        self, claude_handler: ClaudeCodeConfigHandler
    ) -> None:
        base = {"existing": "value"}
        result = claude_handler.merge(base, {})
        assert result == {"existing": "value"}

    def test_merge_empty_base(self, claude_handler: ClaudeCodeConfigHandler) -> None:
        additions = {"mcpServers": {"fs": {"type": "http", "url": "http://x"}}}
        result = claude_handler.merge({}, additions)
        assert result == additions


class TestClaudeCodeWrite:
    def test_write_creates_file(
        self, claude_handler: ClaudeCodeConfigHandler, tmp_path: Path
    ) -> None:
        output = tmp_path / "out" / "claude.json"
        claude_handler.write({"key": "value"}, output)
        assert output.is_file()
        data = json.loads(output.read_text())
        assert data == {"key": "value"}

    def test_write_produces_valid_json(
        self, claude_handler: ClaudeCodeConfigHandler, tmp_path: Path
    ) -> None:
        output = tmp_path / "claude.json"
        config = {"mcpServers": {"s": {"type": "http", "url": "http://x"}}}
        claude_handler.write(config, output)
        data = json.loads(output.read_text())
        assert data == config


class TestClaudeCodeBuild:
    def test_build_full_flow(
        self, claude_handler: ClaudeCodeConfigHandler, tmp_path: Path
    ) -> None:
        _write_json(
            claude_handler.host_config_path,
            {"existingKey": True, "mcpServers": {"old": {"type": "stdio"}}},
        )
        additions = {"mcpServers": {"new": {"type": "http", "url": "http://x"}}}
        output_dir = tmp_path / "output"
        path = build_handler(claude_handler, additions, output_dir)

        assert path.is_file()
        data = json.loads(path.read_text())
        assert data["existingKey"] is True
        assert "old" in data["mcpServers"]
        assert "new" in data["mcpServers"]

    def test_build_missing_source(
        self, claude_handler: ClaudeCodeConfigHandler, tmp_path: Path
    ) -> None:
        additions = {"mcpServers": {"new": {"type": "http", "url": "http://x"}}}
        output_dir = tmp_path / "output"
        path = build_handler(claude_handler, additions, output_dir)

        data = json.loads(path.read_text())
        assert "new" in data["mcpServers"]


# ---------------------------------------------------------------------------
# CodexConfigHandler (TOML)
# ---------------------------------------------------------------------------


class TestCodexRead:
    def test_read_existing_file(self, codex_handler: CodexConfigHandler) -> None:
        _write_toml(
            codex_handler.host_config_path,
            {"model": "o3", "mcp_servers": [{"name": "existing"}]},
        )
        result = codex_handler.read()
        assert result["model"] == "o3"
        assert len(result["mcp_servers"]) == 1

    def test_read_missing_file(self, codex_handler: CodexConfigHandler) -> None:
        assert codex_handler.read() == {}


class TestCodexMerge:
    def test_merge_named_array(self, codex_handler: CodexConfigHandler) -> None:
        base = {
            "model": "o3",
            "mcp_servers": [{"name": "existing", "url": "http://old"}],
        }
        additions = {
            "mcp_servers": [{"name": "new-server", "url": "http://new"}],
        }
        result = codex_handler.merge(base, additions)
        assert result["model"] == "o3"
        names = [s["name"] for s in result["mcp_servers"]]
        assert names == ["existing", "new-server"]

    def test_merge_named_array_conflict(
        self, codex_handler: CodexConfigHandler
    ) -> None:
        base = {
            "mcp_servers": [{"name": "server", "url": "http://old"}],
        }
        additions = {
            "mcp_servers": [{"name": "server", "url": "http://new"}],
        }
        result = codex_handler.merge(base, additions)
        assert len(result["mcp_servers"]) == 1
        assert result["mcp_servers"][0]["url"] == "http://new"

    def test_merge_preserves_order(self, codex_handler: CodexConfigHandler) -> None:
        base = {
            "mcp_servers": [
                {"name": "a", "url": "http://a"},
                {"name": "b", "url": "http://b"},
            ],
        }
        additions = {
            "mcp_servers": [
                {"name": "c", "url": "http://c"},
                {"name": "a", "url": "http://a-updated"},
            ],
        }
        result = codex_handler.merge(base, additions)
        names = [s["name"] for s in result["mcp_servers"]]
        # "a" stays in original position (updated), "b" unchanged, "c" appended.
        assert names == ["a", "b", "c"]
        assert result["mcp_servers"][0]["url"] == "http://a-updated"


class TestCodexWrite:
    def test_write_produces_valid_toml(
        self, codex_handler: CodexConfigHandler, tmp_path: Path
    ) -> None:
        output = tmp_path / "codex.toml"
        config = {"model": "o3", "mcp_servers": [{"name": "s", "url": "http://x"}]}
        codex_handler.write(config, output)
        with open(output, "rb") as f:
            data = tomllib.load(f)
        assert data == config


class TestCodexBuild:
    def test_build_full_flow(
        self, codex_handler: CodexConfigHandler, tmp_path: Path
    ) -> None:
        _write_toml(
            codex_handler.host_config_path,
            {"model": "o3", "mcp_servers": [{"name": "old", "url": "http://old"}]},
        )
        additions = {"mcp_servers": [{"name": "new", "url": "http://new"}]}
        output_dir = tmp_path / "output"
        path = build_handler(codex_handler, additions, output_dir)

        with open(path, "rb") as f:
            data = tomllib.load(f)
        assert data["model"] == "o3"
        names = [s["name"] for s in data["mcp_servers"]]
        assert "old" in names
        assert "new" in names


# ---------------------------------------------------------------------------
# VibeConfigHandler (TOML)
# ---------------------------------------------------------------------------


class TestVibeRead:
    def test_read_existing_file(self, vibe_handler: VibeConfigHandler) -> None:
        _write_toml(
            vibe_handler.host_config_path,
            {"mcp_servers": [{"name": "existing", "transport": "stdio"}]},
        )
        result = vibe_handler.read()
        assert len(result["mcp_servers"]) == 1

    def test_read_missing_file(self, vibe_handler: VibeConfigHandler) -> None:
        assert vibe_handler.read() == {}


class TestVibeBuild:
    def test_build_full_flow(
        self, vibe_handler: VibeConfigHandler, tmp_path: Path
    ) -> None:
        _write_toml(
            vibe_handler.host_config_path,
            {"mcp_servers": [{"name": "old", "transport": "stdio"}]},
        )
        additions = {
            "mcp_servers": [
                {"name": "new", "transport": "streamable-http", "url": "http://x"}
            ]
        }
        output_dir = tmp_path / "output"
        path = build_handler(vibe_handler, additions, output_dir)

        with open(path, "rb") as f:
            data = tomllib.load(f)
        names = [s["name"] for s in data["mcp_servers"]]
        assert "old" in names
        assert "new" in names


# ---------------------------------------------------------------------------
# _merge_named_arrays
# ---------------------------------------------------------------------------


class TestMergeNamedArrays:
    def test_no_overlap(self) -> None:
        base = [{"name": "a", "v": 1}]
        additions = [{"name": "b", "v": 2}]
        result = _merge_named_arrays(base, additions)
        assert result == [{"name": "a", "v": 1}, {"name": "b", "v": 2}]

    def test_overlap_additions_win(self) -> None:
        base = [{"name": "a", "v": 1}]
        additions = [{"name": "a", "v": 2}]
        result = _merge_named_arrays(base, additions)
        assert result == [{"name": "a", "v": 2}]

    def test_preserves_base_order(self) -> None:
        base = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
        additions = [{"name": "d"}, {"name": "b", "updated": True}]
        result = _merge_named_arrays(base, additions)
        names = [e["name"] for e in result]
        assert names == ["a", "b", "c", "d"]
        assert result[1]["updated"] is True

    def test_empty_base(self) -> None:
        result = _merge_named_arrays([], [{"name": "a"}])
        assert result == [{"name": "a"}]

    def test_empty_additions(self) -> None:
        result = _merge_named_arrays([{"name": "a"}], [])
        assert result == [{"name": "a"}]

    def test_both_empty(self) -> None:
        assert _merge_named_arrays([], []) == []

    def test_fallback_without_name_field(self) -> None:
        base = [{"v": 1}]
        additions = [{"v": 2}]
        result = _merge_named_arrays(base, additions)
        assert result == [{"v": 1}, {"v": 2}]


# ---------------------------------------------------------------------------
# build_agent_configs_override
# ---------------------------------------------------------------------------


class TestBuildAgentConfigsOverride:
    def test_generates_override_with_volumes(self, tmp_path: Path) -> None:
        additions = {
            "claude-code": {"mcpServers": {"s": {"type": "http", "url": "http://x"}}},
            "codex": {"mcp_servers": [{"name": "s", "url": "http://x"}]},
        }
        # Create source config files so handlers can read them.
        result = json.loads(build_agent_configs_override(additions, tmp_path))

        assert "claude-code" in result["services"]
        assert "codex" in result["services"]
        # Verify volume mounts point to correct container paths.
        cc_vols = result["services"]["claude-code"]["volumes"]
        assert any("/home/node/.claude/.claude.json" in v for v in cc_vols)
        codex_vols = result["services"]["codex"]["volumes"]
        assert any("/home/node/.codex/config.toml" in v for v in codex_vols)

    def test_skips_agents_with_empty_additions(self, tmp_path: Path) -> None:
        additions = {
            "claude-code": {"mcpServers": {"s": {"type": "http"}}},
            "codex": {},
            "mistral-vibe": {},
        }
        result = json.loads(build_agent_configs_override(additions, tmp_path))
        assert "claude-code" in result["services"]
        assert "codex" not in result["services"]
        assert "mistral-vibe" not in result["services"]

    def test_creates_output_files(self, tmp_path: Path) -> None:
        additions = {
            "claude-code": {"mcpServers": {"s": {"type": "http"}}},
            "codex": {"mcp_servers": [{"name": "s"}]},
            "mistral-vibe": {"mcp_servers": [{"name": "s"}]},
        }
        build_agent_configs_override(additions, tmp_path)
        assert (tmp_path / "claude-code.json").is_file()
        assert (tmp_path / "codex.toml").is_file()
        assert (tmp_path / "mistral-vibe.toml").is_file()


# ---------------------------------------------------------------------------
# _build_agent_config_additions (compose.py integration)
# ---------------------------------------------------------------------------


class TestBuildAgentConfigAdditions:
    def test_empty_mcp_servers(self) -> None:
        from agent_circus.config import build_agent_config_additions

        result = build_agent_config_additions({"mcp_servers": []})
        assert result == {}

    def test_no_mcp_servers_key(self) -> None:
        from agent_circus.config import build_agent_config_additions

        result = build_agent_config_additions({})
        assert result == {}

    def test_single_server(self) -> None:
        from agent_circus.config import build_agent_config_additions

        config = {
            "mcp_servers": [
                {
                    "name": "grafana",
                    "image": "grafana/mcp-grafana:0.11.0",
                    "port": 8000,
                    "transport": "streamable-http",
                }
            ]
        }
        result = build_agent_config_additions(config)

        # Claude Code format — "streamable-http" is mapped to "http"
        cc = result["claude-code"]["mcpServers"]["grafana"]
        assert cc["type"] == "http"
        assert cc["url"] == "http://mcp-grafana:8000/mcp"

        # Codex format (map keyed by server name, url only)
        codex_srv = result["codex"]["mcp_servers"]["grafana"]
        assert codex_srv["url"] == "http://mcp-grafana:8000/mcp"

        # Vibe format — keeps original transport
        vibe_srv = result["mistral-vibe"]["mcp_servers"][0]
        assert vibe_srv["name"] == "grafana"
        assert vibe_srv["transport"] == "streamable-http"

    def test_defaults(self) -> None:
        from agent_circus.config import build_agent_config_additions

        config = {"mcp_servers": [{"name": "minimal", "image": "img:latest"}]}
        result = build_agent_config_additions(config)
        # Default transport is "streamable-http", mapped to "http" for Claude Code.
        cc = result["claude-code"]["mcpServers"]["minimal"]
        assert cc["type"] == "http"
        assert cc["url"] == "http://mcp-minimal:8080/mcp"
        # Vibe keeps the original default transport.
        vibe_srv = result["mistral-vibe"]["mcp_servers"][0]
        assert vibe_srv["transport"] == "streamable-http"
