#!/usr/bin/env bash
# Install uv, lab deps (Kathará), Containerlab, and gnmic into the repo (.venv and
# .nika_cache) without apt, then build/pull every runtime image. Re-runs upgrade
# outdated parts and prune stale ones. Optionally switch git track, install sbx with
# KVM access, and prepare vendor router images (RouterOS CHR, Cisco XRd).
# Usage: ./scripts/install.sh [options]
set -euo pipefail

GNMIC_VERSION="${GNMIC_VERSION:-0.48.0}"
# Installed when clab is missing or older than CLAB_MIN_VERSION; newer ones are kept.
CLAB_VERSION="${CLAB_VERSION:-0.79.0}"
CLAB_MIN_VERSION="${CLAB_MIN_VERSION:-0.79.0}"
INOTIFY_CONF=/etc/sysctl.d/99-nika-inotify.conf
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
labs, and loads and persists the vrf kernel module. Creates .env
and config/nika.yaml from examples when missing.

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
      Or set NIKA_XRD_TARBALL, or put .nika_cache/vendor/xrd-*.tgz under the repo.

  --with-sbx
      Install Docker Sandboxes (sbx) for sandboxed agents (cli.*, sdk.*,
      community.sade) and give your user read/write access to /dev/kvm,
      which the sbx microVMs need. sbx ships only as a package, so this is
      the one step that uses apt. Run 'sbx login' afterwards.

  --skip-images
      Skip building/pulling runtime images (labs then prepare them on first deploy).

  -h, --help        Show this help

Examples:
  ./scripts/install.sh
  ./scripts/install.sh --track stable
  ./scripts/install.sh --with-sbx
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

NIKA_CACHE="${ROOT}/.nika_cache"
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
  need_cmd sudo
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
  sudo install -o root -g root -m 4755 "${tmp}/containerlab" "${VENV_BIN}/containerlab"
  rm -rf "${tmp}"
  trap - EXIT
  ln -sfn containerlab "${VENV_BIN}/clab"
  if ! getent group clab_admins >/dev/null 2>&1; then
    sudo groupadd -r clab_admins
  fi
  local user
  user="$(id -un)"
  if [[ "$(id -u)" -ne 0 ]] && ! id -nG "${user}" | tr ' ' '\n' | grep -qx clab_admins; then
    sudo usermod -aG clab_admins "${user}"
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
  cp "${build_dir}"/common/*.py "${context}/"
  "${VENV_BIN}/python" -m zipfile -e "${chr_zip}" "${context}"
  [[ -f "${context}/${disk_name}" ]] || die "expected ${disk_name} in ${chr_zip}"

  # vrnetlab-base already ships qemu, openssh-client, and sshpass (needed by
  # NIKA's docker-exec → SSH management path). Skip apt-get so builds work
  # when Docker DNS cannot reach deb.debian.org.
  cat >"${ros_dir}/docker/Dockerfile" <<'DOCKERFILE'
FROM ghcr.io/srl-labs/vrnetlab-base:0.3.0
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

ensure_inotify_limits() {
  # k3s (k8s_lab, llmd_lab) and Cisco XRd exhaust the kernel default of 128
  # instances; every running container's shim also holds one. Never lower a
  # value the host already set higher. "# nika-original" lines keep the
  # pre-install values for scripts/uninstall.sh; a conf written by an older
  # installer has none, so its originals are unknown.
  local conf="${INOTIFY_CONF}"
  local key value original settings=""
  for key in max_user_instances max_user_watches; do
    value="$(sysctl -n "fs.inotify.${key}")"
    original="${value}"
    if [[ -f "${conf}" ]]; then
      original="$(sed -n "s/^# nika-original fs\.inotify\.${key}=//p" "${conf}")"
      if [[ -z "${original}" ]]; then
        original=unknown
        log "Migrating ${conf} from an older installer (original ${key} unknown)"
      fi
    fi
    if (( value < 64000 )); then
      value=64000
    fi
    settings="# nika-original fs.inotify.${key}=${original}"$'\n'"${settings}"
    settings+="fs.inotify.${key}=${value}"$'\n'
  done

  local sudo=""
  if [[ "$(id -u)" -ne 0 ]]; then
    sudo=sudo
  fi
  if printf '%s' "${settings}" | ${sudo} tee "${conf}" >/dev/null \
    && ${sudo} sysctl -p "${conf}" >/dev/null; then
    log "Raised inotify limits (persisted in ${conf})"
    return
  fi
  warn "Could not raise inotify limits; k8s_lab, llmd_lab, and iosxr_simple_bgp need:"
  warn "  sudo sysctl -w fs.inotify.max_user_instances=64000"
  warn "  sudo sysctl -w fs.inotify.max_user_watches=64000"
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
  # sbx boots each sandbox as a KVM microVM; without read/write on /dev/kvm,
  # 'sbx create' fails with "KVM error: Permission denied".
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
    warn "/dev/kvm is owned by group root; grant ${USER} read/write on it for sbx"
    return
  fi
  need_cmd sudo
  sudo usermod -aG "${group}" "${USER}"
  log "Added ${USER} to the ${group} group for /dev/kvm"
  warn "Open a new login shell (or run 'newgrp ${group}'), then run 'sbx daemon stop'"
  warn "so the next NIKA run starts sandboxd with the new group"
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
Sandboxed agents (after --with-sbx): sbx login, then see docs/operations/agent-sandbox.md
Remove NIKA again: ./scripts/uninstall.sh
EOF
}

main() {
  log "NIKA install root: ${ROOT}"
  check_prerequisites
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
  ensure_inotify_limits
  ensure_vrf_module
  if [[ "${WITH_SBX}" -eq 1 ]]; then
    install_sbx
    ensure_kvm_access
  fi
  prepare_images
  if [[ "${WITH_VENDOR_IMAGES}" -eq 1 ]]; then
    install_vendor_images
  fi
  print_next_steps
}

main
