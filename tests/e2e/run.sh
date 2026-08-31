#!/usr/bin/env bash
set -euo pipefail

E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$E2E_DIR/../.." && pwd)"
PROJECT="agentbox-e2e"

bash "$REPO_DIR/scripts/install-e2e-deps.sh"

if ! docker info >/dev/null 2>&1; then
  if [ -n "${AGENTBOX_E2E_GROUP-}" ] || ! command -v sg >/dev/null 2>&1; then
    echo "✖ this shell cannot reach the docker socket — open a new login shell and retry" >&2
    exit 1
  fi
  echo "→ re-running with the docker group active"
  export AGENTBOX_E2E_GROUP=1
  exec sg docker -c "$(printf '%q ' bash "${BASH_SOURCE[0]}" "$@")"
fi

cd "$E2E_DIR"

# shellcheck source=/dev/null
. ./.env

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "→ e2e run failed with status $status"
  fi
  docker compose -p "$PROJECT" down --remove-orphans
  exit "$status"
}

if [ "${1-}" = "--keep" ]; then
  echo "→ keeping the stack up after the run"
else
  trap cleanup EXIT
fi

fetch_lmstudio_model() {
  for attempt in 1 2 3 4 5 6; do
    if docker compose -p "$PROJECT" exec -T lmstudio lms get "$AGENTBOX_E2E_LMSTUDIO_SOURCE" --yes; then
      return 0
    fi
    echo "→ LM Studio not ready yet (attempt $attempt), retrying in 10s"
    sleep 10
  done
  echo "→ LM Studio never resolved $AGENTBOX_E2E_LMSTUDIO_SOURCE"
  return 1
}

docker compose -p "$PROJECT" build runner
docker compose -p "$PROJECT" up -d --wait ollama lmstudio
fetch_lmstudio_model
docker compose -p "$PROJECT" run --rm runner
