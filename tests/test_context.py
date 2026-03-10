"""Tests for compose context assembly."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_circus.context import (
    _ENV_INJECTION_ANCHOR,
    _copy_project_hooks,
    _inject_env_into_dockerfile,
    build_compose_context,
)


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch("agent_circus.context.build_agent_configs_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={"shadow": [".env"], "mcp_servers": []},
)
@patch("agent_circus.context.resolve_config", return_value=None)
def test_context_includes_shadow_override(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_agent_configs: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    with build_compose_context(tmp_path) as ctx:
        assert ctx.shadow_override is not None
        assert ".env" in ctx.shadow_override


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch("agent_circus.context.build_agent_configs_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={"shadow": [], "mcp_servers": []},
)
@patch("agent_circus.context.resolve_config", return_value=None)
def test_context_no_shadow_when_empty(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_agent_configs: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    with build_compose_context(tmp_path) as ctx:
        assert ctx.shadow_override is None


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch("agent_circus.context.build_agent_configs_override", return_value="{}")
@patch(
    "agent_circus.context.load_config",
    return_value={"shadow": [], "mcp_servers": []},
)
@patch("agent_circus.context.resolve_config")
def test_context_deploy_mode(
    mock_resolve: MagicMock,
    mock_load: MagicMock,
    mock_agent_configs: MagicMock,
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
@patch("agent_circus.context.build_agent_configs_override", return_value="{}")
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
    mock_agent_configs: MagicMock,
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

    with build_compose_context(workspace):
        pass

    assert (template_dir / "hooks" / "base-root.sh").read_text() == hook_content


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch("agent_circus.context.build_agent_configs_override", return_value="{}")
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
    mock_agent_configs: MagicMock,
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

    with build_compose_context(workspace):
        pass

    assert (template_dir / "hooks" / "base-user.sh").read_text() == hook_content


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch("agent_circus.context.build_agent_configs_override", return_value="{}")
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
    mock_agent_configs: MagicMock,
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
        assert ctx is not None

    # Placeholder scripts remain unchanged (empty).
    assert (template_dir / "hooks" / "base-root.sh").read_text() == ""
    assert (template_dir / "hooks" / "base-user.sh").read_text() == ""


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
    assert "ENV GOPATH=/home/node/go" in content
    # ENV must appear before ENTRYPOINT in the file
    assert content.index("ENV GOPATH") < content.index("ENTRYPOINT")


def test_inject_env_multiple_vars(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM scratch{_ENV_INJECTION_ANCHOR} []\n")

    _inject_env_into_dockerfile(
        tmp_path, {"GOPATH": "/home/node/go", "PATH": "/usr/local/go/bin:$PATH"}
    )

    content = dockerfile.read_text()
    assert "ENV GOPATH=/home/node/go" in content
    assert "ENV PATH=/usr/local/go/bin:$PATH" in content


def test_inject_env_empty_leaves_dockerfile_unchanged(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile"
    original = f"FROM scratch{_ENV_INJECTION_ANCHOR} []\n"
    dockerfile.write_text(original)

    _inject_env_into_dockerfile(tmp_path, {})

    assert dockerfile.read_text() == original


@patch("agent_circus.context.build_mcp_compose_override", return_value="{}")
@patch("agent_circus.context.build_agent_configs_override", return_value="{}")
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
    mock_agent_configs: MagicMock,
    mock_mcp: MagicMock,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    template_dir = _make_template_dir(tmp_path)
    mock_tdc.return_value.__enter__ = MagicMock(return_value=template_dir)
    mock_tdc.return_value.__exit__ = MagicMock(return_value=False)

    with build_compose_context(workspace):
        pass

    content = (template_dir / "Dockerfile").read_text()
    assert "ENV PATH=/usr/local/go/bin:$PATH" in content
    assert content.index("ENV PATH") < content.index("ENTRYPOINT")
