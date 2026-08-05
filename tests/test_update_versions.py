"""Tests for the agent-circus-update-templates maintainer script."""

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer
from github.GithubException import GithubException

from agent_circus.update_versions import (
    PINS,
    TEMPLATE_DIR,
    PinResult,
    VersionPin,
    apply_changes,
    apply_version,
    build_report,
    fetch_latest_tag,
    main,
    normalize_tag,
    read_current_version,
    sync_template_lockfile,
)


@pytest.mark.parametrize(
    ("tag_name", "strip_prefix", "expected"),
    [
        ("0.22.0", "", "0.22.0"),
        ("v0.58.1", "v", "0.58.1"),
        ("rust-v0.144.1", "rust-v", "0.144.1"),
        ("v4.53.3", "", "v4.53.3"),
    ],
)
def test_normalize_tag(tag_name: str, strip_prefix: str, expected: str) -> None:
    assert normalize_tag(tag_name, strip_prefix) == expected


@pytest.mark.parametrize("pin", PINS, ids=[pin.name for pin in PINS])
def test_read_current_version_matches_real_template(pin: VersionPin) -> None:
    content = pin.file.read_text()
    version = read_current_version(content, pin)
    assert version


def _bump(version: str) -> str:
    """Increment the last dotted component of a version string.

    Keeps the result shaped like a real release version (digits and dots,
    optionally prefixed with ``v``) so it satisfies every pin's pattern,
    unlike an arbitrary suffix such as ``-test``.
    """
    prefix = "v" if version.startswith("v") else ""
    parts = version[len(prefix) :].split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return prefix + ".".join(parts)


@pytest.mark.parametrize("pin", PINS, ids=[pin.name for pin in PINS])
def test_apply_version_round_trips_without_other_changes(pin: VersionPin) -> None:
    content = pin.file.read_text()
    current = read_current_version(content, pin)
    new_version = _bump(current)

    updated = apply_version(content, pin, new_version)

    assert read_current_version(updated, pin) == new_version
    # Splicing at the matched span should leave everything else untouched.
    match = pin.pattern.search(content)
    assert match is not None
    start, end = match.span(1)
    assert updated[:start] == content[:start]
    assert updated[start + len(new_version) :] == content[end:]


def test_apply_version_raises_when_pattern_missing(tmp_path: Path) -> None:
    pin = PINS[0]
    with pytest.raises(ValueError, match=pin.name):
        apply_version("no version here", pin, "1.0.0")


def test_fetch_latest_tag_returns_tag_name() -> None:
    gh = MagicMock()
    gh.get_repo.return_value.get_latest_release.return_value.tag_name = "v1.2.3"

    tag = fetch_latest_tag(gh, "owner/repo")

    assert tag == "v1.2.3"
    gh.get_repo.assert_called_once_with("owner/repo")


def test_build_report_records_error_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def fake_fetch(gh: object, repo: str) -> str:
        calls.append(repo)
        if repo == PINS[0].repo:
            raise GithubException(status=404, message="Not Found")
        return "v9.9.9"

    monkeypatch.setattr("agent_circus.update_versions.fetch_latest_tag", fake_fetch)

    results = build_report(PINS, gh=MagicMock())

    assert len(results) == len(PINS)
    assert len(calls) == len(PINS)

    failed = results[0]
    assert failed.error is not None
    assert failed.latest is None
    assert not failed.outdated

    for pin, result in zip(PINS[1:], results[1:], strict=True):
        assert result.error is None
        assert result.latest == normalize_tag("v9.9.9", pin.strip_prefix)


def test_apply_changes_only_writes_outdated_files(tmp_path: Path) -> None:
    shutil.copytree(
        TEMPLATE_DIR,
        tmp_path / "agent-circus",
        ignore=shutil.ignore_patterns(".venv"),
    )
    dockerfile = tmp_path / "agent-circus" / "Dockerfile"
    compose = tmp_path / "agent-circus" / "compose.yaml"

    yq_pin = next(p for p in PINS if p.name == "yq")
    opencode_pin = next(p for p in PINS if p.name == "opencode")

    local_yq_pin = replace(yq_pin, file=dockerfile)
    local_opencode_pin = replace(opencode_pin, file=compose)

    current_yq = read_current_version(dockerfile.read_text(), local_yq_pin)
    current_opencode = read_current_version(compose.read_text(), local_opencode_pin)

    results = [
        PinResult(pin=local_yq_pin, current=current_yq, latest=current_yq),  # unchanged
        PinResult(
            pin=local_opencode_pin, current=current_opencode, latest="999.0.0"
        ),  # outdated
    ]

    dockerfile_mtime_before = dockerfile.read_text()
    changed = apply_changes(results)

    assert changed == [compose]
    assert dockerfile.read_text() == dockerfile_mtime_before
    assert read_current_version(compose.read_text(), local_opencode_pin) == "999.0.0"


def test_sync_template_lockfile_invokes_uv_sync_in_template_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_run = MagicMock()
    monkeypatch.setattr("agent_circus.update_versions.subprocess.run", mock_run)

    sync_template_lockfile(tmp_path)

    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args == (["uv", "sync"],)
    assert kwargs["cwd"] == tmp_path
    assert kwargs["check"] is True


def test_sync_template_lockfile_strips_ambient_project_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A dev shell for *this* repo (e.g. devenv) commonly exports one of these
    # pointing at the main project's own venv; uv honors it regardless of
    # cwd, so it must not leak into the subprocess env or `uv sync` would
    # clobber the caller's unrelated venv instead of the template's.
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/somewhere/main-project/venv")
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/main-project/venv")
    mock_run = MagicMock()
    monkeypatch.setattr("agent_circus.update_versions.subprocess.run", mock_run)

    sync_template_lockfile(tmp_path)

    passed_env = mock_run.call_args.kwargs["env"]
    assert "UV_PROJECT_ENVIRONMENT" not in passed_env
    assert "VIRTUAL_ENV" not in passed_env


def test_main_syncs_lockfile_when_pyproject_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_result = PinResult(pin=PINS[-1], current="1.0.0", latest="1.0.1")
    monkeypatch.setattr(
        "agent_circus.update_versions.build_report", lambda pins, gh: [dummy_result]
    )
    monkeypatch.setattr(
        "agent_circus.update_versions.apply_changes",
        lambda results: [TEMPLATE_DIR / "pyproject.toml"],
    )
    sync_mock = MagicMock()
    monkeypatch.setattr(
        "agent_circus.update_versions.sync_template_lockfile", sync_mock
    )

    main(apply=True)

    sync_mock.assert_called_once_with()


def test_main_skips_sync_when_pyproject_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_result = PinResult(pin=PINS[0], current="1.0.0", latest="1.0.1")
    monkeypatch.setattr(
        "agent_circus.update_versions.build_report", lambda pins, gh: [dummy_result]
    )
    monkeypatch.setattr(
        "agent_circus.update_versions.apply_changes",
        lambda results: [TEMPLATE_DIR / "Dockerfile"],
    )
    sync_mock = MagicMock()
    monkeypatch.setattr(
        "agent_circus.update_versions.sync_template_lockfile", sync_mock
    )

    main(apply=True)

    sync_mock.assert_not_called()


def test_main_exits_with_error_when_sync_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dummy_result = PinResult(pin=PINS[-1], current="1.0.0", latest="1.0.1")
    monkeypatch.setattr(
        "agent_circus.update_versions.build_report", lambda pins, gh: [dummy_result]
    )
    monkeypatch.setattr(
        "agent_circus.update_versions.apply_changes",
        lambda results: [TEMPLATE_DIR / "pyproject.toml"],
    )

    def failing_sync() -> None:
        raise subprocess.CalledProcessError(returncode=1, cmd=["uv", "sync"])

    monkeypatch.setattr(
        "agent_circus.update_versions.sync_template_lockfile", failing_sync
    )

    with pytest.raises(typer.Exit):
        main(apply=True)
