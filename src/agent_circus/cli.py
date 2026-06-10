"""Main CLI entry point for Agent Circus."""

from pathlib import Path
from typing import Annotated

import typer

from agent_circus.commands import (
    build,
    config_store,
    destroy,
    exec_,
    init,
    ps,
    remove,
    up,
)
from agent_circus.config import load_user_config
from agent_circus.utils import setup_logging

app = typer.Typer(
    name="agent-circus",
    help="CLI for managing agent containers.",
    no_args_is_help=True,
)


@app.callback()
def main(
    log_level: Annotated[
        str | None,
        typer.Option(
            "--log-level",
            envvar="LOGLEVEL",
            help="Set the log level (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
        ),
    ] = None,
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
    logging_cfg = load_user_config().get("logging", {})
    setup_logging(
        level=log_level or logging_cfg.get("level", "INFO"),
        log_file=log_file
        or (Path(logging_cfg["file"]) if logging_cfg.get("file") else None),
    )


app.command()(init.init)
app.command()(build.build)
app.command()(up.up)
app.command()(ps.ps)
app.command(name="exec")(exec_.exec_cmd)
app.command()(remove.remove)
app.command(name="rm", hidden=True)(remove.remove)
app.command()(destroy.destroy)
app.add_typer(config_store.app, name="config")


def run_cli() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    run_cli()
