#!/usr/bin/env bash
# Install Docker, uv, lab deps (Kathará), Containerlab, gnmic, and eBPF tools.
# Usage: ./scripts/install.sh
set -euo pipefail

GNMIC_VERSION="${GNMIC_VERSION:-0.48.0}"

usage() {
  cat <<'EOF'
Usage: ./scripts/install.sh

Installs Docker (if needed), uv, lab Python deps (Kathará), Containerlab,
gnmic, and eBPF tools (clang, iproute2 on Debian/Ubuntu). Creates .env and
config/nika.yaml from examples when missing.

  -h, --help    Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

log() { printf '+ %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

docker_usable() {
  docker info >/dev/null 2>&1
}

ensure_path_uv() {
  export PATH="${HOME}/.local/bin:${PATH}"
}

ensure_docker_group() {
  if [[ "$(id -u)" -eq 0 ]]; then
    return
  fi
  if getent group docker >/dev/null 2>&1; then
    sudo usermod -aG docker "$USER" || true
  fi
}

install_docker() {
  log "Installing Docker (get.docker.com)"
  need_cmd curl
  need_cmd sudo
  curl -fsSL https://get.docker.com | sudo sh
  if command -v systemctl >/dev/null 2>&1; then
    sudo systemctl enable --now docker >/dev/null 2>&1 || sudo systemctl start docker || true
  fi
  ensure_docker_group
  log "Added ${USER} to the docker group (re-login or 'newgrp docker' if docker still fails)"
  if docker_usable; then
    log "Docker is ready"
    return
  fi
  if command -v sg >/dev/null 2>&1 && sg docker -c 'docker info' >/dev/null 2>&1; then
    log "Docker is ready (via docker group)"
    return
  fi
  die "Docker installed but not usable yet. Run: newgrp docker   then re-run ./scripts/install.sh"
}

ensure_docker() {
  if docker_usable; then
    log "Docker already usable"
    return
  fi
  if command -v docker >/dev/null 2>&1; then
    log "Docker present but not usable; fixing group membership"
    need_cmd sudo
    ensure_docker_group
    if docker_usable; then
      log "Docker is ready"
      return
    fi
    if command -v sg >/dev/null 2>&1 && sg docker -c 'docker info' >/dev/null 2>&1; then
      log "Docker is ready (via docker group)"
      return
    fi
    die "Docker is installed but not usable in this shell. Run: newgrp docker"
  fi
  install_docker
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    log "uv already installed: $(command -v uv)"
    return
  fi
  log "Installing uv"
  need_cmd curl
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ensure_path_uv
  command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH; add ~/.local/bin to PATH"
  log "uv ready: $(command -v uv)"
}

sync_python() {
  ensure_path_uv
  need_cmd uv
  log "Syncing Python deps (uv sync --extra labs)"
  uv sync --extra labs
  log "Python lab deps ready (Kathará via PyPI)"
}

install_containerlab() {
  if command -v clab >/dev/null 2>&1; then
    log "Containerlab already installed: $(command -v clab)"
    return
  fi
  log "Installing Containerlab"
  need_cmd curl
  need_cmd bash
  bash -c "$(curl -fsSL https://get.containerlab.dev)"
  command -v clab >/dev/null 2>&1 || die "Containerlab install finished but 'clab' not found on PATH"
  log "Containerlab ready: $(command -v clab)"
}

install_gnmic() {
  if command -v gnmic >/dev/null 2>&1; then
    log "gnmic already installed: $(command -v gnmic)"
    return
  fi
  log "Installing gnmic ${GNMIC_VERSION}"
  need_cmd curl
  need_cmd bash
  curl -fsSL https://get-gnmic.openconfig.net | bash -s -- --version "${GNMIC_VERSION}"
  # Official installer may place the binary in /usr/local/bin
  if ! command -v gnmic >/dev/null 2>&1 && [[ -x /usr/local/bin/gnmic ]]; then
    export PATH="/usr/local/bin:${PATH}"
  fi
  command -v gnmic >/dev/null 2>&1 || die "gnmic install finished but 'gnmic' not found on PATH"
  log "gnmic ready: $(command -v gnmic)"
}

bootstrap_config() {
  if [[ ! -f .env ]]; then
    if [[ -f .env.example ]]; then
      cp .env.example .env
      log "Created .env from .env.example"
    else
      log "No .env.example found; skipping .env bootstrap"
    fi
  else
    log ".env already present"
  fi

  if [[ ! -f config/nika.yaml ]]; then
    if [[ -f config/nika.example.yaml ]]; then
      mkdir -p config
      cp config/nika.example.yaml config/nika.yaml
      log "Created config/nika.yaml from config/nika.example.yaml"
    else
      log "No config/nika.example.yaml found; skipping run-config bootstrap"
    fi
  else
    log "config/nika.yaml already present"
  fi
}

install_ebpf_tools() {
  if ! command -v apt-get >/dev/null 2>&1; then
    log "Skipping eBPF tools (clang, iproute2): apt-get not found; install them manually for device_forwarding_packet_corruption"
    return
  fi
  need_cmd sudo
  log "Installing eBPF build tools (clang, iproute2)"
  sudo apt-get update
  sudo apt-get install -y clang iproute2
}

print_next_steps() {
  cat <<'EOF'

Done.

1) Set a provider API key in .env (or export it):
     OPENAI_API_KEY=...

2) Run:
     uv run nika benchmark run --release 0.2.0 --split test --result_dir results/my-run
     # or: uv run nika agent run -a byo.langgraph -p openai -m gpt-5-mini --problem dc_clos_s_link_down

New Docker install: open a new shell or run newgrp docker.
Remote install: docs/operations/remote.md
Optional sbx: docs/operations/agent-sandbox.md
EOF
}

main() {
  log "NIKA install root: ${ROOT}"

  ensure_docker
  install_uv
  sync_python
  install_containerlab
  install_gnmic
  bootstrap_config
  install_ebpf_tools
  print_next_steps
}

main
