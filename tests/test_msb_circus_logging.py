"""Tests for msb-circus logging setup (CLI options and [logging] config)."""

import logging

import pytest
from typer.testing import CliRunner

from msb_circus.cli import app
from msb_circus.utils import setup_logging

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean_root_handlers():
    """Keep the root logger pristine around each test."""
    handlers = logging.root.handlers[:]
    level = logging.root.level
    logging.root.handlers.clear()
    yield
    logging.root.handlers[:] = handlers
    logging.root.setLevel(level)


def _file_handlers() -> list[logging.FileHandler]:
    return [h for h in logging.root.handlers if isinstance(h, logging.FileHandler)]


def test_setup_logging_writes_to_stderr_and_file(tmp_path):
    log_file = tmp_path / "msbc.log"
    setup_logging(level="DEBUG", log_file=log_file)
    logging.getLogger("msb_circus.test").debug("hello file")
    for handler in logging.root.handlers:
        handler.flush()
    assert "hello file" in log_file.read_text()


def test_setup_logging_without_file_uses_stderr_only():
    setup_logging(level="INFO")
    assert _file_handlers() == []


def test_cli_log_file_option_creates_log_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    log_file = tmp_path / "out.log"
    # `version` may exit non-zero (no msb binary here); only logging matters.
    runner.invoke(app, ["--log-level", "DEBUG", "--log-file", str(log_file), "version"])
    assert any(isinstance(h, logging.FileHandler) for h in logging.root.handlers)
    assert log_file.exists()


def test_cli_applies_logging_defaults_from_config(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / ".agent-circus").mkdir(parents=True)
    (project / ".agent-circus" / "config.toml").write_bytes(
        b'[logging]\nlevel = "DEBUG"\nfile = "from-config.log"\n'
    )
    monkeypatch.chdir(project)
    runner.invoke(app, ["version"])
    handlers = logging.root.handlers
    assert any(isinstance(h, logging.FileHandler) for h in handlers)
    assert (project / "from-config.log").exists()
    assert logging.root.level == logging.DEBUG


def test_cli_cli_options_win_over_config(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / ".agent-circus").mkdir(parents=True)
    (project / ".agent-circus" / "config.toml").write_bytes(
        b'[logging]\nlevel = "INFO"\nfile = "from-config.log"\n'
    )
    monkeypatch.chdir(project)
    cli_log = tmp_path / "cli.log"
    runner.invoke(app, ["--log-file", str(cli_log), "version"])
    assert (project / "from-config.log").exists() is False
    assert cli_log.exists()


def test_cli_invalid_config_falls_back_to_defaults(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / ".agent-circus").mkdir(parents=True)
    (project / ".agent-circus" / "config.toml").write_bytes(b"logging = 42\n")
    monkeypatch.chdir(project)
    result = runner.invoke(app, ["version"])
    assert logging.root.level == logging.INFO
    assert _file_handlers() == []
    assert result.exit_code != 0  # the command itself still surfaces the error
