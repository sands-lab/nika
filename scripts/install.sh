#!/usr/bin/env bash
# Install uv, lab deps (Kathará), Containerlab, and gnmic into the repo (.venv and
# .nika_cache) without apt, then build/pull every runtime image. Re-runs upgrade
# outdated parts and prune stale ones. Optionally switch git track, install sbx with
# KVM access, prepare vendor router images (RouterOS CHR, Cisco XRd), and set up
# the host for Firecracker benchmark execution.
# Usage: ./scripts/install.sh [options]
set -euo pipefail

GNMIC_VERSION="${GNMIC_VERSION:-0.48.0}"
# Installed when clab is missing or older than CLAB_MIN_VERSION; newer ones are kept.
CLAB_VERSION="${CLAB_VERSION:-0.79.0}"
CLAB_MIN_VERSION="${CLAB_MIN_VERSION:-0.79.0}"
INOTIFY_CONF=/etc/sysctl.d/99-nika-inotify.conf
NEIGH_CONF=/etc/sysctl.d/99-nika-neigh.conf
VRF_MODULES_CONF=/etc/modules-load.d/nika-vrf.conf

# Keep in sync with lab.py IMAGE constants.
ROUTEROS_VERSION="${ROUTEROS_VERSION:-7.21.5}"
ROUTEROS_IMAGE="vrnetlab/mikrotik_routeros:${ROUTEROS_VERSION}"
XRD_IMAGE_TAG="${XRD_IMAGE_TAG:-26.2.1}"
XRD_IMAGE="ios-xr/xrd-control-plane:${XRD_IMAGE_TAG}"
VRNETLAB_REPO_URL="${VRNETLAB_REPO_URL:-https://github.com/hellt/vrnetlab}"
CHR_BASE_URL="${CHR_BASE_URL:-https://download.mikrotik.com/routeros/${ROUTEROS_VERSION}}"

TRACK=""
WITH_VENDOR_IMAGES=0
WITH_SBX=0
WITH_AGENTENV=0
# AgentENV, the recommended sandbox runtime: an open-source E2B-compatible
# service. Pinned by digest so every host runs the same sandbox server.
AGENTENV_IMAGE="${AGENTENV_IMAGE:-ghcr.io/kvcache-ai/aenv-server@sha256:d1a656f3a1dea2464589ada0bc9f7223cff210cfc0865bd920d14f1bf5361978}"
AGENTENV_CONTAINER="${AGENTENV_CONTAINER:-nika-agentenv}"
# Host port of the AgentENV API; 8000 is left to local model servers (vLLM).
AGENTENV_PORT="${AGENTENV_PORT:-8800}"
UBLK_MODULES_CONF=/etc/modules-load.d/nika-agentenv-ublk.conf
UBLK_MODPROBE_CONF=/etc/modprobe.d/nika-agentenv-ublk.conf
WITH_FIRECRACKER=0
HUGEPAGES_CONF=/etc/sysctl.d/99-nika-hugepages.conf
USERFAULTFD_RULE=/etc/udev/rules.d/99-nika-userfaultfd.rules
# Host steps the installer cannot do itself, printed at the end.
MANUAL_STEPS=()
SKIP_IMAGES=0
XRD_TARBALL="${NIKA_XRD_TARBALL:-}"
ORIG_ARGS=("$@")

usage() {
  cat <<EOF
Usage: ./scripts/install.sh [options]

Needs Docker Engine and the host vrf kernel module, because some scenarios
(enterprise_branch, part of the benchmark) create Linux VRF devices; stops
with an error before changing anything when either is missing. Installs uv (when missing), lab Python deps
(Kathará), Containerlab, and gnmic without apt: Python deps go to .venv,
the clab and gnmic binaries to .venv/bin, and uv with its cache to
.nika_cache/. Containerlab needs sudo once for its setuid bit and the
clab_admins group. Raises and persists host inotify limits for k3s and XRd
labs and neighbor table limits for large or concurrent labs, and loads and persists the vrf kernel module. Creates .env
and config/nika.yaml from examples when missing, and enables the repo git
hooks that copy config/custom/ into new worktrees.

Then builds and pulls every Docker image the non-vendor scenarios use and
caches the Kubernetes workload images and Helm charts, so benchmarks start
without image builds. Re-running upgrades an outdated gnmic or Containerlab,
rebuilds nika/* images whose Dockerfiles changed, and removes images and
.nika_cache entries that older NIKA releases left behind.
Undo with ./scripts/uninstall.sh.

Options:
  --track stable|latest
      stable  switch to git branch main (published benchmarks)
      latest  switch to git branch dev (newest features)
      Omit to keep your current branch (default; safe for CI).

  --with-vendor-images
      After the base install, prepare images for vendor BGP labs:
        - RouterOS: download MikroTik CHR into .nika_cache/vendor/,
          clone hellt/vrnetlab there, and build ${ROUTEROS_IMAGE}
        - XRd: docker load a local Cisco tarball as ${XRD_IMAGE}
      Do nothing vendor-related unless you pass this flag.

  --xrd-tarball PATH
      Path to a Cisco XRd Control Plane container .tgz (no public URL).
      Or set NIKA_XRD_TARBALL, or put .nika_cache/vendor/xrd-*.tgz under
      the main NIKA checkout (shared by git worktrees).

  --with-agentenv
      Recommended. Every agent runs in a sandbox; this runs AgentENV, an
      open-source E2B-compatible sandbox service, as a Docker container
      (${AGENTENV_CONTAINER}, 127.0.0.1:${AGENTENV_PORT}) and points NIKA at it.
      Needs KVM (/dev/kvm) and kernel 6.8+; loads and persists the ublk_drv
      kernel module and runs the container privileged.

  --with-sbx
      Alternative sandbox runtime: install Docker Sandboxes (sbx) and give
      your user read/write access to /dev/kvm, which the sbx microVMs need.
      sbx ships only as a package, so this step uses apt. Run 'sbx login'
      afterwards.

  --with-firecracker
      Set up this host to run benchmark trials in Firecracker microVMs
      (docs/operations/firecracker.md) and select it in config/nika.yaml:
      KVM access, half of host memory as huge pages (reserved now and at
      boot), the userfaultfd device for the kvm group, a btrfs loop file of
      the free disk minus 20 GiB (60-150 GiB) mounted at runtime/firecracker
      (installs btrfs-progs with apt when mkfs.btrfs is missing), and the
      Kathara network plugin. Needs x86-64 and /dev/kvm. Prints what is left
      to do by hand, such as a reboot.

  --skip-images
      Skip building/pulling runtime images (labs then prepare them on first deploy).

  -h, --help        Show this help

Examples:
  ./scripts/install.sh
  ./scripts/install.sh --track stable
  ./scripts/install.sh --with-agentenv
  ./scripts/install.sh --with-sbx
  ./scripts/install.sh --with-agentenv --with-firecracker
  ./scripts/install.sh --track latest --with-vendor-images
  ./scripts/install.sh --with-vendor-images --xrd-tarball ~/xrd-control-plane-container-x86_64-26.2.1.tgz
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --track)
      [[ $# -ge 2 ]] || { echo "error: --track requires stable|latest" >&2; exit 2; }
      TRACK="$2"
      shift 2
      ;;
    --track=*)
      TRACK="${1#*=}"
      shift
      ;;
    --with-vendor-images) WITH_VENDOR_IMAGES=1; shift ;;
    --with-sbx) WITH_SBX=1; shift ;;
    --with-agentenv) WITH_AGENTENV=1; shift ;;
    --with-firecracker) WITH_FIRECRACKER=1; shift ;;
    --skip-images) SKIP_IMAGES=1; shift ;;
    --xrd-tarball)
      [[ $# -ge 2 ]] || { echo "error: --xrd-tarball requires a path" >&2; exit 2; }
      XRD_TARBALL="$2"
      shift 2
      ;;
    --xrd-tarball=*)
      XRD_TARBALL="${1#*=}"
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

case "${TRACK}" in
  ""|stable|latest) ;;
  *)
    echo "error: --track must be stable or latest (got ${TRACK})" >&2
    exit 2
    ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# runtime/ and .nika_cache/ live at the main checkout, shared by worktrees.
if common="$(git -C "${ROOT}" rev-parse --git-common-dir 2>/dev/null)" && [[ -n "${common}" ]]; then
  [[ "${common}" == /* ]] || common="${ROOT}/${common}"
  NIKA_STATE="$(cd "$(dirname -- "${common}")" && pwd -P)"
else
  NIKA_STATE="${ROOT}"
fi
NIKA_CACHE="${NIKA_STATE}/.nika_cache"
VENDOR_CACHE="${NIKA_VENDOR_CACHE:-${NIKA_CACHE}/vendor}"
# uv resolves a relative UV_PROJECT_ENVIRONMENT against the project root.
VENV_DIR="${UV_PROJECT_ENVIRONMENT:-.venv}"
[[ "${VENV_DIR}" == /* ]] || VENV_DIR="${ROOT}/${VENV_DIR}"
VENV_BIN="${VENV_DIR}/bin"
# Keep uv, its cache, and any uv-managed Python under the repo while this
# script runs; NIKA itself never calls uv.
export UV_CACHE_DIR="${UV_CACHE_DIR:-${NIKA_CACHE}/uv/cache}"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-${NIKA_CACHE}/uv/python}"
# Binaries in .venv/bin win over older host copies, as under 'uv run'. The
# trailing ~/.local/bin finds a uv that older installers put there.
export PATH="${VENV_BIN}:${NIKA_CACHE}/bin:${PATH}:${HOME}/.local/bin"

log() { printf '+ %s\n' "$*"; }
warn() { printf '! %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

docker_usable() {
  docker info >/dev/null 2>&1
}

docker_image_exists() {
  docker image inspect "$1" >/dev/null 2>&1
}
# version_lt A B: true when version A sorts before B.
version_lt() {
  [[ "$1" != "$2" && "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" == "$1" ]]
}

ensure_docker_group() {
  if [[ "$(id -u)" -eq 0 ]]; then
    return
  fi
  if getent group docker >/dev/null 2>&1; then
    sudo usermod -aG docker "$USER" || true
  fi
}

vrf_module_available() {
  # Loaded or built in, or present for modprobe; -n resolves without root.
  [[ -d /sys/module/vrf ]] || PATH="${PATH}:/usr/sbin:/sbin" modprobe -n vrf >/dev/null 2>&1
}

check_prerequisites() {
  # Fail before changing anything when a host prerequisite is missing.
  local missing=()
  if ! command -v docker >/dev/null 2>&1; then
    missing+=("Docker Engine: https://docs.docker.com/engine/install/ (for example: curl -fsSL https://get.docker.com | sudo sh)")
  fi
  if ! vrf_module_available; then
    missing+=("vrf kernel module, needed by scenarios with Linux VRF devices such as enterprise_branch in the benchmark (on Ubuntu: sudo apt-get install linux-modules-extra-\$(uname -r))")
  fi
  if [[ "${WITH_FIRECRACKER}" -eq 1 ]]; then
    if [[ "$(uname -m)" != x86_64 ]]; then
      missing+=("an x86-64 host, for --with-firecracker")
    fi
    if [[ ! -e /dev/kvm ]]; then
      missing+=("KVM (/dev/kvm), for --with-firecracker: enable virtualization in the BIOS, or nested virtualization when this host is a VM")
    fi
  fi
  [[ ${#missing[@]} -eq 0 ]] && return
  printf 'error: missing host prerequisites; install them, then re-run ./scripts/install.sh:\n' >&2
  printf '  - %s\n' "${missing[@]}" >&2
  exit 1
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
  die "Docker is required. Install Docker Engine (https://docs.docker.com/engine/install/), then re-run ./scripts/install.sh"
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    log "uv already installed: $(command -v uv)"
    return
  fi
  log "Installing uv into ${NIKA_CACHE}/bin"
  need_cmd curl
  # An unmanaged install writes no update receipt and leaves shell profiles alone.
  curl -LsSf https://astral.sh/uv/install.sh \
    | env UV_UNMANAGED_INSTALL="${NIKA_CACHE}/bin" sh
  command -v uv >/dev/null 2>&1 || die "uv installed but not found in ${NIKA_CACHE}/bin"
  log "uv ready: $(command -v uv)"
}

sync_python() {
  need_cmd uv
  log "Syncing Python deps (uv sync)"
  uv sync
  # 'source .venv/bin/activate' then also provides a uv this script installed.
  if [[ "$(command -v uv)" == "${NIKA_CACHE}/bin/uv" ]]; then
    ln -sfn "${NIKA_CACHE}/bin/uv" "${VENV_BIN}/uv"
  fi
  log "Python lab deps ready (Kathará via PyPI)"
}

# fetch_release URL ARCHIVE DEST: download a GitHub release archive into DEST
# and check it against the release's checksums.txt.
fetch_release() {
  local base="$1" archive="$2" dest="$3"
  need_cmd curl
  log "Downloading ${base}/${archive}"
  curl -fsSL -o "${dest}/${archive}" "${base}/${archive}"
  curl -fsSL -o "${dest}/checksums.txt" "${base}/checksums.txt"
  (cd "${dest}" && grep " ${archive}\$" checksums.txt | sha256sum -c --quiet -) \
    || die "checksum mismatch for ${archive}"
  tar -xzf "${dest}/${archive}" -C "${dest}"
}

install_containerlab() {
  if command -v clab >/dev/null 2>&1; then
    local current
    current="$(clab version 2>/dev/null | sed -n 's/^ *version: *//p' | head -n1)"
    if [[ -n "${current}" ]] && ! version_lt "${current}" "${CLAB_MIN_VERSION}"; then
      log "Containerlab ${current} already installed: $(command -v clab)"
      return
    fi
    log "Upgrading Containerlab ${current:-unknown} to ${CLAB_VERSION} in ${VENV_BIN}"
  else
    log "Installing Containerlab ${CLAB_VERSION} into ${VENV_BIN}"
  fi
  local sudo=""
  if [[ "$(id -u)" -ne 0 ]]; then
    need_cmd sudo
    sudo=sudo
  fi
  if findmnt -no OPTIONS -T "${VENV_BIN}" | tr , '\n' | grep -qx nosuid; then
    die "${VENV_BIN} is on a nosuid mount; Containerlab needs its setuid bit there"
  fi
  local tmp
  tmp="$(mktemp -d "${NIKA_CACHE}/clab.XXXXXX")"
  # shellcheck disable=SC2064
  trap "rm -rf '${tmp}'" EXIT
  fetch_release "https://github.com/srl-labs/containerlab/releases/download/v${CLAB_VERSION}" \
    "containerlab_${CLAB_VERSION}_linux_$(host_arch).tar.gz" "${tmp}"
  # clab runs as root through its setuid bit for members of clab_admins.
  # Network homes may refuse root-owned files (root_squash) or drop the bit.
  ${sudo} install -o root -g root -m 4755 "${tmp}/containerlab" "${VENV_BIN}/containerlab" \
    && [[ -u "${VENV_BIN}/containerlab" && "$(stat -c %u "${VENV_BIN}/containerlab")" == 0 ]] \
    || {
      # A leftover copy would shadow a system-wide clab on the next run.
      ${sudo} rm -f "${VENV_BIN}/containerlab" 2>/dev/null || true
      die "could not install a root-owned setuid clab in ${VENV_BIN} (network home?). Install Containerlab >= ${CLAB_MIN_VERSION} system-wide (https://containerlab.dev/install/), then re-run this script"
    }
  rm -rf "${tmp}"
  trap - EXIT
  ln -sfn containerlab "${VENV_BIN}/clab"
  if ! getent group clab_admins >/dev/null 2>&1; then
    ${sudo} groupadd -r clab_admins
  fi
  local user
  user="$(id -un)"
  if [[ "$(id -u)" -ne 0 ]] && ! id -nG "${user}" | tr ' ' '\n' | grep -qx clab_admins; then
    ${sudo} usermod -aG clab_admins "${user}"
    warn "Added ${user} to clab_admins; open a new login shell (or run 'newgrp clab_admins') before Containerlab labs"
  fi
  log "Containerlab ready: $(command -v clab)"
}

install_gnmic() {
  if command -v gnmic >/dev/null 2>&1; then
    local current
    current="$(gnmic version 2>/dev/null | sed -n 's/^ *version *: *//p' | head -n1)"
    if [[ "${current}" == "${GNMIC_VERSION}" ]]; then
      log "gnmic ${current} already installed: $(command -v gnmic)"
      return
    fi
    log "Replacing gnmic ${current:-unknown} with pinned ${GNMIC_VERSION} in ${VENV_BIN}"
  else
    log "Installing gnmic ${GNMIC_VERSION} into ${VENV_BIN}"
  fi
  local arch tmp
  case "$(host_arch)" in
    amd64) arch=x86_64 ;;
    arm64) arch=aarch64 ;;
  esac
  tmp="$(mktemp -d "${NIKA_CACHE}/gnmic.XXXXXX")"
  # shellcheck disable=SC2064
  trap "rm -rf '${tmp}'" EXIT
  fetch_release "https://github.com/openconfig/gnmic/releases/download/v${GNMIC_VERSION}" \
    "gnmic_${GNMIC_VERSION}_Linux_${arch}.tar.gz" "${tmp}"
  install -m 755 "${tmp}/gnmic" "${VENV_BIN}/gnmic"
  rm -rf "${tmp}"
  trap - EXIT
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

  # Checks commit messages and copies untracked config/custom/ files into new worktrees.
  local hooks_path
  git rev-parse --git-dir >/dev/null 2>&1 || return 0
  hooks_path="$(git config --get core.hooksPath || true)"
  if [[ -z "${hooks_path}" ]]; then
    git config core.hooksPath scripts/git-hooks
    log "Enabled repository git hooks (core.hooksPath=scripts/git-hooks)"
  elif [[ "${hooks_path}" != "scripts/git-hooks" ]]; then
    warn "core.hooksPath is ${hooks_path}; commit messages are not checked and config/custom/ is not copied into new worktrees"
  fi
}

check_host_tools() {
  # Containerlab link faults run the host's tc, ip, and nsenter through sudo.
  local cmd missing=()
  for cmd in tc ip nsenter; do
    command -v "${cmd}" >/dev/null 2>&1 || missing+=("${cmd}")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    warn "Containerlab link faults need ${missing[*]} on the host (iproute2, util-linux)"
  fi
}

checkout_track() {
  local branch remote_ref
  case "${TRACK}" in
    "") return ;;
    stable) branch="main" ;;
    latest) branch="dev" ;;
  esac
  remote_ref="origin/${branch}"

  need_cmd git
  [[ -d "${ROOT}/.git" ]] || die "--track requires a git checkout of the NIKA repository"

  if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
    die "working tree is dirty; commit or stash before --track ${TRACK}"
  fi

  log "Fetching remotes for track=${TRACK} (${remote_ref})"
  git fetch origin "${branch}"
  if git show-ref --verify --quiet "refs/remotes/${remote_ref}"; then
    git checkout -B "${branch}" "${remote_ref}"
  else
    die "remote ref ${remote_ref} not found after fetch"
  fi
  log "On branch $(git branch --show-current) @ $(git rev-parse --short HEAD)"
}

host_arch() {
  local m
  m="$(uname -m)"
  case "${m}" in
    x86_64|amd64) echo amd64 ;;
    aarch64|arm64) echo arm64 ;;
    *) die "unsupported host architecture: ${m}" ;;
  esac
}

download_file() {
  local url="$1" dest="$2"
  if [[ -f "${dest}" ]]; then
    log "Using cached $(basename "${dest}")"
    return
  fi
  need_cmd curl
  mkdir -p "$(dirname "${dest}")"
  log "Downloading ${url}"
  # download.mikrotik.com often resets mid-transfer; resume the partial file.
  local attempt
  for attempt in 1 2 3 4 5; do
    curl -fL -C - -o "${dest}.partial" "${url}" && break
    [[ "${attempt}" -lt 5 ]] || die "download failed after 5 attempts: ${url}"
    sleep 2
  done
  mv "${dest}.partial" "${dest}"
}

ensure_routeros_image() {
  if docker_image_exists "${ROUTEROS_IMAGE}"; then
    log "RouterOS image already present: ${ROUTEROS_IMAGE}"
    return
  fi

  need_cmd docker
  need_cmd git

  if [[ ! -e /dev/kvm ]]; then
    warn "/dev/kvm not found; RouterOS/vrnetlab boot will be slow or may fail without KVM"
  fi

  local arch chr_zip chr_url build_dir disk_name
  arch="$(host_arch)"
  mkdir -p "${VENDOR_CACHE}"
  if [[ "${arch}" == arm64 ]]; then
    disk_name="chr-${ROUTEROS_VERSION}-arm64.vdi"
    chr_zip="${VENDOR_CACHE}/${disk_name}.zip"
    chr_url="${CHR_BASE_URL}/${disk_name}.zip"
  else
    disk_name="chr-${ROUTEROS_VERSION}.vmdk"
    chr_zip="${VENDOR_CACHE}/${disk_name}.zip"
    chr_url="${CHR_BASE_URL}/${disk_name}.zip"
  fi

  download_file "${chr_url}" "${chr_zip}"

  build_dir="${VENDOR_CACHE}/vrnetlab"
  # The build rewrites a tracked Dockerfile, so refresh with a hard reset.
  if [[ -d "${build_dir}/.git" ]] \
    && git -C "${build_dir}" fetch --depth 1 origin \
    && git -C "${build_dir}" reset --hard FETCH_HEAD; then
    log "Refreshed vrnetlab clone at ${build_dir}"
  else
    log "Cloning ${VRNETLAB_REPO_URL}"
    rm -rf "${build_dir}"
    git clone --depth 1 "${VRNETLAB_REPO_URL}" "${build_dir}"
  fi

  local ros_dir="${build_dir}/mikrotik/routeros"
  [[ -d "${ros_dir}" ]] || die "vrnetlab checkout missing mikrotik/routeros"

  # The build context is vrnetlab's docker/ dir plus its common/ helpers and
  # the CHR disk, as vrnetlab's 'make docker-image' assembles it.
  local context="${ros_dir}/docker"
  # Drop disks a failed build or another RouterOS version left in the context.
  rm -f "${context}"/chr-*.vmdk "${context}"/chr-*.vdi
  cp "${build_dir}"/common/*.py "${context}/"
  "${VENV_BIN}/python" -m zipfile -e "${chr_zip}" "${context}"
  [[ -f "${context}/${disk_name}" ]] || die "expected ${disk_name} in ${chr_zip}"

  # vrnetlab-base already ships qemu, openssh-client, and sshpass (needed by
  # NIKA's docker-exec → SSH management path). Skip apt-get so builds work
  # when Docker DNS cannot reach deb.debian.org. The FROM pin must match
  # VRNETLAB_BASE_IMAGE in src/nika/workflows/setup/images.py.
  cat >"${ros_dir}/docker/Dockerfile" <<'DOCKERFILE'
FROM ghcr.io/srl-labs/vrnetlab-base:0.3.0@sha256:57f36ae1cf44a78a6b2cad35a6276565c56edfd28e8160ae9a772929db28fd6d
LABEL org.opencontainers.image.authors="roman@dodin.dev"

ARG IMAGE
COPY $IMAGE* /
COPY *.py /

EXPOSE 22 161/udp 830 5000 5678 8291 10000-10099
DOCKERFILE

  log "Building ${ROUTEROS_IMAGE} via vrnetlab (this may take a few minutes)"
  docker build --platform "linux/${arch}" --build-arg "IMAGE=${disk_name}" \
    -t "${ROUTEROS_IMAGE}-${arch}" -t "${ROUTEROS_IMAGE}" "${context}"
  rm -f "${context}/${disk_name}"
  docker_image_exists "${ROUTEROS_IMAGE}" || die "RouterOS build finished but ${ROUTEROS_IMAGE} was not found"
  log "RouterOS image ready: ${ROUTEROS_IMAGE}"
}

find_xrd_tarball() {
  if [[ -n "${XRD_TARBALL}" ]]; then
    [[ -f "${XRD_TARBALL}" ]] || die "XRd tarball not found: ${XRD_TARBALL}"
    printf '%s\n' "${XRD_TARBALL}"
    return
  fi
  local match
  shopt -s nullglob
  for match in "${VENDOR_CACHE}"/xrd-*.tgz "${VENDOR_CACHE}"/xrd-*.tar; do
    if [[ -f "${match}" ]]; then
      printf '%s\n' "${match}"
      return
    fi
  done
  shopt -u nullglob
  return 1
}

ensure_sysctl_floor() {
  # Usage: ensure_sysctl_floor CONF LABEL WHO key=min...
  # Raise each key to at least min and persist it in CONF. Never lower a
  # value the host already set higher. "# nika-original" lines keep the
  # pre-install values for scripts/uninstall.sh; a conf written by an older
  # installer has none, so its originals are unknown.
  local conf="$1" label="$2" who="$3"
  shift 3
  local pair key floor value original settings=""
  for pair in "$@"; do
    key="${pair%%=*}"
    floor="${pair#*=}"
    value="$(sysctl -n "${key}")"
    original="${value}"
    if [[ -f "${conf}" ]]; then
      original="$(sed -n "s/^# nika-original ${key//./\\.}=//p" "${conf}")"
      if [[ -z "${original}" ]]; then
        original=unknown
        log "Migrating ${conf} from an older installer (original ${key} unknown)"
      fi
    fi
    if (( value < floor )); then
      value="${floor}"
    fi
    settings="# nika-original ${key}=${original}"$'\n'"${settings}"
    settings+="${key}=${value}"$'\n'
  done

  local sudo=""
  if [[ "$(id -u)" -ne 0 ]]; then
    sudo=sudo
  fi
  if printf '%s' "${settings}" | ${sudo} tee "${conf}" >/dev/null \
    && ${sudo} sysctl -p "${conf}" >/dev/null; then
    log "Raised ${label} (persisted in ${conf})"
    return
  fi
  warn "Could not raise ${label}; ${who} need:"
  for pair in "$@"; do
    warn "  sudo sysctl -w ${pair}"
  done
}

ensure_host_limits() {
  # k3s (k8s_lab, llmd_lab) and Cisco XRd exhaust the kernel default of 128
  # inotify instances; every running container's shim also holds one.
  ensure_sysctl_floor "${INOTIFY_CONF}" "inotify limits" \
    "k8s_lab, llmd_lab, and iosxr_simple_bgp" \
    fs.inotify.max_user_instances=64000 fs.inotify.max_user_watches=64000
  # One kernel neighbor (ARP/NDP) table, with one gc_thresh limit, serves
  # every container network namespace. Large or concurrent labs overflow the
  # default of 1024 entries and lose packets ("neighbor table overflow" in
  # dmesg, ENOBUFS on send), which flaps BGP/IGP sessions in healthy labs.
  ensure_sysctl_floor "${NEIGH_CONF}" "neighbor table limits" \
    "large or concurrent labs" \
    net.ipv4.neigh.default.gc_thresh1=8192 \
    net.ipv4.neigh.default.gc_thresh2=16384 \
    net.ipv4.neigh.default.gc_thresh3=32768 \
    net.ipv6.neigh.default.gc_thresh1=8192 \
    net.ipv6.neigh.default.gc_thresh2=16384 \
    net.ipv6.neigh.default.gc_thresh3=32768
}

ensure_vrf_module() {
  # enterprise_branch Site Edges create Linux VRF devices; containers cannot
  # load modules, so the host must. Ubuntu cloud/virtual kernels ship vrf.ko
  # only in linux-modules-extra, which check_prerequisites requires up front.
  local conf="${VRF_MODULES_CONF}"
  local sudo=""
  if [[ "$(id -u)" -ne 0 ]]; then
    sudo=sudo
  fi
  ${sudo} modprobe vrf || die "could not load the vrf kernel module"
  printf 'vrf\n' | ${sudo} tee "${conf}" >/dev/null
  log "Loaded vrf kernel module (persisted in ${conf})"
}

install_sbx() {
  if command -v sbx >/dev/null 2>&1; then
    log "sbx already installed: $(sbx version 2>/dev/null | head -n1)"
    return
  fi
  need_cmd sudo
  # docker-sbx ships in Docker's apt repo, which get.docker.com configures.
  if command -v apt-get >/dev/null 2>&1 \
    && [[ "$(apt-cache policy docker-sbx 2>/dev/null | sed -n 's/^ *Candidate: *//p')" =~ ^[0-9] ]]; then
    log "Installing docker-sbx"
    sudo apt-get install -y docker-sbx
  else
    log "Installing sbx (get.docker.com with SBX=1)"
    need_cmd curl
    curl -fsSL https://get.docker.com | sudo SBX=1 sh
  fi
  command -v sbx >/dev/null 2>&1 || die "sbx install finished but 'sbx' not found on PATH"
  log "sbx ready: $(command -v sbx)"
}

ensure_kvm_access() {
  # sbx sandboxes and Firecracker labs are KVM microVMs; without read/write on
  # /dev/kvm, 'sbx create' fails with "KVM error: Permission denied".
  if [[ ! -e /dev/kvm ]]; then
    warn "/dev/kvm not found; sbx needs KVM (enable hardware or nested virtualization)"
    return
  fi
  if [[ -r /dev/kvm && -w /dev/kvm ]]; then
    log "/dev/kvm is accessible"
    return
  fi
  local group
  group="$(stat -c %G /dev/kvm)"
  if [[ "${group}" == root ]]; then
    MANUAL_STEPS+=("Grant ${USER} read/write on /dev/kvm (owned by group root)")
    return
  fi
  if ! id -nG "${USER}" | tr ' ' '\n' | grep -qx "${group}"; then
    need_cmd sudo
    sudo usermod -aG "${group}" "${USER}"
    log "Added ${USER} to the ${group} group for /dev/kvm"
  fi
  local step="Open a new login shell (or run 'newgrp ${group}') to use /dev/kvm"
  if [[ "${WITH_SBX}" -eq 1 ]]; then
    step+=", then run 'sbx daemon stop' so the next NIKA run starts sandboxd with the new group"
  fi
  MANUAL_STEPS+=("${step}")
}

ensure_ublk_module() {
  # AgentENV serves sandbox disks through ublk; containers cannot load modules.
  local sudo=""
  if [[ "$(id -u)" -ne 0 ]]; then
    need_cmd sudo
    sudo=sudo
  fi
  printf 'options ublk_drv ublks_max=4096\n' | ${sudo} tee "${UBLK_MODPROBE_CONF}" >/dev/null
  if [[ ! -d /sys/module/ublk_drv ]]; then
    ${sudo} modprobe ublk_drv ublks_max=4096 \
      || die "could not load ublk_drv; AgentENV needs kernel 6.8+ (linux-modules-extra on Ubuntu)"
  fi
  printf 'ublk_drv\n' | ${sudo} tee "${UBLK_MODULES_CONF}" >/dev/null
  log "Loaded ublk_drv kernel module (persisted in ${UBLK_MODULES_CONF})"
}

install_agentenv() {
  [[ -e /dev/kvm ]] || die "AgentENV needs KVM, but /dev/kvm is missing; enable hardware or nested virtualization, or use E2B cloud (docs/operations/agent-sandbox.md)"
  ensure_ublk_module
  local current
  current="$(docker inspect -f '{{.Config.Image}}' "${AGENTENV_CONTAINER}" 2>/dev/null || true)"
  if [[ -n "${current}" && "${current}" != "${AGENTENV_IMAGE}" ]]; then
    log "Replacing AgentENV container ${AGENTENV_CONTAINER} (image ${current})"
    docker rm -f "${AGENTENV_CONTAINER}" >/dev/null
    current=""
  fi
  if [[ -z "${current}" ]]; then
    if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:${AGENTENV_PORT}/"; then
      die "port ${AGENTENV_PORT} is in use; rerun with AGENTENV_PORT=<free port>"
    fi
    log "Starting AgentENV (${AGENTENV_IMAGE})"
    docker run -d --name "${AGENTENV_CONTAINER}" --restart unless-stopped \
      --privileged -v /dev:/dev -p "127.0.0.1:${AGENTENV_PORT}:8000" \
      "${AGENTENV_IMAGE}" >/dev/null
  else
    docker start "${AGENTENV_CONTAINER}" >/dev/null
    log "AgentENV container ${AGENTENV_CONTAINER} already present"
  fi
  local key="" healthy=0 attempt
  for attempt in $(seq 1 60); do
    key="$(docker exec "${AGENTENV_CONTAINER}" cat /workspace/env/secrets/api-key 2>/dev/null || true)"
    if [[ -n "${key}" ]] && curl -fsS -o /dev/null "http://127.0.0.1:${AGENTENV_PORT}/health" 2>/dev/null; then
      healthy=1
      break
    fi
    sleep 2
  done
  [[ "${healthy}" -eq 1 ]] || die "AgentENV did not start; see: docker logs ${AGENTENV_CONTAINER}"
  # The key lives in the repository-root .env with the other credentials.
  if grep -q '^E2B_API_KEY=' .env 2>/dev/null; then
    sed -i "s|^E2B_API_KEY=.*|E2B_API_KEY=${key}|" .env
  else
    printf '\n# AgentENV sandbox service (scripts/install.sh --with-agentenv)\nE2B_API_KEY=%s\n' "${key}" >> .env
  fi
  uv run nika config set nika.sandbox.backend=e2b \
    "nika.sandbox.url=http://127.0.0.1:${AGENTENV_PORT}" >/dev/null
  log "AgentENV ready at http://127.0.0.1:${AGENTENV_PORT}; config/nika.yaml now uses it"
}

ensure_xrd_image() {
  if docker_image_exists "${XRD_IMAGE}"; then
    log "XRd image already present: ${XRD_IMAGE}"
    return
  fi

  local tarball
  if ! tarball="$(find_xrd_tarball)"; then
    if ! prompt_for_xrd_tarball; then
      warn "Skipping ${XRD_IMAGE}; iosxr_simple_bgp will fail until you load an XRd tarball"
      return
    fi
    tarball="$(find_xrd_tarball)" \
      || die "XRd tarball still not found after download prompt"
  fi

  need_cmd docker
  local load_out loaded inner unpack_dir=""
  # Cisco's CCO download wraps the loadable image with signing files.
  inner="$(tar -tf "${tarball}" 2>/dev/null | grep -m1 -E '\.dockerv1\.(tgz|tar)$' || true)"
  if [[ -n "${inner}" ]]; then
    unpack_dir="$(mktemp -d "${VENDOR_CACHE}/xrd-unpack.XXXXXX")"
    log "Extracting ${inner} from ${tarball}"
    tar -xf "${tarball}" -C "${unpack_dir}" "${inner}"
    tarball="${unpack_dir}/${inner}"
  fi
  log "Loading XRd tarball: ${tarball}"
  load_out="$(docker load -i "${tarball}")" || load_out=""
  [[ -z "${unpack_dir}" ]] || rm -rf "${unpack_dir}"
  printf '%s\n' "${load_out}"

  loaded="$(printf '%s\n' "${load_out}" | sed -n 's/^Loaded image: //p' | tail -n1)"
  if [[ -z "${loaded}" ]]; then
    die "docker load did not report a 'Loaded image:' line; tag ${XRD_IMAGE} manually"
  fi
  if [[ "${loaded}" != "${XRD_IMAGE}" ]]; then
    docker tag "${loaded}" "${XRD_IMAGE}"
    log "Tagged ${loaded} -> ${XRD_IMAGE}"
  fi
  docker_image_exists "${XRD_IMAGE}" || die "failed to create ${XRD_IMAGE}"
  log "XRd image ready: ${XRD_IMAGE}"
}

# Returns 0 once a tarball is present, 1 if the user skips or stdin is not a TTY.
prompt_for_xrd_tarball() {
  warn "XRd tarball not found (needed for ${XRD_IMAGE})."
  warn "Cisco does not publish a public download URL. Download an XRd Control"
  warn "Plane container .tgz from CCO / Cisco Modeling Labs"
  warn "(filename like xrd-control-plane-container-x86_64-<version>.tgz), then:"
  warn "  - place it at ${VENDOR_CACHE}/xrd-*.tgz, or"
  warn "  - pass --xrd-tarball /path/to/file.tgz, or"
  warn "  - set NIKA_XRD_TARBALL=/path/to/file.tgz"

  if [[ ! -r /dev/tty ]]; then
    warn "Non-interactive install: continuing without XRd"
    return 1
  fi

  local reply
  while true; do
    printf '%s' \
      "Place the downloaded tarball, then press Enter to continue (or 's' + Enter to skip XRd): " \
      >/dev/tty
    if ! read -r reply </dev/tty; then
      return 1
    fi
    case "${reply}" in
      s|S|skip|SKIP)
        return 1
        ;;
      *)
        if find_xrd_tarball >/dev/null; then
          log "Found XRd tarball"
          return 0
        fi
        warn "Still not found under ${VENDOR_CACHE}/xrd-*.tgz (or --xrd-tarball / NIKA_XRD_TARBALL)"
        ;;
    esac
  done
}

# Firecracker host setup (docs/operations/firecracker.md). Set by setup_firecracker.
FC_SUDO=""

ensure_nested_virt() {
  # Labs that run VMs themselves (vrnetlab RouterOS) need nested KVM.
  local param value
  for param in /sys/module/kvm_amd/parameters/nested /sys/module/kvm_intel/parameters/nested; do
    [[ -r "${param}" ]] || continue
    value="$(cat "${param}")"
    if [[ "${value}" == [1Y] ]]; then
      log "KVM nested virtualization is on"
    else
      MANUAL_STEPS+=("For RouterOS labs in Firecracker, turn on KVM nested virtualization (${param} is ${value})")
    fi
    return
  done
}

ensure_userfaultfd() {
  # The page server that restores copies onto huge pages opens /dev/userfaultfd.
  getent group kvm >/dev/null 2>&1 || die "group kvm not found; Firecracker needs /dev/kvm owned by group kvm"
  printf 'KERNEL=="userfaultfd", GROUP="kvm", MODE="0660"\n' \
    | ${FC_SUDO} tee "${USERFAULTFD_RULE}" >/dev/null
  if [[ ! -e /dev/userfaultfd ]]; then
    MANUAL_STEPS+=("Firecracker restores need /dev/userfaultfd, which kernel $(uname -r) lacks: use Linux 6.1 or newer")
    return
  fi
  ${FC_SUDO} udevadm control --reload-rules
  ${FC_SUDO} udevadm trigger --name-match=userfaultfd
  ${FC_SUDO} udevadm settle 2>/dev/null || true
  if [[ "$(stat -c %G /dev/userfaultfd)" == kvm ]]; then
    log "/dev/userfaultfd belongs to group kvm (persisted in ${USERFAULTFD_RULE})"
  else
    MANUAL_STEPS+=("Give group kvm read/write on /dev/userfaultfd: the udev rule ${USERFAULTFD_RULE} did not apply")
  fi
}

ensure_hugepages() {
  # Each running copy holds its VM memory in 2 MiB huge pages; reserved memory
  # is unavailable to everything else. "# nika-original" keeps the
  # pre-install value for scripts/uninstall.sh.
  local total_kib pages current original reserved
  total_kib="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)"
  pages=$(( total_kib / 2 / 2048 ))  # 2 MiB pages
  current="$(sysctl -n vm.nr_hugepages)"
  original="${current}"
  if [[ -f "${HUGEPAGES_CONF}" ]]; then
    original="$(sed -n 's/^# nika-original vm\.nr_hugepages=//p' "${HUGEPAGES_CONF}")"
  fi
  # Keep a larger reservation the host already has.
  if (( current > pages )); then
    pages="${current}"
  fi
  printf '# nika-original vm.nr_hugepages=%s\nvm.nr_hugepages=%s\n' "${original}" "${pages}" \
    | ${FC_SUDO} tee "${HUGEPAGES_CONF}" >/dev/null
  if (( current != pages )); then
    # Free the page cache and compact memory so that enough contiguous 2 MiB
    # blocks exist without a reboot.
    sync
    ${FC_SUDO} sh -c 'echo 3 > /proc/sys/vm/drop_caches; echo 1 > /proc/sys/vm/compact_memory'
    ${FC_SUDO} sysctl -qw "vm.nr_hugepages=${pages}"
  fi
  reserved="$(awk '/^HugePages_Total:/ {print $2}' /proc/meminfo)"
  if (( reserved < pages )); then
    MANUAL_STEPS+=("Reboot to reserve all huge pages: only $(( reserved / 512 )) of $(( pages / 512 )) GiB fit into memory now; ${HUGEPAGES_CONF} reserves them at boot")
  else
    log "Reserved $(( pages / 512 )) GiB of huge pages (persisted in ${HUGEPAGES_CONF})"
  fi
  if (( pages < 20 * 512 )); then
    warn "$(( pages / 512 )) GiB of huge pages cannot hold a 20 GiB VM (isp_pdh, isp_di-yuan, isp_dfn-gwin)"
  fi
}

ensure_firecracker_root() {
  # Trial disks are reflink clones of image disks, so the root must be on btrfs
  # or XFS. The default root gets a btrfs loop file in runtime/, which
  # scripts/uninstall.sh unmounts and deletes.
  local root fstype image="${ROOT}/runtime/firecracker.img"
  root="$(uv run --no-sync python -c \
    'from nika.firecracker.config import firecracker_settings; print(firecracker_settings().root)')"
  [[ "${root}" == /* ]] || root="${ROOT}/${root}"
  mkdir -p "${root}"
  fstype="$(findmnt -no FSTYPE -T "${root}")"
  if [[ "${fstype}" == btrfs || "${fstype}" == xfs ]]; then
    log "Firecracker root ${root} is on ${fstype}"
    return
  fi
  [[ "${root}" == "${ROOT}/runtime/firecracker" ]] \
    || die "Firecracker root ${root} is on ${fstype}; nika.execution.firecracker.root needs btrfs or XFS"
  [[ "${root}" != *[[:space:]]* ]] || die "cannot mount ${root} from /etc/fstab: its path contains whitespace"
  [[ -z "$(ls -A "${root}")" ]] \
    || die "${root} holds files on ${fstype}; delete them ('uv run nika firecracker clean --all', then the directory) and re-run"
  if [[ ! -f "${image}" ]]; then
    if ! command -v mkfs.btrfs >/dev/null 2>&1; then
      command -v apt-get >/dev/null 2>&1 \
        || die "mkfs.btrfs not found; install btrfs-progs, then re-run ./scripts/install.sh --with-firecracker"
      log "Installing btrfs-progs"
      ${FC_SUDO} apt-get install -y btrfs-progs >/dev/null
    fi
    local avail size
    avail="$(df --output=avail -BG "${ROOT}/runtime" | tail -n1 | tr -dc 0-9)"
    # Leave room for Docker images: a loop file that outgrows the free disk
    # fails with I/O errors.
    size=$(( avail - 20 < 150 ? avail - 20 : 150 ))
    (( size >= 60 )) \
      || die "the Firecracker root needs 60 GiB, but the disk holding ${ROOT}/runtime has only ${avail} GiB free (20 GiB stay free); free disk space, or set nika.execution.firecracker.root to a directory on btrfs or XFS"
    log "Creating a ${size} GiB btrfs loop file at ${image}"
    truncate -s "${size}G" "${image}"
    mkfs.btrfs -q "${image}"
  fi
  if ! grep -qF " ${root} " /etc/fstab; then
    printf '%s %s btrfs loop,noatime,nofail 0 0\n' "${image}" "${root}" \
      | ${FC_SUDO} tee -a /etc/fstab >/dev/null
  fi
  if command -v systemctl >/dev/null 2>&1; then
    ${FC_SUDO} systemctl daemon-reload
  fi
  ${FC_SUDO} mount "${root}"
  ${FC_SUDO} chown "$(id -u):$(id -g)" "${root}"
  log "Mounted a btrfs loop file at ${root} (persisted in /etc/fstab)"
}

ensure_kathara_plugin() {
  # Lab images get this host's Kathara network plugin: they have no network.
  if docker plugin ls --format '{{.Name}}' | grep -q '^kathara/katharanp'; then
    log "Kathara network plugin already installed"
    return
  fi
  log "Installing the Kathara network plugin"
  docker plugin install --grant-all-permissions "kathara/katharanp_vde:$(host_arch)" >/dev/null
}

setup_firecracker() {
  log "Setting up the host for Firecracker execution"
  if [[ "$(id -u)" -ne 0 ]]; then
    need_cmd sudo
    FC_SUDO=sudo
  fi
  # mkfs.btrfs, sysctl and udevadm live in sbin, which some distributions
  # leave off a user's PATH.
  PATH="${PATH}:/usr/sbin:/sbin"
  ensure_kvm_access
  ensure_nested_virt
  ensure_userfaultfd
  ensure_hugepages
  ensure_firecracker_root
  ensure_kathara_plugin
  uv run nika config set nika.execution.mode=firecracker >/dev/null
  log "config/nika.yaml now runs benchmark trials in Firecracker; the first run builds the base image (about 20 min)"
}

validate_config() {
  # Never rewritten: obsolete keys fail validation and must be fixed by hand.
  if [[ -f config/nika.yaml ]] && ! uv run nika config show >/dev/null; then
    warn "config/nika.yaml does not validate against this NIKA version (see error above)."
    warn "Compare it with config/nika.example.yaml and remove or rename obsolete keys."
  fi
}

prepare_images() {
  if [[ "${SKIP_IMAGES}" -eq 1 ]]; then
    log "Skipping runtime image preparation (--skip-images)"
    return
  fi
  log "Preparing runtime images and caches (first run: ~20 min, ~16 GB; later runs only update what changed)"
  uv run nika images prepare
}

install_vendor_images() {
  log "Preparing vendor router images"
  mkdir -p "${VENDOR_CACHE}"
  # XRd first: missing Cisco download is noticed before the long RouterOS build.
  ensure_xrd_image
  ensure_routeros_image
}

print_next_steps() {
  local track_note=""
  case "${TRACK}" in
    stable)
      track_note="Track: stable (main). Prefer frozen benchmarks:
     uv run nika benchmark run --release 0.2.0 --split test --result_dir results/my-run
"
      ;;
    latest)
      track_note="Track: latest (dev). Working matrix / newest scenarios:
     uv run nika agent run -a byo.langgraph --problem dc_clos_s_link_down
"
      ;;
  esac

  local uv_note="Without uv: 'source ${VENV_BIN}/activate', then run 'nika ...' directly.
  This puts nika, clab, gnmic, and uv on PATH and writes nothing outside the repo.
"

  cat <<EOF

Done.

1) Set a provider API key in .env (or export it):
     OPENAI_API_KEY=...

2) Run:
${track_note}     uv run nika benchmark run --release 0.2.0 --split test --result_dir results/my-run
     # or: uv run nika agent run -a byo.langgraph -p openai -m gpt-5-mini --problem dc_clos_s_link_down

Vendor labs (after --with-vendor-images):
     uv run nika env run iosxr_simple_bgp
     uv run nika env run routeros_simple_bgp

${uv_note}New docker or clab_admins group membership: open a new login shell.
Remote install: docs/operations/remote.md
Every agent runs in a sandbox: install a runtime with --with-agentenv (recommended)
or --with-sbx (then sbx login). See docs/operations/agent-sandbox.md
Remove NIKA again: ./scripts/uninstall.sh
EOF
  if [[ ${#MANUAL_STEPS[@]} -gt 0 ]]; then
    printf '\nStill to do by hand:\n'
    printf '  - %s\n' "${MANUAL_STEPS[@]}"
  fi
}

main() {
  log "NIKA install root: ${ROOT}"
  check_prerequisites
  ensure_vrf_module
  mkdir -p "${NIKA_CACHE}"

  checkout_track
  ensure_docker
  if ! docker_usable && [[ -z "${NIKA_INSTALL_UNDER_SG:-}" ]]; then
    log "Continuing under the new docker group membership (sg docker)"
    export NIKA_INSTALL_UNDER_SG=1
    exec sg docker -c "$(printf '%q ' "${ROOT}/scripts/install.sh" "${ORIG_ARGS[@]}")"
  fi
  install_uv
  sync_python
  install_containerlab
  install_gnmic
  bootstrap_config
  validate_config
  check_host_tools
  ensure_host_limits
  if [[ "${WITH_AGENTENV}" -eq 1 ]]; then
    install_agentenv
  fi
  if [[ "${WITH_SBX}" -eq 1 ]]; then
    install_sbx
    ensure_kvm_access
  fi
  prepare_images
  if [[ "${WITH_VENDOR_IMAGES}" -eq 1 ]]; then
    install_vendor_images
  fi
  # After the images, which the base image copies, and so that the loop file
  # is sized to the disk they leave free.
  if [[ "${WITH_FIRECRACKER}" -eq 1 ]]; then
    setup_firecracker
  fi
  print_next_steps
}

main
