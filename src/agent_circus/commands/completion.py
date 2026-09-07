"""Print and install shell completion scripts for agent-circus."""

import re
from pathlib import Path
from typing import Annotated

import typer
from typer.completion import Shells, get_completion_script

_PROG_NAME = "agent-circus"
_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")


def _completion_func_name() -> str:
    """Name of the bash function the base completion script defines.

    :returns: Function name, matching Typer's own naming scheme.
    """
    cf_name = re.sub(r"[^A-Za-z0-9_]", "", _PROG_NAME.replace("-", "_"))
    return f"_{cf_name}_completion"


def _completion_script(shell: Shells) -> str:
    """Render the completion script for a shell.

    :param shell: Target shell.
    :returns: Completion script contents.
    """
    complete_var = f"_{_PROG_NAME.upper().replace('-', '_')}_COMPLETE"
    return get_completion_script(
        prog_name=_PROG_NAME, complete_var=complete_var, shell=shell.value
    )


def _alias_snippet(alias: str) -> str:
    """Render a bash snippet defining a shell alias wired up for completion.

    The alias's completion wrapper always invokes ``agent-circus`` directly
    (ignoring the ``$1`` bash normally passes), since ``$1`` is the literal
    typed word (the alias itself), which isn't a real executable on ``PATH``.

    :param alias: Alias name, e.g. ``ac``.
    :returns: Bash source defining the alias and its completion registration.
    :raises ValueError: If ``alias`` is not a plausible shell word.
    """
    if not _ALIAS_RE.match(alias):
        raise ValueError(f"{alias!r} is not a valid alias name")
    wrapper = f"_{alias}_{_PROG_NAME.replace('-', '_')}_completion"
    return (
        f"alias {alias}={_PROG_NAME}\n"
        f"{wrapper}() {{ {_completion_func_name()} {_PROG_NAME}; }}\n"
        f"complete -o default -F {wrapper} {alias}\n"
    )


def _bash_completion_path() -> Path:
    """Location bash-completion setups conventionally auto-source per user.

    :returns: Path to the agent-circus bash completion file.
    """
    return Path.home() / ".bash_completion.d" / _PROG_NAME


def completion(
    shell: Annotated[
        Shells,
        typer.Argument(help="Target shell."),
    ] = Shells.bash,
    install: Annotated[
        bool,
        typer.Option(
            "--install",
            help=(
                "Write the completion script to "
                "~/.bash_completion.d/agent-circus instead of printing it "
                "(bash only; created if missing, left untouched if already "
                "current)."
            ),
        ),
    ] = False,
    alias: Annotated[
        str | None,
        typer.Option(
            "--alias",
            help=(
                "Also define this shell alias for agent-circus and wire up "
                "its completion, e.g. --alias ac (bash only)."
            ),
        ),
    ] = None,
) -> None:
    """Print or install a shell completion script for agent-circus.

    Without ``--install``, the script is printed to stdout, meant to be
    sourced directly, e.g. ``source <(agent-circus completion bash)``,
    matching the ``cmd completion <shell>`` convention used by tools such as
    kubectl, gh, and k3d.

    With ``--install`` (bash only), the script is written to
    ``~/.bash_completion.d/agent-circus``, a directory many bash-completion
    setups auto-source, creating the directory and file if missing and
    leaving them untouched if already up to date.

    With ``--alias`` (bash only), an ``alias <name>=agent-circus`` definition
    and a matching completion registration for that alias are appended, so a
    single sourced/installed script gives you both the alias and its
    completion.

    :param shell: Target shell (bash, zsh, fish, powershell, or pwsh).
    :param install: Write to ``~/.bash_completion.d/agent-circus`` instead of stdout.
    :param alias: Also define and wire up completion for this alias name.
    :raises typer.Exit: If ``--install`` or ``--alias`` is used with a shell
        other than bash, or if ``--alias`` is not a valid alias name.
    """
    script = _completion_script(shell)

    if alias is not None:
        if shell != Shells.bash:
            typer.echo("Error: --alias is only supported for bash", err=True)
            raise typer.Exit(code=1)
        try:
            script += "\n" + _alias_snippet(alias)
        except ValueError as e:
            typer.echo(f"Error: {e}", err=True)
            raise typer.Exit(code=1) from e

    if not install:
        typer.echo(script)
        return

    if shell != Shells.bash:
        typer.echo("Error: --install is only supported for bash", err=True)
        raise typer.Exit(code=1)

    path = _bash_completion_path()
    if path.exists() and path.read_text() == script:
        typer.echo(f"{path} is already up to date")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(script)
    typer.echo(f"Installed completion script to {path}")
