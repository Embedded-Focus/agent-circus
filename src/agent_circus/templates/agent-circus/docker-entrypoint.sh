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

# Copy SSH config files from agent-circus intermediate mounts.
# Files are mounted read-only at /run/ssh-host/ and copied here so that
# /home/node/.ssh/ is owned by the container user (avoiding SSH ownership checks).
if [[ -s /run/ssh-host/config ]]; then
  mkdir -p /home/node/.ssh
  cp /run/ssh-host/config /home/node/.ssh/config
  chmod 600 /home/node/.ssh/config
fi
if [[ -s /run/ssh-host/known_hosts ]]; then
  mkdir -p /home/node/.ssh
  cp /run/ssh-host/known_hosts /home/node/.ssh/known_hosts
  chmod 600 /home/node/.ssh/known_hosts
fi

# Generate /home/node/.gitconfig from the host config via git's [include] mechanism.
# The include pulls in all host settings; subsequent directives override host-specific
# absolute paths (e.g. user.signingkey) with container-local equivalents.
if [[ -s /run/git-host/config ]]; then
  {
    printf '[include]\n\tpath = /run/git-host/config\n'
    if [[ -s /run/git-host/signingkey.pub ]]; then
      printf '[user]\n\tsigningkey = /run/git-host/signingkey.pub\n'
    fi
  } > /home/node/.gitconfig
fi

# Install forwarded CA certificates and refresh the system trust store.
if compgen -G "/run/ca-host/*.crt" > /dev/null 2>&1; then
  sudo /usr/local/bin/install-ca-certs.sh
fi

exec "${@}"
