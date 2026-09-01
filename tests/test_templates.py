from pathlib import Path

from agent_circus.templates import deploy_templates, template_dir_context


def test_deploy_templates_copies_files(tmp_path: Path) -> None:
    deployed = deploy_templates(tmp_path)

    assert len(deployed) > 0
    for path in deployed:
        assert path.is_file()
    assert not (tmp_path / ".agent-circus" / ".venv").exists()


def test_deploy_templates_skips_existing(tmp_path: Path) -> None:
    deploy_templates(tmp_path)
    second = deploy_templates(tmp_path)

    assert second == []


def test_deploy_templates_force_overwrites(tmp_path: Path) -> None:
    first = deploy_templates(tmp_path)
    second = deploy_templates(tmp_path, force=True)

    assert len(second) == len(first)


def test_deploy_templates_includes_hooks_dir(tmp_path: Path) -> None:
    deploy_templates(tmp_path)

    hooks_dir = tmp_path / ".agent-circus" / "hooks"
    assert hooks_dir.is_dir()
    assert (hooks_dir / "base-root.sh").is_file()
    assert (hooks_dir / "base-user.sh").is_file()


def test_base_user_hook_runs_after_switching_to_node() -> None:
    with template_dir_context() as template_dir:
        dockerfile = (template_dir / "Dockerfile").read_text()

    hook_run_index = dockerfile.index("bash /tmp/hook-base-user.sh")
    before_hook_run = dockerfile[:hook_run_index]
    last_user_line = [
        line.strip()
        for line in before_hook_run.splitlines()
        if line.startswith("USER ")
    ][-1]

    assert last_user_line == "USER node"


def test_claude_code_install_allows_required_postinstall_scripts() -> None:
    with template_dir_context() as template_dir:
        dockerfile = (template_dir / "Dockerfile").read_text()

    assert "npm install -g --allow-scripts=@anthropic-ai/claude-code,bun" in dockerfile


def test_claude_code_auxiliary_tools_are_build_args() -> None:
    with template_dir_context() as template_dir:
        dockerfile = (template_dir / "Dockerfile").read_text()
        compose = (template_dir / "compose.yaml").read_text()

    assert "ARG BUN_VERSION" in dockerfile
    assert "ARG CLAUDE_MEM_VERSION" in dockerfile
    assert "bun@${BUN_VERSION}" in dockerfile
    assert "claude-mem@${CLAUDE_MEM_VERSION}" in dockerfile
    assert "BUN_VERSION:" in compose
    assert "CLAUDE_MEM_VERSION:" in compose
