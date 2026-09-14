"""Tests for the version command."""

from pathlib import Path
from unittest.mock import Mock

from typer.testing import CliRunner

from agent_circus import __version__
from agent_circus.cli import app
from agent_circus.commands import version

runner = CliRunner()


def test_version_prints_package_version_and_checkout_commit(monkeypatch) -> None:
    commit = "1" * 40
    monkeypatch.setattr(version, "git_commit", lambda: commit)

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output == f"agent-circus {__version__} (git {commit})\n"


def test_version_is_available_without_loading_configuration(monkeypatch) -> None:
    def fail_if_called() -> None:
        raise AssertionError("configuration should not be loaded")

    monkeypatch.setattr("agent_circus.cli.load_user_config", fail_if_called)

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0


def test_version_rejects_extra_arguments() -> None:
    result = runner.invoke(app, ["version", "ps"])

    assert result.exit_code == 2
    assert "Got unexpected extra argument(s) (ps)" in result.output
    assert f"agent-circus {__version__} (git" not in result.output


def test_checkout_path_reads_local_directory_install_metadata(monkeypatch) -> None:
    metadata = Mock()
    metadata.read_text.return_value = (
        '{"url":"file:///tmp/agent%20circus","dir_info":{"editable":true}}'
    )
    monkeypatch.setattr(version, "distribution", lambda name: metadata)

    assert version.checkout_path() == Path("/tmp/agent circus")


def test_checkout_path_ignores_distribution_artifact(monkeypatch) -> None:
    metadata = Mock()
    metadata.read_text.return_value = (
        '{"url":"file:///tmp/agent-circus.whl","archive_info":{}}'
    )
    monkeypatch.setattr(version, "distribution", lambda name: metadata)

    assert version.checkout_path() is None


def test_git_commit_prefers_checkout_over_embedded_hash(monkeypatch) -> None:
    checkout = Path("/checkout")
    monkeypatch.setattr(version, "checkout_path", lambda: checkout)
    monkeypatch.setattr(version, "checkout_commit", lambda path: "2" * 40)
    monkeypatch.setattr(version, "GIT_COMMIT", "3" * 40)

    assert version.git_commit() == "2" * 40


def test_git_commit_uses_embedded_hash_for_distribution(monkeypatch) -> None:
    monkeypatch.setattr(version, "checkout_path", lambda: None)
    monkeypatch.setattr(version, "GIT_COMMIT", "3" * 40)

    assert version.git_commit() == "3" * 40


def test_version_reports_when_commit_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(version, "git_commit", lambda: None)

    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output == f"agent-circus {__version__} (git unknown)\n"
