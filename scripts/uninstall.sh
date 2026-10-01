#!/usr/bin/env bash
# Remove what scripts/install.sh and NIKA runs created: labs, Docker images,
# caches, host settings, Containerlab, and gnmic.
# Keeps Docker, uv, apt packages, the source tree, .env, config/, and results/.
# Usage: ./scripts/uninstall.sh [options]
set -euo pipefail

INOTIFY_CONF=/etc/sysctl.d/99-nika-inotify.conf
SUDOERS_FILE=/etc/sudoers.d/nika-host-tc
GNMIC_BIN=/usr/local/bin/gnmic

ASSUME_YES=0
PRUNE_BUILD_CACHE=0
ORIG_ARGS=("$@")

usage() {
  cat <<EOF
Usage: ./scripts/uninstall.sh [options]

Removes everything NIKA set up on this host:
  - running sessions, Kathara labs, Containerlab labs, the Batfish container
  - Docker images NIKA built or pulled (nika/*, kathara/*, k3s, srlinux,
    vendor router images, build parents, legacy tags) unless a non-NIKA
    container still uses them, and the Kathara Docker network plugin
  - the inotify sysctl file (original values restored) and ${SUDOERS_FILE}
  - Containerlab (package, /etc/containerlab, clab_admins group) and gnmic
  - ~/.config/kathara.conf and, in the repo, .venv, .nika_cache, runtime/

Keeps Docker and docker group membership, uv, apt packages (clang, iproute2,
skopeo), the source tree, .env, config/, and results/.

Options:
  -y, --yes             Do not ask for confirmation
  --prune-build-cache   Also run 'docker builder prune -af'; the build cache
                        is shared with non-NIKA builds on this host
  -h, --help            Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    --prune-build-cache) PRUNE_BUILD_CACHE=1; shift ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PATH="${HOME}/.local/bin:${PATH}"

log() { printf '+ %s\n' "$*"; }
warn() { printf '! %s\n' "$*" >&2; }

SUDO=""
if [[ "$(id -u)" -ne 0 ]]; then
  SUDO=sudo
fi

docker_usable() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

confirm() {
  if [[ "${ASSUME_YES}" -eq 1 ]]; then
    return
  fi
  if [[ ! -r /dev/tty ]]; then
    echo "error: non-interactive uninstall needs --yes" >&2
    exit 2
  fi
  printf 'Remove NIKA labs, images, caches, host settings, Containerlab and gnmic from this host? [y/N] ' >/dev/tty
  local reply
  read -r reply </dev/tty || reply=""
  [[ "${reply}" == [yY] || "${reply}" == [yY][eE][sS] ]] || { echo "Aborted."; exit 1; }
}

nika_cli_available() {
  [[ -x .venv/bin/nika ]] && command -v uv >/dev/null 2>&1
}

OWNED_IMAGES=()

stop_labs() {
  if nika_cli_available; then
    log "Closing NIKA sessions and wiping Kathara/Containerlab labs"
    uv run --no-sync nika session wipe -y || warn "nika session wipe failed; removing lab containers directly"
    local owned
    if owned="$(uv run --no-sync nika images list --owned)"; then
      mapfile -t OWNED_IMAGES <<<"${owned}"
    else
      warn "Could not list NIKA images; only nika/* and NIKA-labeled images will be removed"
    fi
  elif command -v clab >/dev/null 2>&1; then
    ${SUDO} clab destroy --all --cleanup --yes --log-level error || true
  fi
  docker_usable || return 0

  local ids
  ids="$(
    {
      docker ps -aq --filter label=app=kathara
      docker ps -aq --filter label=containerlab
      docker ps -aq --filter name=nika-batfish-
      docker images -q 'nika/*' | sort -u | while read -r image; do
        docker ps -aq --filter "ancestor=${image}"
      done
    } | sort -u
  )"
  if [[ -n "${ids}" ]]; then
    log "Removing leftover NIKA containers"
    # shellcheck disable=SC2086
    docker rm -f ${ids} >/dev/null
  fi
  local networks
  networks="$(docker network ls -q --filter label=app=kathara)"
  if [[ -n "${networks}" ]]; then
    # shellcheck disable=SC2086
    docker network rm ${networks} >/dev/null || warn "Some Kathara networks are still in use"
  fi
  docker network prune -f --filter label=containerlab >/dev/null || true
}

remove_images() {
  if ! docker_usable; then
    warn "Docker not usable; skipping image removal"
    return
  fi
  local refs
  mapfile -t refs < <(
    {
      printf '%s\n' "${OWNED_IMAGES[@]}"
      docker images --format '{{.Repository}}:{{.Tag}}' 'nika/*'
      docker images --format '{{.Repository}}:{{.Tag}}' 'kathara/nika-*'
      docker images -aq --no-trunc --filter "label=io.nika.image"
    } | sed '/^$/d; /<none>/d' | sort -u
  )
  # The classic builder keeps unlabeled step images as parents of each NIKA
  # build; walk those chains until an image tagged outside NIKA.
  local -A owned=()
  local ref parent tag foreign ancestors=()
  for ref in "${refs[@]}"; do owned[$ref]=1; done
  for ref in "${refs[@]}"; do
    parent="$(docker image inspect -f '{{.Parent}}' "${ref}" 2>/dev/null || true)"
    while [[ -n "${parent}" && -z "${owned[$parent]:-}" ]]; do
      foreign=0
      for tag in $(docker image inspect -f '{{join .RepoTags " "}}' "${parent}" 2>/dev/null || echo "?"); do
        [[ -n "${owned[$tag]:-}" ]] || foreign=1
      done
      [[ "${foreign}" -eq 0 ]] || break
      owned[$parent]=1
      ancestors+=("${parent}")
      parent="$(docker image inspect -f '{{.Parent}}' "${parent}" 2>/dev/null || true)"
    done
  done
  refs+=("${ancestors[@]}")
  log "Removing NIKA Docker images"
  local pass=0 removed kept
  # Later passes remove parents once their children are gone.
  while [[ ${#refs[@]} -gt 0 ]]; do
    pass=$((pass + 1))
    removed=0
    kept=()
    for ref in "${refs[@]}"; do
      docker image inspect "${ref}" >/dev/null 2>&1 || continue
      if docker image rm "${ref}" >/dev/null 2>&1; then
        removed=$((removed + 1))
      else
        kept+=("${ref}")
      fi
    done
    log "Pass ${pass}: removed ${removed} image reference(s)"
    refs=("${kept[@]}")
    [[ "${removed}" -gt 0 ]] || break
  done
  for ref in "${refs[@]}"; do
    warn "Kept ${ref} (used by a non-NIKA container or image)"
  done

  local plugin
  while read -r plugin; do
    [[ -n "${plugin}" ]] || continue
    log "Removing Docker plugin ${plugin}"
    docker plugin disable -f "${plugin}" >/dev/null 2>&1 || true
    docker plugin rm -f "${plugin}" >/dev/null || warn "Could not remove plugin ${plugin}"
  done < <(docker plugin ls --format '{{.Name}}' | grep '^kathara/katharanp' || true)

  if [[ "${PRUNE_BUILD_CACHE}" -eq 1 ]]; then
    log "Pruning Docker build cache"
    docker builder prune -af >/dev/null
  fi
}

restore_inotify() {
  [[ -f "${INOTIFY_CONF}" ]] || return 0
  local key original unknown=0
  local -A originals=()
  for key in max_user_instances max_user_watches; do
    originals[$key]="$(sed -n "s/^# nika-original fs\.inotify\.${key}=//p" "${INOTIFY_CONF}")"
  done
  ${SUDO} rm -f "${INOTIFY_CONF}"
  for key in max_user_instances max_user_watches; do
    original="${originals[$key]}"
    if [[ "${original}" =~ ^[0-9]+$ ]]; then
      ${SUDO} sysctl -qw "fs.inotify.${key}=${original}"
    else
      unknown=1
    fi
  done
  # Re-apply any other host sysctl files that set the same keys.
  ${SUDO} sysctl --system >/dev/null
  if [[ "${unknown}" -eq 1 ]]; then
    warn "Original inotify limits were not recorded (older installer); kernel defaults return after reboot"
  fi
  log "Removed ${INOTIFY_CONF} and restored inotify limits"
}

remove_host_tools() {
  if [[ -f "${SUDOERS_FILE}" ]]; then
    ${SUDO} rm -f "${SUDOERS_FILE}"
    log "Removed ${SUDOERS_FILE}"
  fi

  if command -v dpkg >/dev/null 2>&1 && dpkg -s containerlab >/dev/null 2>&1; then
    log "Removing Containerlab package"
    # /etc/containerlab keeps runtime files; it is removed below.
    ${SUDO} dpkg --purge containerlab 2>&1 >/dev/null | grep -v '/etc/containerlab' >&2 || true
    if dpkg -s containerlab >/dev/null 2>&1; then
      warn "dpkg could not remove containerlab"
    fi
  elif command -v rpm >/dev/null 2>&1 && rpm -q containerlab >/dev/null 2>&1; then
    log "Removing Containerlab package"
    ${SUDO} rpm -e containerlab
  elif command -v clab >/dev/null 2>&1; then
    warn "clab at $(command -v clab) was not installed from a package; remove it manually"
  fi
  if [[ -d /etc/containerlab ]]; then
    ${SUDO} rm -rf /etc/containerlab
  fi
  if getent group clab_admins >/dev/null 2>&1; then
    ${SUDO} groupdel clab_admins
    log "Removed clab_admins group"
  fi

  if [[ -f "${GNMIC_BIN}" ]]; then
    ${SUDO} rm -f "${GNMIC_BIN}"
    log "Removed ${GNMIC_BIN}"
  elif command -v gnmic >/dev/null 2>&1; then
    warn "gnmic at $(command -v gnmic) was not installed by NIKA; leaving it"
  fi
}

remove_files() {
  local kathara_conf="${HOME}/.config/kathara.conf"
  if [[ -f "${kathara_conf}" ]]; then
    rm -f "${kathara_conf}"
    log "Removed ${kathara_conf}"
  fi
  local path
  for path in .venv .nika_cache runtime; do
    if [[ -e "${path}" ]]; then
      # Lab runs can leave root-owned files under runtime/.
      rm -rf "${path}" 2>/dev/null || ${SUDO} rm -rf "${path}"
      log "Removed ${ROOT}/${path}"
    fi
  done
}

main() {
  if ! docker_usable && command -v sg >/dev/null 2>&1 \
    && [[ -z "${NIKA_UNINSTALL_UNDER_SG:-}" ]] \
    && sg docker -c 'docker info' >/dev/null 2>&1; then
    export NIKA_UNINSTALL_UNDER_SG=1
    exec sg docker -c "$(printf '%q ' "${ROOT}/scripts/uninstall.sh" "${ORIG_ARGS[@]}")"
  fi
  confirm
  stop_labs
  remove_images
  restore_inotify
  remove_host_tools
  remove_files
  log "NIKA removed. Kept: Docker, uv, apt packages, source tree, .env, config/, results/"
}

main
