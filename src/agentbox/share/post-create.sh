#!/usr/bin/env bash
set -euo pipefail

: "${AGENTBOX_AGENTS:?set containerEnv.AGENTBOX_AGENTS in the devcontainer config}"

read -r -a agents <<<"$AGENTBOX_AGENTS"

if [ "${#agents[@]}" -eq 0 ]; then
  echo "→ no agents requested"
  exit 0
fi

echo "→ installing agents: ${agents[*]}"
npm install -g "${agents[@]}"
