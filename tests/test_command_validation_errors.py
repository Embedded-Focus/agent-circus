"""CLI validation error handling tests for service arguments."""

from typer.testing import CliRunner

from agent_circus.cli import app

runner = CliRunner()


def test_up_invalid_service_shows_clean_error() -> None:
    result = runner.invoke(app, ["up", "not-a-service"])

    assert result.exit_code == 1
    assert "Error: Invalid service(s): not-a-service." in result.output
    assert "Traceback" not in result.output


def test_build_invalid_service_shows_clean_error() -> None:
    result = runner.invoke(app, ["build", "not-a-service"])

    assert result.exit_code == 1
    assert "Error: Invalid service(s): not-a-service." in result.output
    assert "Traceback" not in result.output


def test_exec_invalid_service_shows_clean_error() -> None:
    result = runner.invoke(app, ["exec", "not-a-service", "--", "echo", "hi"])

    assert result.exit_code == 1
    assert "Error: Invalid service(s): not-a-service." in result.output
    assert "Traceback" not in result.output
