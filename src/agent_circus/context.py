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
from collections.abc import Iterator
from pathlib import Path

from .agent_config import build_agent_configs_override
from .compose import ComposeContext
from .config import (
    AVAILABLE_SERVICES,
    COMPOSE_FILE_NAME,
    CONFIG_DIR_NAME,
    DOCKERFILE_NAME,
    HOOKS_DIR_NAME,
    build_additional_dirs_override,
    build_agent_config_additions,
    build_env_dockerfile_lines,
    build_shadow_override,
    load_config,
    resolve_config,
    sanitize_project_name,
)
from .mcp import build_compose_override as build_mcp_compose_override
from .state import get_agent_configs_dir
from .templates import template_dir_context

logger = logging.getLogger(__name__)

_HOOK_SCRIPTS = ("base-root.sh", "base-user.sh")
# Inject ENV lines immediately before the base-stage ENTRYPOINT instruction.
_ENV_INJECTION_ANCHOR = "\nENTRYPOINT"


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


def _inject_env_into_dockerfile(build_context: Path, env: dict[str, str]) -> None:
    """Inject ``ENV`` instructions into the Dockerfile in the build context.

    Inserts one ``ENV key=value`` line per entry immediately before the first
    ``ENTRYPOINT`` instruction (i.e. at the end of the ``base`` build stage).
    When *env* is empty the Dockerfile is left unchanged.

    Docker evaluates ``$VARNAME`` in ``ENV`` values against previously set
    ``ENV`` variables, so ``PATH=/usr/local/go/bin:$PATH`` correctly prepends
    to the image's existing PATH.

    :param build_context: Directory containing the Dockerfile to patch.
    :param env: Mapping of variable names to values to inject.
    """
    if not env:
        return
    dockerfile = build_context / DOCKERFILE_NAME
    text = dockerfile.read_text()
    lines = build_env_dockerfile_lines(env)
    insertion = "\n".join(lines) + _ENV_INJECTION_ANCHOR
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
    agent_config_additions = build_agent_config_additions(config)

    # Build override strings (None when not needed).
    shadow_override = build_shadow_override(shadow) if shadow else None
    additional_dirs_override = (
        build_additional_dirs_override(additional_dirs) if additional_dirs else None
    )

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

    config_dir = resolve_config(workspace)

    if config_dir is not None:
        # Deploy mode: compose file lives in the workspace.
        yield ComposeContext(
            workspace=workspace,
            project_name=project_name,
            compose_file=config_dir / COMPOSE_FILE_NAME,
            cwd=config_dir,
            shadow_override=shadow_override,
            agent_configs_override=agent_configs_override,
            mcp_override=mcp_override,
            additional_dirs_override=additional_dirs_override,
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
                    agent_configs_override=agent_configs_override,
                    mcp_override=mcp_override,
                    additional_dirs_override=additional_dirs_override,
                )
