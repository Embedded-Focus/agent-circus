"""Tests for MCP sidecar container integration."""

import json

import pytest

from agent_circus.mcp import (
    DEFAULT_MCP_PORT,
    HOST_GATEWAY_ENTRY,
    SERVICE_PREFIX,
    build_compose_override,
    requires_host_gateway,
    service_names,
)

AGENTS = ["claude-code", "codex", "mistral-vibe", "opencode"]

# -- Fixtures ---------------------------------------------------------------


@pytest.fixture()
def single_server() -> list[dict]:
    return [
        {
            "name": "filesystem",
            "image": "mcp/filesystem:latest",
            "port": 8080,
            "transport": "streamable-http",
        }
    ]


@pytest.fixture()
def multi_server() -> list[dict]:
    return [
        {
            "name": "filesystem",
            "image": "mcp/filesystem:latest",
            "port": 8080,
            "transport": "streamable-http",
            "env": {"ROOT_PATH": "/workspace"},
            "volumes": ["/host/path:/workspace:ro"],
        },
        {
            "name": "github",
            "image": "mcp/github:latest",
            "port": 9090,
            "transport": "sse",
            "env": {"GITHUB_TOKEN": "tok"},
        },
    ]


@pytest.fixture()
def defaults_only_server() -> list[dict]:
    """Server with only required fields, relying on defaults."""
    return [
        {
            "name": "minimal",
            "image": "mcp/minimal:latest",
        }
    ]


# -- Compose override -------------------------------------------------------


class TestBuildComposeOverride:
    def test_single_server(self, single_server: list[dict]) -> None:
        result = json.loads(build_compose_override(single_server, AGENTS))

        assert "services" in result
        svc = result["services"]["mcp-filesystem"]
        assert svc["image"] == "mcp/filesystem:latest"
        assert 8080 in svc["expose"]

    def test_multi_server(self, multi_server: list[dict]) -> None:
        result = json.loads(build_compose_override(multi_server, AGENTS))

        fs = result["services"]["mcp-filesystem"]
        assert fs["environment"] == {"ROOT_PATH": "/workspace"}
        assert fs["volumes"] == ["/host/path:/workspace:ro"]
        assert 8080 in fs["expose"]

        gh = result["services"]["mcp-github"]
        assert gh["image"] == "mcp/github:latest"
        assert 9090 in gh["expose"]
        assert gh["environment"] == {"GITHUB_TOKEN": "tok"}
        assert "volumes" not in gh

    def test_empty_servers(self) -> None:
        result = json.loads(build_compose_override([], AGENTS))
        assert result == {"services": {}}

    def test_external_servers_do_not_create_services(self) -> None:
        servers = [
            {
                "name": "existing",
                "url": "http://host.docker.internal:9000/mcp",
            }
        ]
        result = json.loads(build_compose_override(servers, AGENTS))
        assert result == {"services": {}}

    def test_stdio_servers_do_not_create_services(self) -> None:
        servers = [
            {
                "name": "github",
                "transport": "stdio",
                "command": "github-mcp-server",
                "args": ["stdio"],
            }
        ]
        result = json.loads(build_compose_override(servers, AGENTS))
        assert result == {"services": {}}

    def test_mixed_servers_only_create_managed_sidecars(
        self, single_server: list[dict]
    ) -> None:
        servers = [
            *single_server,
            {"name": "existing", "url": "https://mcp.example.com/mcp"},
        ]
        result = json.loads(build_compose_override(servers, AGENTS))

        assert "mcp-filesystem" in result["services"]
        assert "mcp-existing" not in result["services"]
        for agent in AGENTS:
            assert set(result["services"][agent]["depends_on"]) == {"mcp-filesystem"}

    def test_defaults_applied(self, defaults_only_server: list[dict]) -> None:
        result = json.loads(build_compose_override(defaults_only_server, AGENTS))
        svc = result["services"]["mcp-minimal"]
        assert svc["image"] == "mcp/minimal:latest"
        assert DEFAULT_MCP_PORT in svc["expose"]
        assert "environment" not in svc
        assert "volumes" not in svc

    def test_command_included_when_set(self) -> None:
        servers = [
            {
                "name": "grafana",
                "image": "grafana/mcp-grafana:0.11.0",
                "port": 8000,
                "command": ["-t", "streamable-http"],
            }
        ]
        result = json.loads(build_compose_override(servers, AGENTS))
        svc = result["services"]["mcp-grafana"]
        assert svc["command"] == ["-t", "streamable-http"]

    def test_command_omitted_when_not_set(self, single_server: list[dict]) -> None:
        result = json.loads(build_compose_override(single_server, AGENTS))
        svc = result["services"]["mcp-filesystem"]
        assert "command" not in svc

    def test_ca_certificate_install_is_opt_in(self, single_server: list[dict]) -> None:
        result = json.loads(build_compose_override(single_server, AGENTS))
        assert "post_start" not in result["services"]["mcp-filesystem"]

    def test_ca_certificate_install_hook(self) -> None:
        servers = [
            {
                "name": "internal",
                "image": "mcp/internal:latest",
                "install_ca_certs": True,
            }
        ]
        result = json.loads(build_compose_override(servers, AGENTS))
        hook = result["services"]["mcp-internal"]["post_start"][0]

        assert hook["user"] == "root"
        assert hook["command"][:2] == ["/bin/sh", "-c"]
        assert "update-ca-certificates" in hook["command"][2]

    def test_service_name_prefix(self, single_server: list[dict]) -> None:
        result = json.loads(build_compose_override(single_server, AGENTS))
        sidecar_names = [n for n in result["services"] if n not in AGENTS]
        assert all(name.startswith(SERVICE_PREFIX) for name in sidecar_names)

    def test_agents_depend_on_sidecars(self, single_server: list[dict]) -> None:
        result = json.loads(build_compose_override(single_server, AGENTS))
        for agent in AGENTS:
            deps = result["services"][agent]["depends_on"]
            assert "mcp-filesystem" in deps
            assert deps["mcp-filesystem"]["condition"] == "service_started"

    def test_agents_depend_on_all_sidecars(self, multi_server: list[dict]) -> None:
        result = json.loads(build_compose_override(multi_server, AGENTS))
        for agent in AGENTS:
            deps = result["services"][agent]["depends_on"]
            assert "mcp-filesystem" in deps
            assert "mcp-github" in deps

    def test_no_depends_on_when_no_servers(self) -> None:
        result = json.loads(build_compose_override([], AGENTS))
        for agent in AGENTS:
            assert agent not in result["services"]


def test_service_names_excludes_external_servers() -> None:
    servers = [
        {"name": "managed", "image": "mcp/managed:latest"},
        {"name": "existing", "url": "https://mcp.example.com/mcp"},
    ]
    assert service_names(servers) == ["mcp-managed"]


def test_requires_host_gateway_for_host_docker_internal() -> None:
    servers = [{"name": "existing", "url": "http://host.docker.internal:9000/mcp"}]
    assert requires_host_gateway(servers) is True
    assert HOST_GATEWAY_ENTRY == "host.docker.internal:host-gateway"


def test_requires_host_gateway_ignores_remote_urls() -> None:
    servers = [{"name": "remote", "url": "https://mcp.example.com/mcp"}]
    assert requires_host_gateway(servers) is False
