#!/usr/bin/env bash
set -euo pipefail

: "${AGENTS:?the agentbox feature requires the 'agents' option}"

read -r -a packages <<<"$AGENTS"

if [ "${#packages[@]}" -eq 0 ]; then
  echo "→ no agents requested"
  exit 0
fi

echo "→ installing agents: ${packages[*]}"
npm install -g "${packages[@]}"
