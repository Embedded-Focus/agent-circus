"""Tests for the agent-circus proxy command's shell wiring."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from typer.testing import CliRunner

import agent_circus.cli as cli_module
import agent_circus.proxy as proxy_module
from agent_circus.cli import app

runner = CliRunner()


class FakeProxyInvoker:
    """Minimal invoker for the proxy command (nothing is executed)."""

    behavior: ClassVar[dict] = {}

    def __init__(self, binary: Path = Path("/fake/msb")) -> None:
        self.binary = binary

    def run(self, args, **kwargs):
        self.behavior.setdefault("calls", []).append(args)
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")


@pytest.fixture
def fake_proxy(monkeypatch, tmp_path):
    """Patch invoker resolution and serve; record the DashboardApp."""
    FakeProxyInvoker.behavior = {}
    config_dir = tmp_path / ".agent-circus"
    config_dir.mkdir()
    (config_dir / "config.toml").write_bytes(b"")
    monkeypatch.setattr(
        cli_module, "_resolve_invoker", lambda w: (None, FakeProxyInvoker(tmp_path))
    )
    captured: dict = {}

    async def fake_serve(port, resolver, *, tracker=None, dashboard=None):
        captured["dashboard"] = dashboard

        class FakeServer:
            async def serve_forever(self):
                await asyncio.Event().wait()

            async def wait_closed(self):
                return None

            def close(self):
                pass

        return [FakeServer(), FakeServer()]

    async def fake_gather(*coros):
        return []

    monkeypatch.setattr(cli_module, "serve", fake_serve)
    monkeypatch.setattr(proxy_module.asyncio, "gather", fake_gather)
    monkeypatch.setattr(
        cli_module, "shell_token_path", lambda: tmp_path / "runtime" / "shell-token"
    )
    return captured, tmp_path


def test_proxy_without_shell_flag_disables_shell_and_creates_no_token(
    tmp_path, fake_proxy, monkeypatch
):
    captured, tmp = fake_proxy
    # serve must return quickly: make asyncio.run return after one call.
    monkeypatch.setattr(
        cli_module.asyncio,
        "run",
        lambda coro: _drain(coro, captured),
    )
    result = runner.invoke(app, ["proxy", "-w", str(tmp_path)])
    assert result.exit_code == 0
    assert "Shell: off" in result.output
    assert "#token=" not in result.output
    dashboard = captured.get("dashboard")
    assert dashboard is not None
    assert dashboard.shells.enabled is False
    assert not (tmp / "runtime" / "shell-token").exists()


_REAL_RUN = asyncio.run


def _drain(coro, captured):
    """Run a serve coroutine briefly, then stop (the proxy runs forever)."""

    async def wrap():
        try:
            await asyncio.wait_for(coro, 0.2)
        except TimeoutError:
            pass
        return 0

    return _REAL_RUN(wrap())


def test_proxy_with_shell_flag_creates_token_and_prints_url(
    tmp_path, fake_proxy, monkeypatch
):
    captured, tmp = fake_proxy
    monkeypatch.setattr(cli_module.asyncio, "run", lambda coro: _drain(coro, captured))
    result = runner.invoke(app, ["proxy", "--shell", "-w", str(tmp_path)])
    assert result.exit_code == 0
    assert "Shell: on" in result.output
    token_file = tmp / "runtime" / "shell-token"
    assert token_file.exists()
    token = token_file.read_text()
    assert f"#token={token}" in result.output
    dashboard = captured.get("dashboard")
    assert dashboard is not None
    assert dashboard.shells.enabled is True
    import stat

    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_proxy_with_shell_flag_fails_when_token_file_cannot_be_written(
    tmp_path, fake_proxy, monkeypatch
):
    _captured, tmp = fake_proxy
    # The parent path is a regular file, so creating the token directory
    # fails with an OSError (this container runs as root, so directory
    # permissions cannot simulate it).
    blocker = tmp / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr(
        cli_module,
        "shell_token_path",
        lambda: blocker / "shell-token",
    )
    result = runner.invoke(app, ["proxy", "--shell", "-w", str(tmp_path)])
    assert result.exit_code == 1
    assert "cannot create the shell token file" in result.output


def test_proxy_audit_lines_use_the_logger(tmp_path, fake_proxy, monkeypatch):
    """Shell open/close lines go through the logger (with a level)."""

    captured, _tmp = fake_proxy
    monkeypatch.setattr(cli_module.asyncio, "run", lambda coro: _drain(coro, captured))
    runner.invoke(app, ["proxy", "--shell", "-w", str(tmp_path)])
    dashboard = captured.get("dashboard")
    assert dashboard is not None
    # The CLI routes the audit lines through its module logger, so they
    # carry a level like every other proxy log line.
    assert dashboard.shells._notify == cli_module.logger.info
