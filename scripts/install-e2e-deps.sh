#!/usr/bin/env bash
set -euo pipefail

DAEMON_LOG=/var/log/dockerd.log
DAEMON_TIMEOUT=30
DAEMON_CONFIG=/etc/docker/daemon.json

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

systemd_init() {
  [ "$(ps -p 1 -o comm=)" = "systemd" ]
}

compose_installed() {
  command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1
}

daemon_running() {
  as_root docker info >/dev/null 2>&1
}

# shellcheck source=/dev/null
install_apt() {
  local distro codename repo
  distro="$(. /etc/os-release && echo "${ID-}")"
  codename="$(. /etc/os-release && echo "${VERSION_CODENAME-}")"
  [ -n "$distro" ] || fail "no ID in /etc/os-release"
  [ -n "$codename" ] || fail "no VERSION_CODENAME in /etc/os-release"
  repo="https://download.docker.com/linux/$distro"

  as_root install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "$repo/gpg" |
    as_root gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
  as_root chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] $repo $codename stable" |
    as_root tee /etc/apt/sources.list.d/docker.list >/dev/null

  as_root apt-get update -qq
  DEBIAN_FRONTEND=noninteractive as_root apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

install_pacman() {
  as_root pacman -S --needed --noconfirm docker docker-compose
}

start_daemon() {
  if systemd_init; then
    as_root systemctl start docker
  else
    echo "→ no init system, starting dockerd itself — log in $DAEMON_LOG"
    as_root sh -c "nohup dockerd >$DAEMON_LOG 2>&1 &"
  fi

  for _ in $(seq "$DAEMON_TIMEOUT"); do
    daemon_running && return 0
    sleep 1
  done
  fail "dockerd did not come up within ${DAEMON_TIMEOUT}s — see $DAEMON_LOG"
}

daemon_gone() {
  ! as_root pgrep -x dockerd >/dev/null 2>&1
}

stop_daemon() {
  if systemd_init; then
    as_root systemctl stop docker
  else
    as_root pkill -x dockerd || true
  fi
  for _ in $(seq "$DAEMON_TIMEOUT"); do
    daemon_gone && return 0
    sleep 1
  done
  fail "dockerd is still running ${DAEMON_TIMEOUT}s after being asked to stop"
}

join_docker_group() {
  local user
  user="$(id -un)"
  [ "$(id -u)" -eq 0 ] && return 0
  getent group docker | grep -qw "$user" && return 0
  echo "→ adding $user to the docker group"
  as_root usermod -aG docker "$user"
}

uplink_mtu() {
  local iface
  iface="$(ip route show default | awk '{print $5; exit}')"
  [ -n "$iface" ] || fail "no default route — cannot read the uplink MTU"
  ip -o link show "$iface" | sed -nE 's/.* mtu ([0-9]+) .*/\1/p'
}

configured_mtu() {
  as_root python3 -c "
import json, pathlib
path = pathlib.Path('$DAEMON_CONFIG')
config = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
print(config.get('mtu', ''))
"
}

pin_mtu() {
  local mtu
  mtu="$(uplink_mtu)"
  [ -n "$mtu" ] || fail "could not read the uplink MTU"
  [ "$(configured_mtu)" = "$mtu" ] && return 1

  echo "→ pinning the docker MTU to $mtu, the uplink's"
  as_root python3 - "$mtu" <<'PYTHON'
import json
import sys
from pathlib import Path

path = Path("/etc/docker/daemon.json")
config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
config["mtu"] = int(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
PYTHON
  return 0
}

if ! compose_installed; then
  echo "→ the e2e suite needs docker with the compose plugin"
  if command -v apt-get >/dev/null 2>&1; then
    install_apt
  elif command -v pacman >/dev/null 2>&1; then
    install_pacman
  else
    fail "install docker and its compose plugin yourself — no apt-get and no pacman on this system"
  fi
  compose_installed || fail "docker is installed but 'docker compose' is not"
fi

join_docker_group

if pin_mtu && daemon_running; then
  echo "→ restarting docker to pick the MTU up"
  stop_daemon
fi

daemon_running || start_daemon

echo "→ docker is ready"
