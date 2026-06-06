"""Compose context assembly for Agent Circus.

This module is the "glue" layer that loads configuration from all
sources, builds override strings, and assembles a
:class:`~agent_circus.compose.ComposeContext` ready to be passed
into the low-level compose functions.
"""

import contextlib
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

from .agent_config import build_agent_configs_override
from .compose import ComposeContext
from .config import (
    AVAILABLE_SERVICES,
    CA_CERTS_DEFAULT_DIR,
    COMPOSE_FILE_NAME,
    CONFIG_DIR_NAME,
    DOCKERFILE_NAME,
    HOOKS_DIR_NAME,
    build_additional_dirs_override,
    build_agent_config_additions,
    build_agent_config_mounts_override,
    build_ca_certs_override,
    build_data_store_override,
    build_env_dockerfile_lines,
    build_env_passthrough_override,
    build_env_profile_script,
    build_git_override,
    build_git_worktree_mirror_override,
    build_hosts_override,
    build_llama_cpp_override,
    build_port_forwards_override,
    build_shadow_override,
    build_ssh_override,
    build_startup_hook_override,
    filter_env,
    filter_hosts,
    get_claimed_agent_config_mounts,
    get_companion_services,
    load_config,
    match_files,
    parse_hosts_file,
    resolve_config,
    sanitize_project_name,
    write_hook_script,
)
from .exceptions import ConfigurationError
from .mcp import build_compose_override as build_mcp_compose_override
from .state import (
    get_agent_configs_dir,
    get_data_store_dir,
    get_startup_hook_path,
)
from .templates import template_dir_context

logger = logging.getLogger(__name__)

_HOOK_SCRIPTS = ("base-root.sh", "base-user.sh")
# Maps config.toml [hooks] keys to their build-context filenames.
# Only build-time hooks (baked into the image) are supported here;
# startup.sh is bind-mounted from the workspace at runtime and cannot
# be inlined in the build context.
_CONFIG_HOOK_MAP = {
    "base_root": "base-root.sh",
    "base_user": "base-user.sh",
}
# Inject ENV lines immediately before the base-stage ENTRYPOINT instruction.
_ENV_INJECTION_ANCHOR = "\nENTRYPOINT"
# Filename used inside the build-context hooks/ dir for the profile.d script.
_ENV_PROFILE_SCRIPT_NAME = "env-profile.sh"
# Path inside the container where the profile.d script is installed.
_ENV_PROFILE_D_PATH = "/etc/profile.d/agent-circus.sh"
_DATA_STORE_SEED_MARKER = ".agent-circus-seeded"


def _resolve_seed_source(seed_from: str) -> Path:
    """Resolve a data store seed source path for host-side copying.

    :param seed_from: Raw ``seed_from`` value from ``config.toml``.
    :returns: Absolute host path after environment interpolation.
    :raises ConfigurationError: If interpolation does not produce an absolute path.
    """
    source = Path(os.path.expandvars(seed_from))
    if not source.is_absolute():
        raise ConfigurationError(
            "Data store seed_from must resolve to an absolute host path"
        )
    return source


@contextlib.contextmanager
def _data_store_seed_lock(target: Path) -> Iterator[None]:
    """Serialize host-side seeding for one data store directory.

    :param target: Host data store directory being seeded.
    :yields: Once an exclusive lock is held.
    """
    lock_path = target.with_name(f"{target.name}.seed.lock")
    with lock_path.open("w") as lock_file:
        import fcntl

        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _remove_path(path: Path) -> None:
    """Remove a file, symlink, or directory before replacing it.

    :param path: Path to remove if it exists.
    """
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _path_exists(path: Path) -> bool:
    """Return whether a path exists, including broken symlinks.

    :param path: Path to check.
    :returns: ``True`` for existing paths and symlinks.
    """
    return path.exists() or path.is_symlink()


def _copy_seed_contents(source: Path, target: Path) -> None:
    """Copy the children of a seed directory into a data store directory.

    :param source: Host source directory.
    :param target: Host data store directory.
    """
    if not source.exists() or not source.is_dir():
        return

    for child in source.iterdir():
        destination = target / child.name
        if child.is_dir() and not child.is_symlink():
            if _path_exists(destination) and (
                not destination.is_dir() or destination.is_symlink()
            ):
                _remove_path(destination)
            shutil.copytree(child, destination, symlinks=True, dirs_exist_ok=True)
        else:
            if _path_exists(destination):
                _remove_path(destination)
            shutil.copy2(child, destination, follow_symlinks=False)


def _seed_data_stores(data_stores: list[dict], data_base_dir: Path) -> None:
    """Seed project data stores from host directories once.

    :param data_stores: Data store entries from ``config.toml``.
    :param data_base_dir: Base directory containing per-store host directories.
    """
    for entry in data_stores:
        seed_from = entry.get("seed_from")
        if not seed_from:
            continue

        name = entry["name"]
        source = _resolve_seed_source(seed_from)
        target = data_base_dir / name
        marker = target / _DATA_STORE_SEED_MARKER
        target.mkdir(parents=True, exist_ok=True)

        with _data_store_seed_lock(target):
            if marker.exists():
                continue

            _copy_seed_contents(source, target)
            marker.write_text(f"Agent Circus seeded this data store from {source}\n")


def _copy_project_hooks(workspace: Path, build_context: Path) -> None:
    """Copy project-level hook scripts into the Docker build context.

    Overwrites the empty placeholder scripts bundled with the template.
    Only scripts that exist in the project's hooks directory are copied;
    missing ones are left as the bundled placeholders so ``COPY`` never
    fails during ``docker build``.

    :param workspace: Project workspace root.
    :param build_context: Target build context directory (temp dir in instant mode).
    """
    hooks_src = workspace / CONFIG_DIR_NAME / HOOKS_DIR_NAME
    if not hooks_src.is_dir():
        return
    hooks_dst = build_context / HOOKS_DIR_NAME
    for hook_name in _HOOK_SCRIPTS:
        src = hooks_src / hook_name
        if src.is_file():
            shutil.copy2(src, hooks_dst / hook_name)


def _write_config_hooks(hooks_config: dict, build_context: Path) -> None:
    """Write inline hook scripts from config.toml into the Docker build context.

    Overwrites any scripts previously copied by :func:`_copy_project_hooks`.
    Only ``base_root`` and ``base_user`` are supported; ``startup`` is
    executed at runtime from ``/workspace/.agent-circus/hooks/startup.sh``
    and cannot be inlined into the build context.

    :param hooks_config: ``[hooks]`` table from merged config.
    :param build_context: Target build context directory.
    """
    hooks_dst = build_context / HOOKS_DIR_NAME
    for key, filename in _CONFIG_HOOK_MAP.items():
        content = hooks_config.get(key)
        if content is not None:
            write_hook_script(content, hooks_dst / filename)


def _inject_env_into_dockerfile(build_context: Path, env: dict[str, str]) -> None:
    """Inject ``ENV`` instructions into the Dockerfile in the build context.

    Inserts one ``ENV key=value`` line per entry immediately before the first
    ``ENTRYPOINT`` instruction (i.e. at the end of the ``base`` build stage).
    When *env* is empty the Dockerfile is left unchanged.

    Docker evaluates ``$VARNAME`` in ``ENV`` values against previously set
    ``ENV`` variables, so ``PATH=/usr/local/go/bin:$PATH`` correctly prepends
    to the image's existing PATH.

    Additionally writes a ``/etc/profile.d/agent-circus.sh`` script into the
    image.  This ensures the env vars are also visible to login shells (e.g.
    Codex command runner that uses ``/bin/bash -lc``).  Debian's
    ``/etc/profile`` resets ``PATH`` for all login shells, which would
    otherwise discard the Docker ``ENV`` layer; the ``profile.d`` script runs
    *after* ``/etc/profile`` and restores the configured values.

    :param build_context: Directory containing the Dockerfile to patch.
    :param env: Mapping of variable names to values to inject.
    """
    if not env:
        return
    dockerfile = build_context / DOCKERFILE_NAME
    text = dockerfile.read_text()

    # Write the profile.d script into the build context alongside the hooks.
    profile_src = build_context / HOOKS_DIR_NAME / _ENV_PROFILE_SCRIPT_NAME
    profile_src.parent.mkdir(parents=True, exist_ok=True)
    profile_src.write_text(build_env_profile_script(env))

    env_lines = build_env_dockerfile_lines(env)
    profile_install = (
        f"USER root\n"
        f"COPY hooks/{_ENV_PROFILE_SCRIPT_NAME} {_ENV_PROFILE_D_PATH}\n"
        f"RUN chmod 644 {_ENV_PROFILE_D_PATH}\n"
        f"USER node\n"
    )
    insertion = "\n".join(env_lines) + "\n" + profile_install + _ENV_INJECTION_ANCHOR
    text = text.replace(_ENV_INJECTION_ANCHOR, insertion, 1)
    dockerfile.write_text(text)


@contextlib.contextmanager
def build_compose_context(workspace: Path) -> Iterator[ComposeContext]:
    """Load configuration and assemble a :class:`ComposeContext`.

    Resolves deploy vs instant mode, loads the merged configuration,
    and builds all override strings.  Returns a context manager
    because instant mode relies on a temporary directory for the
    bundled template files.

    :param workspace: Workspace path.
    :yields: Fully assembled :class:`ComposeContext`.
    """
    config = load_config(workspace)
    shadow = config.get("shadow", [])
    mcp_servers = config.get("mcp_servers", [])
    env_vars: dict[str, str] = config.get("env", {})
    additional_dirs: list[dict] = config.get("additional_dirs", [])
    port_forwards: list[dict] = config.get("port_forwards", [])
    data_stores: list[dict] = config.get("data_stores", [])
    ssh_config = config.get("ssh")
    git_config = config.get("git")
    llama_cpp_config = config.get("llama_cpp")
    companion_services = tuple(get_companion_services(config))
    agent_config_additions = build_agent_config_additions(config)

    # Build override strings (None when not needed).
    shadow_override = build_shadow_override(shadow) if shadow else None
    additional_dirs_override = (
        build_additional_dirs_override(additional_dirs) if additional_dirs else None
    )
    port_forwards_override = (
        build_port_forwards_override(port_forwards) if port_forwards else None
    )
    llama_cpp_override = (
        build_llama_cpp_override(llama_cpp_config)
        if llama_cpp_config is not None
        else None
    )
    claimed_agent_config_mounts = get_claimed_agent_config_mounts(data_stores)
    agent_config_mounts_override = build_agent_config_mounts_override(
        claimed_agent_config_mounts
    )

    ssh_override: str | None = None
    if ssh_config is not None:
        if not os.environ.get("SSH_AUTH_SOCK"):
            raise ConfigurationError(
                "SSH agent forwarding requested but SSH_AUTH_SOCK is not set. "
                "Is ssh-agent running? Start one with: eval $(ssh-agent) && ssh-add"
            )
        config_path = ssh_config.get("config_path")
        known_hosts_path = ssh_config.get("known_hosts_path")
        if config_path:
            config_path = str(Path(config_path).expanduser())
        if known_hosts_path:
            known_hosts_path = str(Path(known_hosts_path).expanduser())
        ssh_override = build_ssh_override(
            config_path=config_path,
            known_hosts_path=known_hosts_path,
        )

    env_passthrough_override: str | None = None
    env_passthrough_patterns: list[str] = config.get("env_passthrough", [])
    if env_passthrough_patterns:
        matched_vars = filter_env(dict(os.environ), env_passthrough_patterns)
        if matched_vars:
            env_passthrough_override = build_env_passthrough_override(matched_vars)

    ca_certs_override: str | None = None
    ca_certs_config = config.get("ca_certs")
    if ca_certs_config is not None:
        certs_dir = ca_certs_config.get("path", CA_CERTS_DEFAULT_DIR)
        cert_patterns: list[str] = ca_certs_config.get("patterns", [])
        if cert_patterns:
            cert_paths = match_files(certs_dir, cert_patterns)
            if cert_paths:
                ca_certs_override = build_ca_certs_override(cert_paths)

    hosts_override: str | None = None
    hosts_config = config.get("hosts")
    if hosts_config is not None:
        hosts_file = hosts_config.get("file", "/etc/hosts")
        patterns: list[str] = hosts_config.get("patterns", [])
        if patterns:
            entries = parse_hosts_file(hosts_file)
            extra_hosts = filter_hosts(entries, patterns)
            if extra_hosts:
                hosts_override = build_hosts_override(extra_hosts)

    git_override: str | None = None
    if git_config is not None:
        git_config_path = str(
            Path(git_config.get("config_path", "~/.gitconfig")).expanduser()
        )
        git_signing_key = git_config.get("signing_key_path")
        if git_signing_key:
            git_signing_key = str(Path(git_signing_key).expanduser())
        git_override = build_git_override(git_config_path, git_signing_key)

    agent_configs_override: str | None = None
    if agent_config_additions and any(agent_config_additions.values()):
        configs_dir = get_agent_configs_dir(workspace)
        agent_configs_override = build_agent_configs_override(
            agent_config_additions, configs_dir
        )

    mcp_override: str | None = None
    if mcp_servers:
        mcp_override = build_mcp_compose_override(mcp_servers, AVAILABLE_SERVICES)

    project_name = sanitize_project_name(workspace.name)

    hooks_config = config.get("hooks")

    startup_hook_override: str | None = None
    if hooks_config and hooks_config.get("startup"):
        startup_path = get_startup_hook_path(workspace)
        write_hook_script(hooks_config["startup"], startup_path)
        startup_hook_override = build_startup_hook_override(startup_path)

    git_worktree_mirror_override: str | None = None
    if git_config and git_config.get("worktree_mirror"):
        git_worktree_mirror_override = build_git_worktree_mirror_override(workspace)

    data_store_override: str | None = None
    data_store_seeder: Callable[[], None] | None = None
    if data_stores:
        store_dirs = [get_data_store_dir(workspace, e["name"]) for e in data_stores]
        data_base_dir = store_dirs[0].parent
        data_store_override = build_data_store_override(data_stores, data_base_dir)

        def seed_data_stores() -> None:
            _seed_data_stores(data_stores, data_base_dir)

        data_store_seeder = seed_data_stores

    config_dir = resolve_config(workspace)

    if config_dir is not None:
        # Deploy mode: compose file lives in the workspace.
        if hooks_config is not None and any(
            k in hooks_config for k in ("base_root", "base_user")
        ):
            logger.warning(
                "config.toml [hooks].base_root / base_user is ignored in deploy mode — "
                "place hook scripts in .agent-circus/hooks/ directly."
            )
        yield ComposeContext(
            workspace=workspace,
            project_name=project_name,
            compose_file=config_dir / COMPOSE_FILE_NAME,
            cwd=config_dir,
            shadow_override=shadow_override,
            agent_config_mounts_override=agent_config_mounts_override,
            agent_configs_override=agent_configs_override,
            mcp_override=mcp_override,
            additional_dirs_override=additional_dirs_override,
            ssh_override=ssh_override,
            git_override=git_override,
            hosts_override=hosts_override,
            ca_certs_override=ca_certs_override,
            env_passthrough_override=env_passthrough_override,
            startup_hook_override=startup_hook_override,
            git_worktree_mirror_override=git_worktree_mirror_override,
            data_store_override=data_store_override,
            port_forwards_override=port_forwards_override,
            llama_cpp_override=llama_cpp_override,
            companion_services=companion_services,
            data_store_seeder=data_store_seeder,
        )
    else:
        # Instant mode: copy bundled templates into a fresh temp directory so
        # that project-specific mutations (hook scripts, ENV injection) never
        # touch the installed package files.
        with template_dir_context() as src_dir:
            with tempfile.TemporaryDirectory() as _tmp:
                build_context = Path(_tmp)
                shutil.copytree(
                    src_dir, build_context, symlinks=True, dirs_exist_ok=True
                )
                _copy_project_hooks(workspace, build_context)
                if hooks_config is not None:
                    _write_config_hooks(hooks_config, build_context)
                _inject_env_into_dockerfile(build_context, env_vars)
                env = os.environ.copy()
                env["AGENT_CIRCUS_WORKSPACE"] = str(workspace)
                yield ComposeContext(
                    workspace=workspace,
                    project_name=project_name,
                    compose_file=build_context / COMPOSE_FILE_NAME,
                    cwd=build_context,
                    env=env,
                    shadow_override=shadow_override,
                    agent_config_mounts_override=agent_config_mounts_override,
                    agent_configs_override=agent_configs_override,
                    mcp_override=mcp_override,
                    additional_dirs_override=additional_dirs_override,
                    ssh_override=ssh_override,
                    git_override=git_override,
                    hosts_override=hosts_override,
                    ca_certs_override=ca_certs_override,
                    env_passthrough_override=env_passthrough_override,
                    startup_hook_override=startup_hook_override,
                    git_worktree_mirror_override=git_worktree_mirror_override,
                    data_store_override=data_store_override,
                    port_forwards_override=port_forwards_override,
                    llama_cpp_override=llama_cpp_override,
                    data_store_seeder=data_store_seeder,
                )
