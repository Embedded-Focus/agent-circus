"""Dependency snapshots, failure atomicity, and build integration."""

import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from github.GithubException import GithubException
from typer.testing import CliRunner

from agent_circus import dependencies as deps
from agent_circus.cli import app
from agent_circus.exceptions import ConfigurationError
from agent_circus.templates import deploy_templates, template_dir_context
from agent_circus.update_versions import PINS, apply_version


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    project = tmp_path / "project"
    project.mkdir()
    return project


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "github_client", MagicMock())
    monkeypatch.setattr(
        deps,
        "fetch_latest_version",
        lambda gh, pin: "v999.0.0" if pin.name == "yq" else "999.0.0",
    )

    def lock_python(directory: Path) -> None:
        path = directory / "uv.lock"
        previous = deps.versions(directory)["mistral-vibe"]
        path.write_text(path.read_text().replace(previous, "999.0.0"))

    monkeypatch.setattr(deps, "lock_python", lock_python)


def test_update_snapshots_and_rollback_preserve_baseline(
    workspace: Path, upstream: None
) -> None:
    baseline = deps.current(workspace)
    deps.update(workspace)
    updated = deps.current(workspace)
    assert deps.snapshot_id(updated) != deps.snapshot_id(baseline)
    assert updated["uv.lock"] != baseline["uv.lock"]
    assert deps.selection(deps.storage_dir(workspace))["previous"] == deps.snapshot_id(
        baseline
    )
    deps.restore(workspace)
    assert deps.current(workspace) == baseline
    deps.restore(workspace)
    assert deps.current(workspace) == updated
    deps.restore(workspace, reset=True)
    assert deps.selection(deps.storage_dir(workspace))["active"] is None
    assert deps.current(workspace) == baseline


def test_dry_run_writes_nothing(
    workspace: Path, upstream: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = MagicMock()
    monkeypatch.setattr(deps, "lock_python", lock)
    assert "Preview only" in deps.update(workspace, dry_run=True)[-1]
    assert not deps.storage_dir(workspace).exists()
    lock.assert_not_called()


@pytest.mark.parametrize("failure", ["lookup", "python", "prerelease"])
def test_failed_update_retains_selection(
    workspace: Path, upstream: None, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    deps.update(workspace)
    root = deps.storage_dir(workspace)
    selected = (root / "selection.json").read_bytes()
    snapshots = list((root / "snapshots").iterdir())
    if failure == "lookup":
        monkeypatch.setattr(
            deps,
            "fetch_latest_version",
            MagicMock(side_effect=GithubException(401, {"message": "secret-token"})),
        )
    elif failure == "python":
        monkeypatch.setattr(
            deps,
            "lock_python",
            MagicMock(side_effect=ConfigurationError("resolution failed")),
        )
    else:
        monkeypatch.setattr(
            deps, "fetch_latest_version", lambda gh, pin: "1000.0.0-rc1"
        )
    with pytest.raises(ConfigurationError) as caught:
        deps.update(workspace)
    assert "secret-token" not in str(caught.value)
    assert (root / "selection.json").read_bytes() == selected
    assert list((root / "snapshots").iterdir()) == snapshots


def test_update_does_not_downgrade_tools(
    workspace: Path, upstream: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    deps.update(workspace)
    before = deps.selection(deps.storage_dir(workspace))
    monkeypatch.setattr(
        deps,
        "fetch_latest_version",
        lambda gh, pin: "v1.0.0" if pin.name == "yq" else "1.0.0",
    )
    report = deps.update(workspace)
    assert "keeping current" in report[0]
    assert deps.selection(deps.storage_dir(workspace)) == before
    assert len(list((deps.storage_dir(workspace) / "snapshots").iterdir())) == 2


def test_project_selection_is_isolated_from_user_selection(
    workspace: Path, upstream: None
) -> None:
    deps.update(workspace)
    global_root = deps.storage_dir(workspace)
    deploy_templates(workspace)
    assert deps.storage_dir(workspace) == workspace / ".agent-circus/dependencies"
    assert deps.current(workspace)["source"] == "release-tested baseline"
    deps.update(workspace)
    assert deps.selection(global_root)["active"] is not None
    deps.restore(workspace, reset=True)
    assert deps.current(workspace)["source"] == "release-tested baseline"


def test_deployed_override_preserves_files_and_build_paths(
    workspace: Path, upstream: None
) -> None:
    deploy_templates(workspace)
    deployed = workspace / ".agent-circus"
    compose_before = (deployed / "compose.yaml").read_bytes()
    dockerfile_before = (deployed / "Dockerfile").read_bytes()
    (deployed / "hooks/base-root.sh").write_text("echo custom-hook\n")
    deps.update(workspace)
    with deps.deployed_override(workspace) as override:
        assert override is not None
        service = json.loads(override.read_text())["services"]["codex"]
        candidate = Path(service["build"]["context"])
        assert service["build"]["args"]["CODEX_VERSION"] == "999.0.0"
        assert (candidate / "hooks/base-root.sh").read_text() == "echo custom-hook\n"
        assert "999.0.0" in (candidate / "Dockerfile").read_text()
        assert not (candidate / "dependencies").exists()
    assert not candidate.exists()
    assert (deployed / "compose.yaml").read_bytes() == compose_before
    assert (deployed / "Dockerfile").read_bytes() == dockerfile_before


def test_custom_deployed_recipes_require_migration(
    workspace: Path, upstream: None
) -> None:
    deploy_templates(workspace)
    path = workspace / ".agent-circus/Dockerfile"
    path.write_text(path.read_text() + "\nRUN echo customized\n")
    with pytest.raises(ConfigurationError, match="Back up"):
        deps.update(workspace)
    assert not (deps.storage_dir(workspace) / "selection.json").exists()


def test_snapshot_corruption_and_selection_traversal_rejected(
    workspace: Path, upstream: None
) -> None:
    deps.update(workspace)
    root = deps.storage_dir(workspace)
    identifier = deps.selection(root)["active"]
    assert identifier is not None
    (root / "snapshots" / identifier / "uv.lock").write_text("corrupted")
    with pytest.raises(ConfigurationError, match="corrupted"):
        deps.current(workspace)
    (root / "selection.json").write_text(
        json.dumps({"active": "../../elsewhere", "previous": None})
    )
    with pytest.raises(ConfigurationError, match="identifier"):
        deps.current(workspace)


def test_old_baseline_remains_available_after_cli_upgrade(
    workspace: Path, upstream: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    old = deps.current(workspace)
    deps.update(workspace)
    with template_dir_context() as source:
        new_baseline = tmp_path / "new-baseline"
        shutil.copytree(source, new_baseline, ignore=shutil.ignore_patterns(".venv"))
    pin = PINS[0]
    path = new_baseline / pin.file.name
    path.write_text(apply_version(path.read_text(), pin, "777.0.0"))
    import contextlib

    monkeypatch.setattr(
        deps, "template_dir_context", lambda: contextlib.nullcontext(new_baseline)
    )
    deps.restore(workspace)
    assert deps.current(workspace) == old
    deps.restore(workspace, reset=True)
    assert (
        tomllib.loads(deps.current(workspace)["versions.toml"])["versions"][pin.name]
        == "777.0.0"
    )


@pytest.mark.parametrize(
    ("primary", "fallback", "expected"),
    [
        ("primary", "fallback", "primary"),
        (None, "fallback", "fallback"),
        (None, None, None),
    ],
)
def test_github_token_precedence(
    monkeypatch: pytest.MonkeyPatch,
    primary: str | None,
    fallback: str | None,
    expected: str | None,
) -> None:
    for name, value in (("GITHUB_TOKEN", primary), ("GH_TOKEN", fallback)):
        monkeypatch.delenv(name, raising=False)
        if value:
            monkeypatch.setenv(name, value)
    factory = MagicMock()
    monkeypatch.setattr(deps, "Github", factory)
    deps.github_client()
    auth = factory.call_args.kwargs["auth"]
    assert (auth.token if auth else None) == expected
    assert factory.call_args.kwargs["retry"] == 0


def test_python_lock_does_not_sync_or_inherit_discovery_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("VIRTUAL_ENV", "/unrelated")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/unrelated")
    monkeypatch.setattr(deps.shutil, "which", lambda cmd: "/bin/uv")
    run = MagicMock()
    monkeypatch.setattr(deps.subprocess, "run", run)
    deps.lock_python(tmp_path)
    run.assert_called_once()
    assert run.call_args.args[0] == [
        "uv",
        "lock",
        "--upgrade",
        "--prerelease",
        "disallow",
        "--no-config",
    ]
    assert (
        not {"GITHUB_TOKEN", "GH_TOKEN", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}
        & run.call_args.kwargs["env"].keys()
    )
    run.side_effect = subprocess.CalledProcessError(1, ["uv"], stderr="secret")
    with pytest.raises(ConfigurationError) as caught:
        deps.lock_python(tmp_path)
    assert "secret" not in str(caught.value)


def test_python_resolution_rejects_downgrades_and_new_prereleases() -> None:
    def locked(version: str) -> str:
        return f'[[package]]\nname = "example"\nversion = "{version}"\nsource = {{ registry = "https://pypi.org/simple" }}\n'

    with pytest.raises(ConfigurationError, match="downgrade"):
        deps.validate_python(locked("2.0"), locked("1.9"))
    with pytest.raises(ConfigurationError, match="prerelease"):
        deps.validate_python(locked("2.0"), locked("3.0rc1"))
    deps.validate_python(locked("2.0"), locked("2.1"))


def test_cli_commands_work_without_docker(workspace: Path, upstream: None) -> None:
    runner = CliRunner()
    for command in ("show", "update", "history", "rollback", "reset"):
        result = runner.invoke(app, ["deps", command, "--workspace", str(workspace)])
        assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["deps", "update", "--dry-run", "-w", str(workspace)])
    assert result.exit_code == 0
    assert "Preview only" in result.output


def test_incompatible_snapshot_rejected(workspace: Path, upstream: None) -> None:
    deps.update(workspace)
    with deps.recipe_copy(workspace) as candidate:
        path = candidate / "Dockerfile"
        path.write_text(path.read_text() + "\n# incompatible recipe\n")
        with pytest.raises(ConfigurationError, match="incompatible"):
            deps.apply_snapshot(candidate, deps.current(workspace))


@pytest.mark.parametrize("deployed", [False, True])
def test_build_context_uses_selection_without_modifying_originals(
    workspace: Path,
    upstream: None,
    monkeypatch: pytest.MonkeyPatch,
    deployed: bool,
) -> None:
    from agent_circus.compose import _exec_compose
    from agent_circus.context import build_compose_context

    monkeypatch.setenv("XDG_STATE_HOME", str(workspace / "state"))
    monkeypatch.setattr("agent_circus.context.load_config", lambda project: {})
    if deployed:
        deploy_templates(workspace)
    deps.update(workspace)
    run = MagicMock(
        return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    monkeypatch.setattr("agent_circus.compose.subprocess.run", run)
    with build_compose_context(workspace) as context:
        if deployed:
            assert context.compose_file == workspace / ".agent-circus/compose.yaml"
            assert context.cwd == workspace / ".agent-circus"
            assert context.dependency_override_file is not None
            assert context.dependency_override_file.exists()
        else:
            assert "999.0.0" in context.compose_file.read_text()
            assert context.env is not None
            assert context.env["AGENT_CIRCUS_WORKSPACE"] == str(workspace)
        _exec_compose(["build"], context)
        command = run.call_args.args[0]
        assert command[command.index("-f") + 1] == str(context.compose_file)
        if deployed:
            assert str(context.dependency_override_file) in command
        else:
            assert not (workspace / ".agent-circus").exists()


def test_interrupted_activation_retains_old_selection(
    workspace: Path, upstream: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    deps.update(workspace)
    root = deps.storage_dir(workspace)
    before = (root / "selection.json").read_bytes()
    with deps.store_lock(root):
        previous = deps.selection(root)["active"]
        assert previous is not None
        monkeypatch.setattr(
            Path, "replace", MagicMock(side_effect=OSError("interrupted"))
        )
        with pytest.raises(OSError):
            deps.activate(root, None, previous)
    assert (root / "selection.json").read_bytes() == before


def test_cli_hides_upstream_error_credentials(
    workspace: Path, upstream: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        deps,
        "fetch_latest_version",
        MagicMock(side_effect=OSError("https://secret-token@registry.invalid")),
    )
    result = CliRunner().invoke(app, ["deps", "update", "-w", str(workspace)])
    assert result.exit_code == 1
    assert "Upstream lookup failed" in result.output
    assert "secret-token" not in result.output


@pytest.mark.parametrize(
    "value", ["latest", "1.2", "1.2.3-rc1", "1.2.3;echo unsafe", "1.2.3\n"]
)
def test_stable_version_rejects_unsupported_values(value: str) -> None:
    with pytest.raises(ConfigurationError):
        deps.stable_version(value)


def test_stable_version_compares_numbers() -> None:
    assert deps.stable_version("v1.10.0") > deps.stable_version("1.9.0")


def test_mistral_manifest_uses_locked_version(workspace: Path) -> None:
    with deps.recipe_copy(workspace) as candidate:
        path = candidate / "uv.lock"
        old = deps.versions(candidate)["mistral-vibe"]
        path.write_text(path.read_text().replace(old, "999.0.0"))
        payload = deps.snapshot(candidate, "test")
        assert (
            tomllib.loads(payload["versions.toml"])["versions"]["mistral-vibe"]
            == "999.0.0"
        )
