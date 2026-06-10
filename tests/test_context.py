"""Tests for compose context assembly."""

import json
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_circus.context import (
    _ENV_INJECTION_ANCHOR,
    _ENV_PROFILE_D_PATH,
    _ENV_PROFILE_SCRIPT_NAME,
    _copy_project_hooks,
    _inject_env_into_dockerfile,
    build_compose_context,
)
from agent_circus.state import get_agent_config_store_dir, get_data_store_dir


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={"shadow": [".env"], "mcp_servers": []},
)
@patch("agent_circus.context.resolve_config", return_value=None)
def test_context_includes_shadow_override(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    with build_compose_context(tmp_path) as ctx:
        assert ctx.shadow_override is not None
        assert ".env" in ctx.shadow_override


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={"shadow": [], "mcp_servers": []},
)
@patch("agent_circus.context.resolve_config", return_value=None)
def test_context_no_shadow_when_empty(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    with build_compose_context(tmp_path) as ctx:
        assert ctx.shadow_override is None


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={
        "shadow": [],
        "mcp_servers": [
            {
                "name": "existing",
                "url": "http://host.docker.internal:9000/mcp",
            }
        ],
    },
)
@patch("agent_circus.context.resolve_config", return_value=None)
def test_context_external_host_mcp_adds_host_gateway_without_sidecar(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    with build_compose_context(tmp_path) as ctx:
        hosts = json.loads(ctx.hosts_override or "{}")
        assert hosts["services"]["codex"]["extra_hosts"] == [
            "host.docker.internal:host-gateway"
        ]
        assert ctx.mcp_override is None
        assert ctx.companion_services == ()
    mock_mcp.assert_not_called()


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={
        "shadow": [],
        "mcp_servers": [],
        "port_forwards": [{"service": "codex", "container_port": 3333}],
    },
)
@patch("agent_circus.context.resolve_config", return_value=None)
def test_context_includes_port_forwards_override(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    with build_compose_context(tmp_path) as ctx:
        assert ctx.port_forwards_override is not None
        assert "127.0.0.1:3333:3333/tcp" in ctx.port_forwards_override


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={"shadow": [], "mcp_servers": []},
)
@patch("agent_circus.context.resolve_config")
def test_context_deploy_mode(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    compose_file = config_dir / "compose.yaml"
    compose_file.touch()
    mock_resolve.return_value = config_dir

    with build_compose_context(tmp_path) as ctx:
        assert ctx.compose_file == compose_file
        assert ctx.cwd == config_dir
        assert ctx.env is None


# ---------------------------------------------------------------------------
# Hook script copying (instant mode)
# ---------------------------------------------------------------------------


def _make_template_dir(base: Path) -> Path:
    """Create a minimal fake template directory with placeholder hook scripts and Dockerfile."""
    template_dir = base / "template"
    template_dir.mkdir()
    hooks_dir = template_dir / "hooks"
    hooks_dir.mkdir()
    (hooks_dir / "base-root.sh").write_text("")
    (hooks_dir / "base-user.sh").write_text("")
    (template_dir / "Dockerfile").write_text(
        f"FROM scratch{_ENV_INJECTION_ANCHOR} []\n"
    )
    return template_dir


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={"shadow": [], "mcp_servers": []},
)
@patch("agent_circus.context.resolve_config", return_value=None)
@patch("agent_circus.context.template_dir_context")
def test_context_instant_mode_copies_root_hook(
    mock_tdc: MagicMock,
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    template_dir = _make_template_dir(tmp_path)
    mock_tdc.return_value.__enter__ = MagicMock(return_value=template_dir)
    mock_tdc.return_value.__exit__ = MagicMock(return_value=False)

    hook_content = "apt-get install -y tree\n"
    hooks_src = workspace / ".agent-circus" / "hooks"
    hooks_src.mkdir(parents=True)
    (hooks_src / "base-root.sh").write_text(hook_content)

    with build_compose_context(workspace) as ctx:
        # Check inside the with-block so the temp build context is still alive.
        assert (ctx.cwd / "hooks" / "base-root.sh").read_text() == hook_content


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={"shadow": [], "mcp_servers": []},
)
@patch("agent_circus.context.resolve_config", return_value=None)
@patch("agent_circus.context.template_dir_context")
def test_context_instant_mode_copies_user_hook(
    mock_tdc: MagicMock,
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    template_dir = _make_template_dir(tmp_path)
    mock_tdc.return_value.__enter__ = MagicMock(return_value=template_dir)
    mock_tdc.return_value.__exit__ = MagicMock(return_value=False)

    hook_content = "npm install -g @anthropic-ai/sdk\n"
    hooks_src = workspace / ".agent-circus" / "hooks"
    hooks_src.mkdir(parents=True)
    (hooks_src / "base-user.sh").write_text(hook_content)

    with build_compose_context(workspace) as ctx:
        assert (ctx.cwd / "hooks" / "base-user.sh").read_text() == hook_content


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={"shadow": [], "mcp_servers": []},
)
@patch("agent_circus.context.resolve_config", return_value=None)
@patch("agent_circus.context.template_dir_context")
def test_context_instant_mode_missing_hooks_dir_is_noop(
    mock_tdc: MagicMock,
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    template_dir = _make_template_dir(tmp_path)
    mock_tdc.return_value.__enter__ = MagicMock(return_value=template_dir)
    mock_tdc.return_value.__exit__ = MagicMock(return_value=False)

    # No .agent-circus/hooks/ directory exists in the workspace.
    with build_compose_context(workspace) as ctx:
        # Placeholder scripts in the build context remain empty.
        assert (ctx.cwd / "hooks" / "base-root.sh").read_text() == ""
        assert (ctx.cwd / "hooks" / "base-user.sh").read_text() == ""


# ---------------------------------------------------------------------------
# _copy_project_hooks unit tests
# ---------------------------------------------------------------------------


def test_copy_project_hooks_copies_existing_scripts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    build_context = tmp_path / "build"
    build_context.mkdir()
    (build_context / "hooks").mkdir()

    hooks_src = workspace / ".agent-circus" / "hooks"
    hooks_src.mkdir(parents=True)
    (hooks_src / "base-root.sh").write_text("apt-get install -y tree\n")

    _copy_project_hooks(workspace, build_context)

    assert (
        build_context / "hooks" / "base-root.sh"
    ).read_text() == "apt-get install -y tree\n"
    # base-user.sh was not provided (should not be created)
    assert not (build_context / "hooks" / "base-user.sh").exists()


def test_copy_project_hooks_no_hooks_dir(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    build_context = tmp_path / "build"
    build_context.mkdir()
    (build_context / "hooks").mkdir()

    # No .agent-circus/hooks/ (should be a no-op with no error).
    _copy_project_hooks(workspace, build_context)

    assert list((build_context / "hooks").iterdir()) == []


# ---------------------------------------------------------------------------
# _inject_env_into_dockerfile unit tests
# ---------------------------------------------------------------------------


def test_inject_env_inserts_before_entrypoint(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM scratch{_ENV_INJECTION_ANCHOR} []\n")

    _inject_env_into_dockerfile(tmp_path, {"GOPATH": "/home/node/go"})

    content = dockerfile.read_text()
    assert 'ENV GOPATH="/home/node/go"' in content
    # ENV must appear before ENTRYPOINT in the file
    assert content.index("ENV GOPATH") < content.index("ENTRYPOINT")


def test_inject_env_multiple_vars(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM scratch{_ENV_INJECTION_ANCHOR} []\n")

    _inject_env_into_dockerfile(
        tmp_path, {"GOPATH": "/home/node/go", "PATH": "/usr/local/go/bin:$PATH"}
    )

    content = dockerfile.read_text()
    assert 'ENV GOPATH="/home/node/go"' in content
    assert 'ENV PATH="/usr/local/go/bin:$PATH"' in content


def test_inject_env_value_with_spaces_is_quoted(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM scratch{_ENV_INJECTION_ANCHOR} []\n")

    _inject_env_into_dockerfile(
        tmp_path,
        {"LG_BUILDINFO_NAME": "firmware :: ybp :: yocto-based-platform :: main"},
    )

    content = dockerfile.read_text()
    assert (
        'ENV LG_BUILDINFO_NAME="firmware :: ybp :: yocto-based-platform :: main"'
        in content
    )


def test_inject_env_empty_leaves_dockerfile_unchanged(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    original = f"FROM scratch{_ENV_INJECTION_ANCHOR} []\n"
    dockerfile.write_text(original)

    _inject_env_into_dockerfile(tmp_path, {})

    assert dockerfile.read_text() == original


def test_inject_env_writes_profile_d_script(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM scratch{_ENV_INJECTION_ANCHOR} []\n")

    _inject_env_into_dockerfile(tmp_path, {"PATH": "${PATH}:/home/node/go/bin"})

    profile_src = tmp_path / "hooks" / _ENV_PROFILE_SCRIPT_NAME
    assert profile_src.is_file()
    content = profile_src.read_text()
    assert "#!/bin/sh" in content
    assert 'export PATH="${PATH}:/home/node/go/bin"' in content


def test_inject_env_dockerfile_contains_profile_d_copy(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM scratch{_ENV_INJECTION_ANCHOR} []\n")

    _inject_env_into_dockerfile(tmp_path, {"PATH": "${PATH}:/home/node/go/bin"})

    content = dockerfile.read_text()
    assert f"COPY hooks/{_ENV_PROFILE_SCRIPT_NAME} {_ENV_PROFILE_D_PATH}" in content
    assert f"RUN chmod 644 {_ENV_PROFILE_D_PATH}" in content
    # profile.d install must appear before ENTRYPOINT
    assert content.index(f"COPY hooks/{_ENV_PROFILE_SCRIPT_NAME}") < content.index(
        "ENTRYPOINT"
    )


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={
        "shadow": [],
        "mcp_servers": [],
        "env": {"PATH": "/usr/local/go/bin:$PATH"},
    },
)
@patch("agent_circus.context.resolve_config", return_value=None)
@patch("agent_circus.context.template_dir_context")
def test_context_instant_mode_injects_env(
    mock_tdc: MagicMock,
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    template_dir = _make_template_dir(tmp_path)
    mock_tdc.return_value.__enter__ = MagicMock(return_value=template_dir)
    mock_tdc.return_value.__exit__ = MagicMock(return_value=False)

    with build_compose_context(workspace) as ctx:
        content = (ctx.cwd / "Dockerfile").read_text()
        assert 'ENV PATH="/usr/local/go/bin:$PATH"' in content
        assert content.index("ENV PATH") < content.index("ENTRYPOINT")


def test_context_merges_mcp_into_codex_config_data_store(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    host_codex = tmp_path / "host-codex"
    host_codex.mkdir()
    (host_codex / "config.toml").write_text('sandbox_mode = "workspace-write"\n')
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = {
        "mcp_servers": [
            {
                "name": "github",
                "transport": "stdio",
                "command": "github-mcp-server",
                "args": ["stdio"],
            }
        ],
        "data_stores": [
            {
                "name": "codex-config",
                "mount_path": "/home/node/.codex",
                "seed_from": str(host_codex),
                "services": ["codex"],
            }
        ],
    }

    with (
        patch("agent_circus.context.load_config", return_value=config),
        patch("agent_circus.context.resolve_config", return_value=config_dir),
        build_compose_context(workspace) as ctx,
    ):
        assert ctx.agent_configs_override is None
        assert ctx.data_store_seeder is not None
        ctx.data_store_seeder()

    config_path = get_data_store_dir(workspace, "codex-config") / "config.toml"
    with open(config_path, "rb") as f:
        codex_config = tomllib.load(f)
    assert codex_config["sandbox_mode"] == "workspace-write"
    assert codex_config["mcp_servers"]["github"] == {
        "command": "github-mcp-server",
        "args": ["stdio"],
    }


def test_context_uses_automatic_writable_codex_store_for_mcp(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    host_home = tmp_path / "home"
    host_codex = host_home / ".codex"
    host_codex.mkdir(parents=True)
    (host_codex / "config.toml").write_text('sandbox_mode = "workspace-write"\n')
    monkeypatch.setenv("HOME", str(host_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config = {
        "mcp_servers": [
            {
                "name": "github",
                "transport": "stdio",
                "command": "github-mcp-server",
            }
        ]
    }

    with (
        patch("agent_circus.context.load_config", return_value=config),
        patch("agent_circus.context.resolve_config", return_value=config_dir),
        build_compose_context(workspace) as ctx,
    ):
        override = json.loads(ctx.agent_config_mounts_override or "{}")
        volumes = override["services"]["codex"]["volumes"]
        assert volumes == [
            f"{get_agent_config_store_dir(workspace, 'codex')}:/home/node/.codex:cached"
        ]
        assert all("/home/node/.codex/config.toml" not in volume for volume in volumes)
        assert ctx.agent_configs_override is None
        assert ctx.data_store_seeder is not None
        ctx.data_store_seeder()

    config_path = get_agent_config_store_dir(workspace, "codex") / "config.toml"
    with open(config_path, "rb") as f:
        codex_config = tomllib.load(f)
    assert codex_config["sandbox_mode"] == "workspace-write"
    assert codex_config["mcp_servers"]["github"] == {"command": "github-mcp-server"}
