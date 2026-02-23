"""Main CLI entry point for Agent Circus."""

from pathlib import Path
from typing import Annotated

import typer

from agent_circus.commands import build, exec_, init, ps, remove, up
from agent_circus.utils import setup_logging

app = typer.Typer(
    name="agent-circus",
    help="CLI for managing agent containers.",
    no_args_is_help=True,
)


@app.callback()
def main(
    log_level: Annotated[
        str,
        typer.Option(
            "--log-level",
            envvar="LOGLEVEL",
            help="Set the log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
        ),
    ] = "INFO",
    log_file: Annotated[
        Path | None,
        typer.Option(
            "--log-file",
            envvar="LOGFILE",
            help="Path to a log file. Logs are written to both stdout and this file.",
        ),
    ] = None,
) -> None:
    """CLI for managing agent containers."""
    setup_logging(level=log_level, log_file=log_file)


app.command()(init.init)
app.command()(build.build)
app.command()(up.up)
app.command()(ps.ps)
app.command(name="exec")(exec_.exec_cmd)
app.command()(remove.remove)
app.command(name="rm", hidden=True)(remove.remove)


def run_cli() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    run_cli()
