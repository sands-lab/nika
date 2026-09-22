#!/usr/bin/env bash
# Install Docker, uv, lab deps (Kathará), Containerlab, gnmic, and fault-injection tools.
# Optionally switch git track and prepare vendor router images (RouterOS CHR, Cisco XRd).
# Usage: ./scripts/install.sh [options]
set -euo pipefail

GNMIC_VERSION="${GNMIC_VERSION:-0.48.0}"

# Keep in sync with lab.py IMAGE constants.
ROUTEROS_VERSION="${ROUTEROS_VERSION:-7.21.5}"
ROUTEROS_IMAGE="vrnetlab/mikrotik_routeros:${ROUTEROS_VERSION}"
XRD_IMAGE_TAG="${XRD_IMAGE_TAG:-26.2.1}"
XRD_IMAGE="ios-xr/xrd-control-plane:${XRD_IMAGE_TAG}"
VRNETLAB_REPO_URL="${VRNETLAB_REPO_URL:-https://github.com/hellt/vrnetlab}"
CHR_BASE_URL="${CHR_BASE_URL:-https://download.mikrotik.com/routeros/${ROUTEROS_VERSION}}"

TRACK=""
WITH_VENDOR_IMAGES=0
SKIP_ROUTEROS=0
SKIP_XRD=0
XRD_TARBALL="${NIKA_XRD_TARBALL:-}"

usage() {
  cat <<EOF
Usage: ./scripts/install.sh [options]

Installs Docker (if needed), uv, lab Python deps (Kathará), Containerlab,
gnmic, plus clang and iproute2 for fault injection (via apt-get). Creates
.env and config/nika.yaml from examples when missing.

Options:
  --track stable|latest
      stable  switch to git branch main (published benchmarks)
      latest  switch to git branch dev (newest features)
      Omit to keep your current branch (default; safe for CI).

  --with-vendor-images
      After the base install, prepare images for vendor BGP labs:
        - RouterOS: download MikroTik CHR and build ${ROUTEROS_IMAGE}
        - XRd: docker load a local Cisco tarball as ${XRD_IMAGE}
      Do nothing vendor-related unless you pass this flag.

  --xrd-tarball PATH
      Path to a Cisco XRd Control Plane container .tgz (no public URL).
      Or set NIKA_XRD_TARBALL, or put vendor/xrd-*.tgz under the repo.

  --skip-routeros   With --with-vendor-images, skip RouterOS/CHR build
  --skip-xrd        With --with-vendor-images, skip XRd load
  -h, --help        Show this help

Examples:
  ./scripts/install.sh
  ./scripts/install.sh --track stable
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
    --skip-routeros) SKIP_ROUTEROS=1; shift ;;
    --skip-xrd) SKIP_XRD=1; shift ;;
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

VENDOR_CACHE="${NIKA_VENDOR_CACHE:-${HOME}/.cache/nika/vendor}"

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
  log "Syncing Python deps (uv sync)"
  uv sync
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

install_fault_injection_tools() {
  if ! command -v apt-get >/dev/null 2>&1; then
    log "Skipping clang and iproute2: apt-get not found; install them manually for fault injection"
    return
  fi
  need_cmd sudo
  log "Installing fault-injection tools (clang for eBPF, iproute2 for tc)"
  sudo apt-get update
  sudo apt-get install -y clang iproute2
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
    *) die "unsupported architecture for RouterOS CHR: ${m}" ;;
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
  curl -fL --retry 3 --retry-delay 2 -o "${dest}.partial" "${url}"
  mv "${dest}.partial" "${dest}"
}

ensure_routeros_image() {
  if docker_image_exists "${ROUTEROS_IMAGE}"; then
    log "RouterOS image already present: ${ROUTEROS_IMAGE}"
    return
  fi

  need_cmd docker
  need_cmd git
  need_cmd make
  need_cmd unzip

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
  if [[ ! -d "${build_dir}/.git" ]]; then
    log "Cloning ${VRNETLAB_REPO_URL}"
    rm -rf "${build_dir}"
    git clone --depth 1 "${VRNETLAB_REPO_URL}" "${build_dir}"
  else
    log "Using existing vrnetlab clone at ${build_dir}"
  fi

  local ros_dir="${build_dir}/mikrotik/routeros"
  [[ -d "${ros_dir}" ]] || die "vrnetlab checkout missing mikrotik/routeros"

  # Clear prior CHR disks so make picks a single IMAGE_GLOB match.
  rm -f "${ros_dir}"/chr-*.vmdk "${ros_dir}"/chr-*.vdi
  unzip -o -d "${ros_dir}" "${chr_zip}"
  [[ -f "${ros_dir}/${disk_name}" ]] || die "expected ${disk_name} after unzip"

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
  (
    cd "${ros_dir}"
    make docker-image
  )

  if docker_image_exists "${ROUTEROS_IMAGE}"; then
    log "RouterOS image ready: ${ROUTEROS_IMAGE}"
    return
  fi

  if docker_image_exists "vrnetlab/mikrotik_routeros:${ROUTEROS_VERSION}-${arch}"; then
    docker tag "vrnetlab/mikrotik_routeros:${ROUTEROS_VERSION}-${arch}" "${ROUTEROS_IMAGE}"
    log "Tagged ${ROUTEROS_IMAGE} from ${ROUTEROS_VERSION}-${arch}"
    return
  fi

  die "RouterOS build finished but ${ROUTEROS_IMAGE} was not found (also looked for vrnetlab/mikrotik_routeros:${ROUTEROS_VERSION}-${arch})"
}

find_xrd_tarball() {
  if [[ -n "${XRD_TARBALL}" ]]; then
    [[ -f "${XRD_TARBALL}" ]] || die "XRd tarball not found: ${XRD_TARBALL}"
    printf '%s\n' "${XRD_TARBALL}"
    return
  fi
  local match
  shopt -s nullglob
  for match in "${ROOT}"/vendor/xrd-*.tgz "${ROOT}"/vendor/xrd-*.tar "${VENDOR_CACHE}"/xrd-*.tgz; do
    if [[ -f "${match}" ]]; then
      printf '%s\n' "${match}"
      return
    fi
  done
  shopt -u nullglob
  return 1
}

ensure_xrd_inotify() {
  if [[ "$(id -u)" -eq 0 ]]; then
    sysctl -w fs.inotify.max_user_instances=64000 >/dev/null
    sysctl -w fs.inotify.max_user_watches=64000 >/dev/null
    log "Raised inotify limits for XRd"
    return
  fi
  if command -v sudo >/dev/null 2>&1; then
    if sudo sysctl -w fs.inotify.max_user_instances=64000 >/dev/null \
      && sudo sysctl -w fs.inotify.max_user_watches=64000 >/dev/null; then
      log "Raised inotify limits for XRd"
      return
    fi
  fi
  warn "Could not raise inotify limits; for XRd run:"
  warn "  sudo sysctl -w fs.inotify.max_user_instances=64000"
  warn "  sudo sysctl -w fs.inotify.max_user_watches=64000"
}

ensure_xrd_image() {
  if docker_image_exists "${XRD_IMAGE}"; then
    log "XRd image already present: ${XRD_IMAGE}"
    ensure_xrd_inotify
    return
  fi

  local tarball
  if ! tarball="$(find_xrd_tarball)"; then
    warn "XRd tarball not found; skipping ${XRD_IMAGE}"
    warn "Cisco does not publish a public download URL. Get an XRd Control Plane"
    warn "container .tgz from CCO / Cisco Modeling Labs, then re-run:"
    warn "  ./scripts/install.sh --with-vendor-images --skip-routeros \\"
    warn "    --xrd-tarball /path/to/xrd-control-plane-container-x86_64-*.tgz"
    warn "Or place the file at vendor/xrd-*.tgz under the repo root."
    return
  fi

  need_cmd docker
  log "Loading XRd tarball: ${tarball}"
  local load_out loaded
  load_out="$(docker load -i "${tarball}")"
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
  ensure_xrd_inotify
}

install_vendor_images() {
  log "Preparing vendor router images"
  mkdir -p "${VENDOR_CACHE}" "${ROOT}/vendor"
  if [[ "${SKIP_ROUTEROS}" -eq 0 ]]; then
    ensure_routeros_image
  else
    log "Skipping RouterOS (--skip-routeros)"
  fi
  if [[ "${SKIP_XRD}" -eq 0 ]]; then
    ensure_xrd_image
  else
    log "Skipping XRd (--skip-xrd)"
  fi
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

New Docker install: open a new shell or run newgrp docker.
Remote install: docs/operations/remote.md
Optional sbx: docs/operations/agent-sandbox.md
EOF
}

main() {
  log "NIKA install root: ${ROOT}"

  checkout_track
  ensure_docker
  install_uv
  sync_python
  install_containerlab
  install_gnmic
  bootstrap_config
  install_fault_injection_tools
  if [[ "${WITH_VENDOR_IMAGES}" -eq 1 ]]; then
    install_vendor_images
  fi
  print_next_steps
}

main
