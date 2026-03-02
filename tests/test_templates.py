from pathlib import Path

from agent_circus.templates import deploy_templates


def test_deploy_templates_copies_files(tmp_path: Path) -> None:
    deployed = deploy_templates(tmp_path)

    assert len(deployed) > 0
    for path in deployed:
        assert path.is_file()


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
