#!/usr/bin/env bash
set -euo pipefail

PACMAN_PACKAGES=(docker nodejs npm openssh)
AUR_PACKAGES=(sysbox-ce-bin)
SERVICES=(docker sysbox)
REQUIRED_BINARIES=(docker npx ssh-keygen)

fail() {
  echo "✖ $1" >&2
  exit 1
}

if ! command -v pacman >/dev/null 2>&1; then
  echo "→ not an Arch-based host, install these yourself:"
  echo "    ${REQUIRED_BINARIES[*]}"
  echo "    sysbox (https://github.com/nestybox/sysbox) for unprivileged nested Docker"
  exit 0
fi

missing_pacman=()
for package in "${PACMAN_PACKAGES[@]}"; do
  if ! pacman -Q "$package" >/dev/null 2>&1; then
    missing_pacman+=("$package")
  fi
done

if [ "${#missing_pacman[@]}" -gt 0 ]; then
  echo "→ installing ${missing_pacman[*]}"
  sudo pacman -S --needed "${missing_pacman[@]}"
fi

if ! command -v yay >/dev/null 2>&1; then
  fail "yay is required to install ${AUR_PACKAGES[*]} from the AUR"
fi

missing_aur=()
for package in "${AUR_PACKAGES[@]}"; do
  if ! pacman -Q "$package" >/dev/null 2>&1; then
    missing_aur+=("$package")
  fi
done

if [ "${#missing_aur[@]}" -gt 0 ]; then
  echo "→ installing ${missing_aur[*]} from the AUR"
  yay -S --needed "${missing_aur[@]}"
fi

for service in "${SERVICES[@]}"; do
  if ! systemctl is-active --quiet "$service"; then
    echo "→ enabling $service"
    sudo systemctl enable --now "$service"
  fi
done

for binary in "${REQUIRED_BINARIES[@]}"; do
  command -v "$binary" >/dev/null 2>&1 || fail "$binary is still missing after installation"
done

runtime_registered() {
  docker info --format '{{json .Runtimes}}' | grep -q sysbox-runc
}

register_runtime() {
  local binary
  binary="$(command -v sysbox-runc)" || fail "sysbox-runc is installed but not on PATH"
  echo "→ registering $binary in /etc/docker/daemon.json"
  sudo python3 - "$binary" <<'PYTHON'
import json
import sys
from pathlib import Path

path = Path("/etc/docker/daemon.json")
config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
config.setdefault("runtimes", {})["sysbox-runc"] = {"path": sys.argv[1]}
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
PYTHON
}

if ! runtime_registered; then
  register_runtime
  echo "→ restarting docker, running containers stop and have to be started again"
  sudo systemctl restart docker
fi

if runtime_registered; then
  echo "→ sysbox-runc is registered, boxes with nested Docker stay unprivileged"
else
  echo "⚠ sysbox-runc is still not registered with the docker daemon" >&2
  echo "  nested Docker will need --privileged until it is" >&2
fi
