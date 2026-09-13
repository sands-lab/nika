"""Test-only helpers to simulate an arm64 NIKA user on an amd64 host.

Registers qemu-aarch64 via binfmt, imports/builds a local arm64 runner image
(avoiding flaky Hub multi-arch pulls), runs NIKA against the host Docker socket
with DOCKER_DEFAULT_PLATFORM=linux/arm64, and restores host-arch nika/onos.
Not used by production code.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.support.prerequisites import docker_available

REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
_QEMU_AARCH64_BINFMT = Path("/proc/sys/fs/binfmt_misc/qemu-aarch64")
_BINFMT_IMAGE = "tonistiigi/binfmt:latest"
_UBUNTU_ROOTFS_URL = (
    "https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/"
    "ubuntu-base-24.04.3-base-arm64.tar.gz"
)
_UBUNTU_LOCAL = "nika-test/ubuntu-arm64:local"
_RUNNER_IMAGE = "nika-test/arm64-user-runner:local"
_ARM64_VENV = "/tmp/nika-arm64-user-e2e-venv"
_DOCKER_CLI_FIXTURE = _FIXTURES / "docker-linux-arm64"
_DOCKER_CLI_URL = (
    "https://download.docker.com/linux/static/stable/aarch64/docker-27.5.1.tgz"
)
_HOST_BUILDX_CANDIDATES = (
    Path("/usr/libexec/docker/cli-plugins/docker-buildx"),
    Path("/usr/local/lib/docker/cli-plugins/docker-buildx"),
    Path.home() / ".docker/cli-plugins/docker-buildx",
)
_UV_ARM64_DIR = _FIXTURES / "uv-arm64"
_UV_ARM64_URL = (
    "https://github.com/astral-sh/uv/releases/download/0.8.22/"
    "uv-aarch64-unknown-linux-gnu.tar.gz"
)
_RUNNER_DOCKERFILE = _FIXTURES / "Dockerfile.arm64-user-runner"


def qemu_aarch64_binfmt_registered() -> bool:
    return _QEMU_AARCH64_BINFMT.is_file()


def ensure_qemu_aarch64_binfmt() -> bool:
    """Install qemu-aarch64 binfmt handlers. Return True if usable."""
    if qemu_aarch64_binfmt_registered():
        return True
    if not docker_available():
        return False
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--privileged",
                _BINFMT_IMAGE,
                "--install",
                "arm64",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return qemu_aarch64_binfmt_registered()


def ensure_arm64_docker_cli_fixture() -> Path | None:
    """Ensure a linux/arm64 docker CLI binary exists under tests/support/fixtures."""
    if _DOCKER_CLI_FIXTURE.is_file() and _DOCKER_CLI_FIXTURE.stat().st_size > 0:
        return _DOCKER_CLI_FIXTURE
    try:
        _DOCKER_CLI_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        staging = _DOCKER_CLI_FIXTURE.parent / "_docker_cli_staging"
        staging.mkdir(exist_ok=True)
        tgz = staging / "docker.tgz"
        subprocess.run(
            ["curl", "-fsSL", "-o", str(tgz), _DOCKER_CLI_URL],
            check=True,
            timeout=300,
        )
        subprocess.run(
            ["tar", "-xzf", str(tgz), "-C", str(staging)],
            check=True,
            timeout=60,
        )
        src = staging / "docker" / "docker"
        os.replace(src, _DOCKER_CLI_FIXTURE)
        _DOCKER_CLI_FIXTURE.chmod(0o755)
        for path in sorted(staging.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                path.rmdir()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return _DOCKER_CLI_FIXTURE if _DOCKER_CLI_FIXTURE.is_file() else None


def host_docker_buildx() -> Path | None:
    """Return a host docker-buildx binary (amd64 OK; qemu-x86_64 runs it in-arm64)."""
    for path in _HOST_BUILDX_CANDIDATES:
        if path.is_file():
            return path
    return None


def _image_exists(image: str) -> bool:
    try:
        subprocess.run(
            ["docker", "image", "inspect", image],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def ensure_ubuntu_arm64_base() -> bool:
    """Import Ubuntu arm64 rootfs as a local image (no Hub multi-arch pull)."""
    if _image_exists(_UBUNTU_LOCAL):
        return True
    tarball = Path("/tmp/ubuntu-base-arm64.tar.gz")
    try:
        if not tarball.is_file() or tarball.stat().st_size < 1_000_000:
            subprocess.run(
                ["curl", "-fsSL", "-o", str(tarball), _UBUNTU_ROOTFS_URL],
                check=True,
                timeout=600,
            )
        subprocess.run(
            [
                "docker",
                "import",
                "--platform",
                "linux/arm64",
                str(tarball),
                _UBUNTU_LOCAL,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return _image_exists(_UBUNTU_LOCAL)


def ensure_uv_arm64_fixture() -> Path | None:
    """Ensure the linux/arm64 ``uv`` binary exists for the runner Dockerfile."""
    uv_bin = _UV_ARM64_DIR / "uv"
    if uv_bin.is_file() and uv_bin.stat().st_size > 0:
        return uv_bin
    try:
        _UV_ARM64_DIR.mkdir(parents=True, exist_ok=True)
        tgz = _FIXTURES / "_uv_arm64.tgz"
        subprocess.run(
            ["curl", "-fsSL", "-o", str(tgz), _UV_ARM64_URL],
            check=True,
            timeout=600,
        )
        subprocess.run(
            ["tar", "-xzf", str(tgz), "-C", str(_UV_ARM64_DIR), "--strip-components=1"],
            check=True,
            timeout=60,
        )
        tgz.unlink(missing_ok=True)
        uv_bin.chmod(0o755)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return uv_bin if uv_bin.is_file() else None


def ensure_arm64_user_runner_image() -> bool:
    """Build the test-only arm64 runner image from the local Ubuntu rootfs."""
    if _image_exists(_RUNNER_IMAGE):
        return True
    if not ensure_ubuntu_arm64_base():
        return False
    if ensure_uv_arm64_fixture() is None:
        return False
    if not _RUNNER_DOCKERFILE.is_file():
        return False
    try:
        env = {**os.environ, "DOCKER_BUILDKIT": "1"}
        subprocess.run(
            [
                "docker",
                "build",
                "--platform",
                "linux/arm64",
                "-t",
                _RUNNER_IMAGE,
                "-f",
                str(_RUNNER_DOCKERFILE),
                str(_FIXTURES),
            ],
            check=True,
            env=env,
            timeout=1800,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return _image_exists(_RUNNER_IMAGE)


def arm64_user_e2e_available() -> bool:
    """True when Docker, qemu-arm64, CLI fixture, and host buildx are ready.

    Does not build the runner image (that happens in the test body).
    """
    if not docker_available():
        return False
    if not ensure_qemu_aarch64_binfmt():
        return False
    if ensure_arm64_docker_cli_fixture() is None:
        return False
    return host_docker_buildx() is not None


def restore_host_arch_nika_onos(*, repo: Path | None = None) -> None:
    """Restore host-arch images after an arm64 user E2E run."""
    root = repo or REPO_ROOT
    env = {**os.environ}
    env.pop("DOCKER_DEFAULT_PLATFORM", None)
    host_arch = "amd64" if os.uname().machine in ("x86_64", "amd64") else "arm64"
    platform = f"linux/{host_arch}"
    for image in ("kathara/base:latest", "kathara/sdn:latest"):
        subprocess.run(
            ["docker", "pull", "--platform", platform, image],
            check=False,
            capture_output=True,
            text=True,
            timeout=600,
            env=env,
        )
    subprocess.run(
        [
            "uv",
            "run",
            "python",
            "-m",
            "nika.net_env.utils.kathara.docker_files.docker_images",
            "-f",
            "nika/onos",
            "nika/base",
            "nika/nginx",
        ],
        cwd=str(root),
        env=env,
        check=True,
        timeout=1200,
    )


def run_in_arm64_nika_user(
    script: str,
    *,
    repo: Path | None = None,
    timeout: float = 3600.0,
) -> subprocess.CompletedProcess[str]:
    """Run a bash script as an arm64 NIKA user against the host Docker engine."""
    root = (repo or REPO_ROOT).resolve()
    root_s = str(root)
    cli = ensure_arm64_docker_cli_fixture()
    if cli is None:
        raise RuntimeError("arm64 docker CLI fixture missing")
    buildx = host_docker_buildx()
    if buildx is None:
        raise RuntimeError("host docker-buildx plugin missing")
    if not ensure_arm64_user_runner_image():
        raise RuntimeError("arm64 user runner image missing")

    # Persist venv + uv cache across runs (named volumes survive container exit).
    for vol in ("nika-arm64-e2e-venv", "nika-arm64-e2e-uv-cache"):
        subprocess.run(
            ["docker", "volume", "create", vol],
            check=False,
            capture_output=True,
            timeout=30,
        )

    wrapped = f"""set -euo pipefail
export DOCKER_DEFAULT_PLATFORM=linux/arm64
export DOCKER_CONFIG=/tmp/.docker
export UV_PROJECT_ENVIRONMENT={_ARM64_VENV}
export UV_CACHE_DIR=/uv-cache
export UV_HTTP_TIMEOUT=600
export UV_CONCURRENT_DOWNLOADS=4
export PATH="/usr/local/bin:$PATH"
mkdir -p /tmp/.docker/cli-plugins
docker version >/dev/null
docker buildx version >/dev/null
uname -m
{script}
"""
    cmd = [
        "docker",
        "run",
        "--rm",
        "--platform",
        "linux/arm64",
        "-v",
        "/var/run/docker.sock:/var/run/docker.sock",
        "-v",
        f"{root_s}:{root_s}",
        "-v",
        f"{cli}:/usr/local/bin/docker:ro",
        "-v",
        f"{buildx}:/tmp/.docker/cli-plugins/docker-buildx:ro",
        "-v",
        "nika-arm64-e2e-venv:/tmp/nika-arm64-user-e2e-venv",
        "-v",
        "nika-arm64-e2e-uv-cache:/uv-cache",
        "-w",
        root_s,
        "-e",
        "DOCKER_DEFAULT_PLATFORM=linux/arm64",
        "-e",
        "DOCKER_CONFIG=/tmp/.docker",
        "-e",
        f"UV_PROJECT_ENVIRONMENT={_ARM64_VENV}",
        "-e",
        "UV_CACHE_DIR=/uv-cache",
        "-e",
        "UV_HTTP_TIMEOUT=600",
        "-e",
        "HOME=/tmp",
        _RUNNER_IMAGE,
        "bash",
        "-lc",
        wrapped,
    ]
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
