"""Tests for the shell completion command."""

from pathlib import Path

from typer.testing import CliRunner

from agent_circus.cli import app

runner = CliRunner()


def test_completion_defaults_to_bash() -> None:
    result = runner.invoke(app, ["completion"])

    assert result.exit_code == 0
    assert "_AGENT_CIRCUS_COMPLETE=complete_bash" in result.output
    assert "complete -o default -F" in result.output


def test_completion_accepts_shell_argument() -> None:
    result = runner.invoke(app, ["completion", "zsh"])

    assert result.exit_code == 0
    assert "#compdef agent-circus" in result.output


def test_completion_rejects_unknown_shell() -> None:
    result = runner.invoke(app, ["completion", "notashell"])

    assert result.exit_code != 0
    assert "not one of" in result.output


def test_completion_install_creates_missing_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / ".bash_completion.d" / "agent-circus"

    result = runner.invoke(app, ["completion", "bash", "--install"])

    assert result.exit_code == 0
    assert f"Installed completion script to {target}" in result.output
    assert target.exists()
    assert "_AGENT_CIRCUS_COMPLETE=complete_bash" in target.read_text()


def test_completion_install_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / ".bash_completion.d" / "agent-circus"

    runner.invoke(app, ["completion", "bash", "--install"])
    mtime_before = target.stat().st_mtime_ns

    result = runner.invoke(app, ["completion", "bash", "--install"])

    assert result.exit_code == 0
    assert "already up to date" in result.output
    assert target.stat().st_mtime_ns == mtime_before


def test_completion_install_updates_stale_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / ".bash_completion.d" / "agent-circus"
    target.parent.mkdir(parents=True)
    target.write_text("stale contents")

    result = runner.invoke(app, ["completion", "bash", "--install"])

    assert result.exit_code == 0
    assert f"Installed completion script to {target}" in result.output
    assert "_AGENT_CIRCUS_COMPLETE=complete_bash" in target.read_text()


def test_completion_install_rejects_non_bash_shell(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    result = runner.invoke(app, ["completion", "zsh", "--install"])

    assert result.exit_code != 0
    assert "--install is only supported for bash" in result.output


def test_completion_alias_appends_alias_and_completion_registration() -> None:
    result = runner.invoke(app, ["completion", "bash", "--alias", "ac"])

    assert result.exit_code == 0
    assert "alias ac=agent-circus" in result.output
    assert (
        "_ac_agent_circus_completion() { _agent_circus_completion agent-circus; }"
        in (result.output)
    )
    assert "complete -o default -F _ac_agent_circus_completion ac" in result.output


def test_completion_alias_rejects_non_bash_shell() -> None:
    result = runner.invoke(app, ["completion", "zsh", "--alias", "ac"])

    assert result.exit_code != 0
    assert "--alias is only supported for bash" in result.output


def test_completion_alias_rejects_invalid_alias_name() -> None:
    result = runner.invoke(app, ["completion", "bash", "--alias", "bad alias"])

    assert result.exit_code != 0
    assert "not a valid alias name" in result.output


def test_completion_install_with_alias_writes_both(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    target = tmp_path / ".bash_completion.d" / "agent-circus"

    result = runner.invoke(app, ["completion", "bash", "--install", "--alias", "ac"])

    assert result.exit_code == 0
    content = target.read_text()
    assert "alias ac=agent-circus" in content
    assert "complete -o default -F _ac_agent_circus_completion ac" in content
