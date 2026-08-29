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

read -r -a bun_agents <<<"${AGENTBOX_BUN_AGENTS:-}"

if [ "${#bun_agents[@]}" -gt 0 ]; then
  export BUN_INSTALL="$HOME/.local"
  if [ ! -x "$BUN_INSTALL/bin/bun" ]; then
    echo "→ installing the bun runtime into $BUN_INSTALL/bin"
    curl -fsSL https://bun.sh/install | bash
  fi
  echo "→ installing bun agents: ${bun_agents[*]}"
  "$BUN_INSTALL/bin/bun" install -g "${bun_agents[@]}"
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
