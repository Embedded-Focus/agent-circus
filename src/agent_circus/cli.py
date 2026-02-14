"""Main CLI entry point for Agent Circus."""

import typer

from agent_circus.commands import build, init, ps, remove, up

app = typer.Typer(
    name="agent-circus",
    help="CLI for managing agent containers.",
    no_args_is_help=True,
)

app.command()(init.init)
app.command()(build.build)
app.command()(up.up)
app.command()(ps.ps)
app.command()(remove.remove)
app.command(name="rm", hidden=True)(remove.remove)


def run_cli() -> None:
    """Entry point for the CLI."""
    app()


if __name__ == "__main__":
    run_cli()
