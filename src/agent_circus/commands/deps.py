"""End-user dependency selection commands."""

import json
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from agent_circus import dependencies
from agent_circus.config import get_workspace_path
from agent_circus.exceptions import ConfigurationError

app = typer.Typer(
    help="Inspect and update container dependencies independently of CLI releases.",
    no_args_is_help=True,
)
Workspace = Annotated[
    Path | None,
    typer.Option(
        "--workspace",
        "-w",
        exists=True,
        file_okay=False,
        resolve_path=True,
        help="Workspace (deployed projects use a project-local selection).",
    ),
]


def execute(action: Callable[[], list[str]]) -> None:
    """Render command output with credential-safe filesystem/configuration errors.

    :param action: Dependency operation.
    """
    try:
        for line in action():
            typer.echo(line)
    except ConfigurationError as error:
        typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(1) from None
    except (OSError, ValueError, KeyError, TypeError):
        typer.echo(
            "Error: Cannot read or write dependency state. Check file permissions and snapshot integrity.",
            err=True,
        )
        raise typer.Exit(1) from None


@app.command()
def show(workspace: Workspace = None) -> None:
    """Show effective tool and Python versions, provenance, and selection scope."""

    def action() -> list[str]:
        project = workspace or get_workspace_path()
        payload = dependencies.current(project)
        manifest = tomllib.loads(payload["versions.toml"])
        packages = tomllib.loads(payload["uv.lock"]).get("package", [])
        return [
            f"Store: {dependencies.storage_dir(project)}",
            f"Snapshot: {dependencies.snapshot_id(payload)[:12]} ({payload['source']})",
            *[f"{name}: {version}" for name, version in manifest["versions"].items()],
            "Python dependencies:",
            *[
                f"  {package['name']}: {package['version']}"
                for package in packages
                if "registry" in package.get("source", {})
            ],
        ]

    execute(action)


@app.command()
def update(
    workspace: Workspace = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            help="Preview upstream tool versions without changing selection or resolving Python transitives."
        ),
    ] = False,
) -> None:
    """Resolve stable versions with host uv; Python is managed automatically."""
    typer.echo(
        "Checking upstream versions..."
        if dry_run
        else "Resolving upstream versions and Python dependencies..."
    )
    execute(lambda: dependencies.update(workspace or get_workspace_path(), dry_run))


@app.command()
def history(workspace: Workspace = None) -> None:
    """List saved immutable snapshots; no upstream access is needed."""

    def action() -> list[str]:
        root = dependencies.storage_dir(workspace or get_workspace_path())
        selected = dependencies.selection(root)
        lines = [f"Store: {root}"]
        for path in sorted((root / "snapshots").glob("*/metadata.json")):
            metadata = json.loads(path.read_text())
            identifier = path.parent.name
            flags = ", ".join(
                key for key, value in selected.items() if value == identifier
            )
            lines.append(
                f"{identifier[:12]} {metadata['created']} {metadata['source']} {flags}".rstrip()
            )
        if selected["active"] is None:
            lines.append(
                "Active: default template versions (no explicit snapshot selected)."
            )
        return lines

    execute(action)


@app.command()
def rollback(workspace: Workspace = None) -> None:
    """Restore the previous selection; rebuild and recreate containers afterward."""
    execute(lambda: [dependencies.restore(workspace or get_workspace_path())])


@app.command()
def reset(workspace: Workspace = None) -> None:
    """Return to this CLI release's bundled baseline without upstream access."""
    execute(
        lambda: [dependencies.restore(workspace or get_workspace_path(), reset=True)]
    )
