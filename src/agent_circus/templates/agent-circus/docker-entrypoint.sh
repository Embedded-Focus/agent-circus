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

exec "${@}"
