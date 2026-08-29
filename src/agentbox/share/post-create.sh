#!/usr/bin/env bash
set -euo pipefail

: "${AGENTBOX_AGENTS:?set containerEnv.AGENTBOX_AGENTS in the devcontainer config}"

read -r -a agents <<<"$AGENTBOX_AGENTS"

if [ "${#agents[@]}" -eq 0 ]; then
  echo "→ no agents requested"
else
  echo "→ installing agents: ${agents[*]}"
  npm install -g "${agents[@]}"
fi

read -r -a skills <<<"${AGENTBOX_SKILLS:-}"

for repo in ${skills[@]+"${skills[@]}"}; do
  echo "→ installing skills from $repo"
  checkout="$(mktemp -d)"
  if git clone --quiet --depth 1 "$repo" "$checkout" &&
    TARGET="$HOME" bash "$checkout/scripts/install.sh"; then
    echo "→ skills from $repo are in ~/.claude/skills"
  else
    echo "⚠ skills from $repo were not installed — the box is up without them" >&2
  fi
  rm -rf "$checkout"
done
