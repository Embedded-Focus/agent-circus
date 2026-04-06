"""Initialize agent container configuration."""

import logging
from pathlib import Path
from typing import Annotated

import typer

from agent_circus.commands.up import up as up_command
from agent_circus.config import (
    CONFIG_DIR_NAME,
    config_exists,
    get_compose_file,
    get_config_dir,
    get_dockerfile,
    get_workspace_path,
    read_project_config,
    write_project_config,
)
from agent_circus.templates import deploy_templates

logger = logging.getLogger(__name__)


def init(
    workspace: Annotated[
        Path | None,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory path.",
            exists=True,
            file_okay=False,
            resolve_path=True,
        ),
    ] = None,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            "-c",
            help="Only check if configuration exists, don't create.",
        ),
    ] = False,
    deploy: Annotated[
        bool,
        typer.Option(
            "--deploy",
            "-d",
            help="Deploy template files to workspace.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            "-f",
            help="Overwrite existing files when deploying.",
        ),
    ] = False,
    up: Annotated[
        bool,
        typer.Option(
            "--up",
            "-u",
            help="Start containers after initialization.",
        ),
    ] = False,
    ssh: Annotated[
        bool,
        typer.Option(
            "--ssh",
            help="Enable SSH agent forwarding in config.toml.",
        ),
    ] = False,
    ssh_config: Annotated[
        Path | None,
        typer.Option(
            "--ssh-config",
            help="Path to SSH config file (implies --ssh).",
            exists=True,
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    ssh_known_hosts: Annotated[
        Path | None,
        typer.Option(
            "--ssh-known-hosts",
            help="Path to SSH known_hosts file (implies --ssh).",
            exists=True,
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    shadow: Annotated[
        list[str],
        typer.Option(
            "--shadow",
            help="Shadow a workspace-relative path with /dev/null (repeatable).",
        ),
    ] = [],
    git: Annotated[
        bool,
        typer.Option(
            "--git",
            help="Enable Git configuration forwarding in config.toml.",
        ),
    ] = False,
    git_config: Annotated[
        Path | None,
        typer.Option(
            "--git-config",
            help="Path to gitconfig file (implies --git, defaults to ~/.gitconfig).",
            exists=True,
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    git_signing_key: Annotated[
        Path | None,
        typer.Option(
            "--git-signing-key",
            help="Path to SSH signing public key file (implies --git).",
            exists=True,
            file_okay=True,
            dir_okay=False,
            resolve_path=True,
        ),
    ] = None,
    hosts_pattern: Annotated[
        list[str],
        typer.Option(
            "--hosts-pattern",
            help="Forward /etc/hosts entries matching this pattern (repeatable). "
            "Glob by default; prefix with 're:' for regex.",
        ),
    ] = [],
    ca_cert_pattern: Annotated[
        list[str],
        typer.Option(
            "--ca-cert-pattern",
            help="Forward CA certificates from /usr/local/share/ca-certificates "
            "whose filename matches this pattern (repeatable). "
            "Glob by default; prefix with 're:' for regex.",
        ),
    ] = [],
    env_pattern: Annotated[
        list[str],
        typer.Option(
            "--env-pattern",
            help="Forward host environment variables whose name matches this pattern "
            "(repeatable). Glob by default; prefix with 're:' for regex.",
        ),
    ] = [],
    git_worktree_mirror: Annotated[
        bool,
        typer.Option(
            "--git-worktree-mirror",
            help="Mount the workspace at its host path inside containers for "
            "transparent Git worktree support. Also writes a Git instructions "
            "section to CLAUDE.md.",
        ),
    ] = False,
) -> None:
    """Initialize or verify agent container configuration.

    Checks that the .agent-circus directory exists with the required
    configuration files (compose.yaml, Dockerfile).

    Use --check to verify configuration without making changes.
    Use --deploy to deploy template files to the workspace.
    Use --ssh / --ssh-config / --ssh-known-hosts / --shadow / --git /
    --git-config / --git-signing-key / --hosts-pattern / --ca-cert-pattern /
    --git-worktree-mirror to write configuration options to
    .agent-circus/config.toml without a full deploy.
    """
    workspace = workspace or get_workspace_path()

    _apply_config_options(
        workspace,
        ssh,
        ssh_config,
        ssh_known_hosts,
        shadow,
        git,
        git_config,
        git_signing_key,
        hosts_pattern,
        ca_cert_pattern,
        env_pattern,
        git_worktree_mirror,
    )

    if deploy:
        _deploy_templates(workspace, force)
    elif check:
        _check_config(workspace)
    elif not any(
        [
            ssh,
            ssh_config,
            ssh_known_hosts,
            shadow,
            git,
            git_config,
            git_signing_key,
            hosts_pattern,
            ca_cert_pattern,
            env_pattern,
            git_worktree_mirror,
        ]
    ):
        _init_config(workspace)

    if up:
        up_command(workspace=workspace)


def _apply_config_options(
    workspace: Path,
    ssh: bool,
    ssh_config: Path | None,
    ssh_known_hosts: Path | None,
    shadow: list[str],
    git: bool,
    git_config: Path | None,
    git_signing_key: Path | None,
    hosts_pattern: list[str],
    ca_cert_pattern: list[str],
    env_pattern: list[str],
    git_worktree_mirror: bool = False,
) -> None:
    """Write config options to .agent-circus/config.toml.

    Merges the provided options into the existing project-local config,
    creating the config directory and file if absent.  Returns immediately
    when no options are given.

    When *git_worktree_mirror* is ``True``, also writes a managed section to
    ``CLAUDE.md`` in the workspace instructing the agent to use the host path
    for Git operations.

    :param workspace: Workspace path.
    :param ssh: Enable SSH agent forwarding (adds ``[ssh]`` table).
    :param ssh_config: Host path for SSH config file, or ``None``.
    :param ssh_known_hosts: Host path for SSH known_hosts file, or ``None``.
    :param shadow: Workspace-relative paths to shadow with ``/dev/null``.
    :param git: Enable Git configuration forwarding (adds ``[git]`` table).
    :param git_config: Host path for gitconfig file, or ``None``.
    :param git_signing_key: Host path for SSH signing public key, or ``None``.
    :param hosts_pattern: Glob/regex patterns for forwarding ``/etc/hosts`` entries.
    :param ca_cert_pattern: Glob/regex patterns for forwarding CA certificate files.
    :param env_pattern: Glob/regex patterns for forwarding host environment variables.
    :param git_worktree_mirror: Mount workspace at its host path for Git worktrees.
    """
    want_ssh = ssh or ssh_config is not None or ssh_known_hosts is not None
    want_git = git or git_config is not None or git_signing_key is not None
    if (
        not want_ssh
        and not want_git
        and not shadow
        and not hosts_pattern
        and not ca_cert_pattern
        and not env_pattern
        and not git_worktree_mirror
    ):
        return

    config = read_project_config(workspace)

    if want_ssh:
        ssh_section: dict = config.get("ssh") or {}
        if ssh_config is not None:
            ssh_section["config_path"] = str(ssh_config)
        if ssh_known_hosts is not None:
            ssh_section["known_hosts_path"] = str(ssh_known_hosts)
        config["ssh"] = ssh_section

    if want_git:
        git_section: dict = config.get("git") or {}
        if git_config is not None:
            git_section["config_path"] = str(git_config)
        if git_signing_key is not None:
            git_section["signing_key_path"] = str(git_signing_key)
        config["git"] = git_section

    if shadow:
        existing: list[str] = config.get("shadow") or []
        merged = list(existing)
        for entry in shadow:
            if entry not in merged:
                merged.append(entry)
        config["shadow"] = merged

    if hosts_pattern:
        hosts_section: dict = config.get("hosts") or {}
        existing_patterns: list[str] = hosts_section.get("patterns") or []
        merged_patterns = list(existing_patterns)
        for p in hosts_pattern:
            if p not in merged_patterns:
                merged_patterns.append(p)
        hosts_section["patterns"] = merged_patterns
        config["hosts"] = hosts_section

    if ca_cert_pattern:
        ca_section: dict = config.get("ca_certs") or {}
        existing_ca_patterns: list[str] = ca_section.get("patterns") or []
        merged_ca_patterns = list(existing_ca_patterns)
        for p in ca_cert_pattern:
            if p not in merged_ca_patterns:
                merged_ca_patterns.append(p)
        ca_section["patterns"] = merged_ca_patterns
        config["ca_certs"] = ca_section

    if env_pattern:
        existing_env: list[str] = config.get("env_passthrough") or []
        merged_env = list(existing_env)
        for p in env_pattern:
            if p not in merged_env:
                merged_env.append(p)
        config["env_passthrough"] = merged_env

    if git_worktree_mirror:
        git_section = config.get("git") or {}
        git_section["worktree_mirror"] = True
        config["git"] = git_section
        _write_agents_md_git_worktree_section(workspace)

    write_project_config(workspace, config)
    typer.echo(
        f"Configuration written to {workspace / '.agent-circus' / 'config.toml'}"
    )


_AGENTS_MD_MARKER_START = "<!-- agent-circus: git-worktree-mirror -->"
_AGENTS_MD_MARKER_END = "<!-- /agent-circus: git-worktree-mirror -->"


def _write_agents_md_git_worktree_section(workspace: Path) -> None:
    """Write (or replace) the git-worktree-mirror section in ``AGENTS.md``.

    Inserts a managed block bounded by HTML comment markers so the section
    can be found and updated idempotently on subsequent ``init`` runs.
    If ``AGENTS.md`` does not exist it is created.

    :param workspace: Workspace path.
    """
    host_path = str(workspace)
    section = (
        f"{_AGENTS_MD_MARKER_START}\n"
        "## Git Operations\n\n"
        f"This repository is mounted at `{host_path}` inside the agent container "
        "in addition to the standard `/workspace` mount. "
        "When performing Git operations that involve absolute paths — such as "
        "`git worktree` commands — use `"
        f"{host_path}` rather than `/workspace` to ensure Git's recorded paths "
        "remain consistent with the host.\n"
        f"{_AGENTS_MD_MARKER_END}"
    )

    agents_md = workspace / "AGENTS.md"
    if agents_md.exists():
        text = agents_md.read_text()
        if _AGENTS_MD_MARKER_START in text:
            # Replace existing managed section.
            start = text.index(_AGENTS_MD_MARKER_START)
            end = text.index(_AGENTS_MD_MARKER_END) + len(_AGENTS_MD_MARKER_END)
            text = text[:start] + section + text[end:]
        else:
            text = text.rstrip("\n") + "\n\n" + section + "\n"
    else:
        text = section + "\n"

    agents_md.write_text(text)
    typer.echo(
        f"Git worktree instructions written to {agents_md.relative_to(workspace)}"
    )


def _deploy_templates(workspace: Path, force: bool) -> None:
    """Deploy template files to workspace.

    :param workspace: Workspace path.
    :type workspace: Path
    :param force: Overwrite existing files if True.
    :type force: bool
    """
    deployed = deploy_templates(workspace, force=force)

    if not deployed:
        typer.echo("No files deployed (all already exist). Use --force to overwrite.")
        return

    typer.echo(f"Deployed {len(deployed)} file(s) to {workspace}:")
    for path in deployed:
        typer.echo(f"  {path.relative_to(workspace)}")


def _check_config(workspace: Path) -> None:
    """Check if configuration exists and is valid.

    :param workspace: Workspace path.
    :type workspace: Path
    :raises typer.Exit: If configuration is missing or invalid.
    """
    config_dir = get_config_dir(workspace)
    compose_file = get_compose_file(workspace)
    dockerfile = get_dockerfile(workspace)

    errors = []

    if not config_dir.is_dir():
        errors.append(f"Configuration directory not found: {config_dir}")

    if not compose_file.is_file():
        errors.append(f"Compose file not found: {compose_file}")

    if not dockerfile.is_file():
        errors.append(f"Dockerfile not found: {dockerfile}")

    if errors:
        for error in errors:
            typer.echo(f"Error: {error}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"Configuration valid: {config_dir}")
    typer.echo(f"  Compose file: {compose_file.name}")
    typer.echo(f"  Dockerfile: {dockerfile.name}")


def _init_config(workspace: Path) -> None:
    """Initialize configuration if not present.

    :param workspace: Workspace path.
    :type workspace: Path
    """
    config_dir = get_config_dir(workspace)

    if config_exists(workspace):
        typer.echo(f"Configuration already exists: {config_dir}")
        typer.echo("Use 'agent-circus init --check' to verify configuration.")
        return

    if not config_dir.exists():
        typer.echo(
            f"Configuration directory '{CONFIG_DIR_NAME}' not found in {workspace}",
            err=True,
        )
        typer.echo(
            "\nTo set up agent-circus, create the configuration directory with:",
            err=True,
        )
        typer.echo(f"  mkdir {config_dir}", err=True)
        typer.echo(
            "\nThen add compose.yaml and Dockerfile. See documentation for examples.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Config dir exists but files are missing
    compose_file = get_compose_file(workspace)
    dockerfile = get_dockerfile(workspace)

    missing = []
    if not compose_file.is_file():
        missing.append("compose.yaml")
    if not dockerfile.is_file():
        missing.append("Dockerfile")

    if missing:
        typer.echo(
            f"Configuration directory exists but missing files: {', '.join(missing)}",
            err=True,
        )
        raise typer.Exit(code=1)
