"""Tests for the msb-circus dashboard command."""

from typer.testing import CliRunner

import msb_circus.cli as cli_module
from msb_circus.cli import app

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
