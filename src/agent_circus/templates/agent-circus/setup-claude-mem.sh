#!/usr/bin/env bash

set -Eeuo pipefail

if [[ "${AGENT_CIRCUS_CLAUDE_MEM_ENABLED:-false}" != "true" ]]; then
  exit 0
fi

if [[ "${AGENT_TYPE:-}" != "claude-code" ]]; then
  exit 0
fi

# Claude-Mem is only baked into the image when it is enabled in the config at
# build time. Warn instead of failing so enabling without a rebuild never
# breaks container startup.
if ! command -v claude-mem >/dev/null 2>&1; then
  echo "warning: [claude_mem] is enabled but claude-mem is not installed in this image; run 'agent-circus build claude-code' and restart the container" >&2
  exit 0
fi

export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-/home/node/.claude}"
export CLAUDE_MEM_DATA_DIR="${CLAUDE_MEM_DATA_DIR:-/home/node/.claude-mem}"

mkdir -p "${CLAUDE_CONFIG_DIR}" "${CLAUDE_MEM_DATA_DIR}"
chmod 700 "${CLAUDE_CONFIG_DIR}" "${CLAUDE_MEM_DATA_DIR}" 2>/dev/null || true

plugin_json="${CLAUDE_CONFIG_DIR}/plugins/marketplaces/thedotmack/plugin/.claude-plugin/plugin.json"
if [[ ! -s "${plugin_json}" ]]; then
  claude-mem install --ide claude-code
fi

claude-mem status >/dev/null 2>&1 || claude-mem start
