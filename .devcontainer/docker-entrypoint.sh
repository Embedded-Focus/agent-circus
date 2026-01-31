#!/usr/bin/env bash

set -Eeuo pipefail

sudo AGENT_TYPE="${AGENT_TYPE:-unknown}" /usr/local/bin/init-firewall.sh

# This script must run and is run in the ${containerWorkspaceFolder} by default
# It installs the required libraries and other live dependencies that are not checked in.

if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

if [[ "${AGENT_TYPE:-unknown}" != "claude-code" ]]; then
    # firewall settings of the Claude Code service currently prohibit access to the PyPI
    uv sync
fi

exec "${@}"
