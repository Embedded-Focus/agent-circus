import tomllib
from pathlib import Path
from zipfile import Path as ZipPath
from zipfile import ZipFile

import pytest

from agent_circus import dependencies, templates
from agent_circus.templates import deploy_templates, template_dir_context


def test_zipped_templates_extract_deploy_and_clean_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path = tmp_path / "templates.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("agent-circus/Dockerfile", "FROM scratch\n")
        archive.writestr("agent-circus/hooks/base-root.sh", "echo hook\n")

    with ZipFile(archive_path) as archive:
        monkeypatch.setattr(templates, "TEMPLATES", ZipPath(archive))
        with template_dir_context() as extracted:
            assert (extracted / "Dockerfile").read_text() == "FROM scratch\n"
            assert (extracted / "hooks/base-root.sh").read_text() == "echo hook\n"
        assert not extracted.exists()

        deploy_templates(tmp_path)
        assert (tmp_path / ".agent-circus/Dockerfile").read_text() == "FROM scratch\n"
        assert (
            tmp_path / ".agent-circus/hooks/base-root.sh"
        ).read_text() == "echo hook\n"


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


def test_entrypoint_uses_init_to_forward_signals() -> None:
    with template_dir_context() as template_dir:
        dockerfile = (template_dir / "Dockerfile").read_text()

    assert "  tini \\\n" in dockerfile
    assert 'ENTRYPOINT [ "/usr/bin/tini", "--", "/docker-entrypoint.sh" ]' in dockerfile


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


def test_mistral_vibe_python_version_matches_dependency_resolver() -> None:
    with template_dir_context() as template_dir:
        dockerfile = (template_dir / "Dockerfile").read_text()
        project = tomllib.loads((template_dir / "pyproject.toml").read_text())
        lock = tomllib.loads((template_dir / "uv.lock").read_text())

    version = dependencies.PYTHON_RESOLUTION_VERSION
    requirement = f"=={version}.*"
    assert project["project"]["requires-python"] == requirement
    assert lock["requires-python"] == requirement
    assert f"--python {version}" in dockerfile
