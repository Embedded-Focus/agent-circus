"""Compose context assembly for Agent Circus.

This module is the "glue" layer that loads configuration from all
sources, builds override strings, and assembles a
:class:`~agent_circus.compose.ComposeContext` ready to be passed
into the low-level compose functions.
"""

import contextlib
import logging
import os
from collections.abc import Iterator
from pathlib import Path

from .agent_config import build_agent_configs_override
from .compose import ComposeContext
from .config import (
    AVAILABLE_SERVICES,
    COMPOSE_FILE_NAME,
    build_agent_config_additions,
    build_shadow_override,
    load_config,
    resolve_config,
    sanitize_project_name,
)
from .mcp import build_compose_override as build_mcp_compose_override
from .state import get_agent_configs_dir
from .templates import template_dir_context

logger = logging.getLogger(__name__)


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
    agent_config_additions = build_agent_config_additions(config)

    # Build override strings (None when not needed).
    shadow_override = build_shadow_override(shadow) if shadow else None

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
        )
    else:
        # Instant mode: use bundled templates (temporary directory).
        with template_dir_context() as template_dir:
            env = os.environ.copy()
            env["AGENT_CIRCUS_WORKSPACE"] = str(workspace)
            yield ComposeContext(
                workspace=workspace,
                project_name=project_name,
                compose_file=template_dir / COMPOSE_FILE_NAME,
                cwd=template_dir,
                env=env,
                shadow_override=shadow_override,
                agent_configs_override=agent_configs_override,
                mcp_override=mcp_override,
            )
