"""Tests for the msb-circus dashboard command."""

from typer.testing import CliRunner

import agent_circus.cli as cli_module
from agent_circus.cli import app

runner = CliRunner()


def test_dashboard_command_reports_proxy_not_running(monkeypatch):
    monkeypatch.setattr(cli_module, "resolve_proxy_port", lambda v: 7700)
    monkeypatch.setattr(cli_module, "_proxy_probe_running", lambda port: False)
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    result = runner.invoke(app, ["dashboard"])
    assert result.exit_code == 1
    assert "http://localhost:7700/" in result.output
    assert "the proxy is not running" in result.output
    assert opened == []


def test_dashboard_command_opens_browser(monkeypatch):
    monkeypatch.setattr(cli_module, "resolve_proxy_port", lambda v: 7700)
    monkeypatch.setattr(cli_module, "_proxy_probe_running", lambda port: True)
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    result = runner.invoke(app, ["dashboard"])
    assert result.exit_code == 0
    assert "http://localhost:7700/" in result.output
    assert opened == ["http://localhost:7700/"]


def test_dashboard_command_no_browser_only_prints(monkeypatch):
    monkeypatch.setattr(cli_module, "resolve_proxy_port", lambda v: 7700)
    monkeypatch.setattr(cli_module, "_proxy_probe_running", lambda port: True)
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    result = runner.invoke(app, ["dashboard", "--no-browser"])
    assert result.exit_code == 0
    assert "http://localhost:7700/" in result.output
    assert opened == []


def test_dashboard_command_resolves_env_port(monkeypatch):
    monkeypatch.setattr(cli_module, "resolve_proxy_port", lambda v: 7701)
    monkeypatch.setattr(cli_module, "_proxy_probe_running", lambda port: True)
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    result = runner.invoke(app, ["dashboard"])
    assert "http://localhost:7701/" in result.output


def test_dashboard_here_opens_detail_url(tmp_path, monkeypatch):
    workspace = tmp_path / "my-proj"
    workspace.mkdir()
    monkeypatch.setattr(cli_module, "resolve_proxy_port", lambda v: 7700)
    monkeypatch.setattr(cli_module, "_proxy_probe_running", lambda port: True)
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    result = runner.invoke(app, ["dashboard", "--here", "-w", str(workspace)])
    assert result.exit_code == 0
    assert result.output.strip().endswith("#/sandbox/agents-my-proj")
    assert opened == [result.output.strip()]


def test_dashboard_here_with_no_browser_prints_detail_url(tmp_path, monkeypatch):
    workspace = tmp_path / "my proj"  # space -> percent-encoded
    workspace.mkdir()
    monkeypatch.setattr(cli_module, "resolve_proxy_port", lambda v: 7700)
    monkeypatch.setattr(cli_module, "_proxy_probe_running", lambda port: True)
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    result = runner.invoke(
        app, ["dashboard", "--here", "--no-browser", "-w", str(workspace)]
    )
    assert result.exit_code == 0
    assert "#/sandbox/agents-my-proj" in result.output
    assert opened == []


def test_dashboard_without_here_keeps_root_url(monkeypatch):
    monkeypatch.setattr(cli_module, "resolve_proxy_port", lambda v: 7700)
    monkeypatch.setattr(cli_module, "_proxy_probe_running", lambda port: True)
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    result = runner.invoke(app, ["dashboard"])
    assert result.exit_code == 0
    assert result.output.startswith("http://localhost:7700/\n")
    assert "#/sandbox/" not in result.output


def test_dashboard_url_contains_token_fragment(tmp_path, monkeypatch):
    token_file = tmp_path / "shell-token"
    token_file.write_text("a" * 40)
    token_file.chmod(0o600)
    monkeypatch.setattr(cli_module, "shell_token_path", lambda: token_file)
    monkeypatch.setattr(cli_module, "resolve_proxy_port", lambda v: 7700)
    monkeypatch.setattr(cli_module, "_proxy_probe_running", lambda port: True)
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    result = runner.invoke(app, ["dashboard"])
    assert result.exit_code == 0
    assert result.output.startswith("http://localhost:7700/#token=" + "a" * 40)


def test_dashboard_here_url_contains_token_after_route(tmp_path, monkeypatch):
    workspace = tmp_path / "my-proj"
    workspace.mkdir()
    token_file = tmp_path / "shell-token"
    token_file.write_text("b" * 40)
    token_file.chmod(0o600)
    monkeypatch.setattr(cli_module, "shell_token_path", lambda: token_file)
    monkeypatch.setattr(cli_module, "resolve_proxy_port", lambda v: 7700)
    monkeypatch.setattr(cli_module, "_proxy_probe_running", lambda port: True)
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    result = runner.invoke(app, ["dashboard", "--here", "-w", str(workspace)])
    assert result.exit_code == 0
    assert "#/sandbox/agents-my-proj&token=" + "b" * 40 in result.output


def test_dashboard_url_without_token_file_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_module, "shell_token_path", lambda: tmp_path / "nope")
    monkeypatch.setattr(cli_module, "resolve_proxy_port", lambda v: 7700)
    monkeypatch.setattr(cli_module, "_proxy_probe_running", lambda port: True)
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    result = runner.invoke(app, ["dashboard"])
    assert result.exit_code == 0
    assert result.output.startswith("http://localhost:7700/\n")
