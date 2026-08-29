#!/usr/bin/env bash
set -euo pipefail

STDLIB_PROBE="import json, unittest"

fail() {
  echo "✖ $1" >&2
  exit 1
}

as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  else
    sudo "$@"
  fi
}

if command -v python3 >/dev/null 2>&1 && python3 -c "$STDLIB_PROBE" >/dev/null 2>&1; then
  exit 0
fi

echo "→ the unit tests need python3 with its standard library"

if command -v apt-get >/dev/null 2>&1; then
  as_root apt-get update -qq
  as_root apt-get install -y -qq python3
elif command -v pacman >/dev/null 2>&1; then
  as_root pacman -S --needed --noconfirm python
else
  fail "install python3 yourself — no apt-get and no pacman on this system"
fi

python3 -c "$STDLIB_PROBE" >/dev/null 2>&1 ||
  fail "python3 is installed but its standard library is not importable"

echo "→ python3 is ready"
