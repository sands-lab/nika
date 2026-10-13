"""Stage SDK Python wheels and pins for sandboxed SDK agents."""

from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from pathlib import Path

from nika.config import REPO_ROOT

logger = logging.getLogger(__name__)

SDK_WHEEL_DIRNAME = ".sdk_wheels"
SDK_REQUIREMENTS_FILENAME = "requirements-sdk.txt"
SDK_REQUIREMENTS_FILE = Path(__file__).resolve().parent / SDK_REQUIREMENTS_FILENAME
_WHEEL_CACHE = REPO_ROOT / ".nika_cache" / "sbx-sdk-wheels"
_CACHE_REQ_STAMP = ".requirements-sdk.txt"
_TARGET_PYTHON = "3.14"


def _target_platform() -> str:
    """Wheel platform tag for the microVM, which shares the host architecture."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "manylinux_2_17_x86_64"
    if machine in ("aarch64", "arm64"):
        return "manylinux_2_17_aarch64"
    raise RuntimeError(f"Unsupported host architecture for SDK wheels: {machine}")


def _cache_stamp_text() -> str:
    """Pins plus target platform, so a cache built for another arch is refreshed."""
    req_text = SDK_REQUIREMENTS_FILE.read_text(encoding="utf-8")
    return f"# platform: {_target_platform()}\n{req_text}"


def load_sdk_requirements() -> tuple[str, ...]:
    """Return frozen ``pkg==version`` pins from ``requirements-sdk.txt``."""
    if not SDK_REQUIREMENTS_FILE.is_file():
        raise RuntimeError(f"Missing SDK requirements freeze: {SDK_REQUIREMENTS_FILE}")
    reqs: list[str] = []
    for raw in SDK_REQUIREMENTS_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            raise RuntimeError(
                f"SDK requirements must be exact pins (pkg==version), got: {line!r} "
                f"in {SDK_REQUIREMENTS_FILE}"
            )
        reqs.append(line)
    if not reqs:
        raise RuntimeError(f"SDK requirements freeze is empty: {SDK_REQUIREMENTS_FILE}")
    return tuple(reqs)


# Direct + transitive pins used for download and install.
SDK_PIP_PACKAGES = load_sdk_requirements()


def sdk_wheel_dir(workspace_dir: Path) -> Path:
    return workspace_dir.resolve() / SDK_WHEEL_DIRNAME


def _host_pip_python() -> str:
    """Prefer a system interpreter with pip (uv venvs often omit pip)."""
    for candidate in (
        "/usr/bin/python3",
        shutil.which("python3") or "",
    ):
        if not candidate or not Path(candidate).is_file():
            continue
        probe = subprocess.run(
            [candidate, "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    raise RuntimeError(
        "No host Python with pip is available to download SDK wheels. "
        "Install pip for /usr/bin/python3 or ensure python3 -m pip works, "
        "or disable NIKA_SANDBOX_OFFLINE_SDK_WHEELS to install from PyPI in-sandbox."
    )


def _wheel_name_index(dest: Path) -> str:
    return " ".join(p.name.lower() for p in dest.glob("*.whl"))


def _requirement_wheel_present(names: str, req: str) -> bool:
    name, _, version = req.partition("==")
    # Wheel filenames use underscores in the distribution segment.
    needle = f"{name.lower().replace('-', '_')}-{version.lower()}-"
    return needle in names


def _cache_matches_requirements(dest: Path, stamp_text: str) -> bool:
    stamp = dest / _CACHE_REQ_STAMP
    if not stamp.is_file() or stamp.read_text(encoding="utf-8") != stamp_text:
        return False
    existing = list(dest.glob("*.whl"))
    if not existing:
        return False
    names = _wheel_name_index(dest)
    return all(_requirement_wheel_present(names, req) for req in SDK_PIP_PACKAGES)


def _pip_download(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    stamp_text = _cache_stamp_text()
    if _cache_matches_requirements(dest, stamp_text):
        logger.debug(
            "Reusing %d cached SDK wheels in %s",
            len(list(dest.glob("*.whl"))),
            dest,
        )
        return
    if any(dest.iterdir()):
        # Incomplete or stale cache (pins or platform changed) — refresh.
        shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)

    python = _host_pip_python()
    cmd = [
        python,
        "-m",
        "pip",
        "download",
        "--dest",
        str(dest),
        "--python-version",
        _TARGET_PYTHON,
        "--platform",
        _target_platform(),
        "--implementation",
        "cp",
        "--abi",
        f"cp{_TARGET_PYTHON.replace('.', '')}",
        "--only-binary=:all:",
        "--retries",
        "10",
        "--timeout",
        "120",
        "-r",
        str(SDK_REQUIREMENTS_FILE),
    ]
    # Avoid picking up the active uv venv which may lack pip.
    env = {**os.environ, "VIRTUAL_ENV": "", "PYTHONPATH": ""}
    logger.info(
        "Downloading frozen SDK wheels for sbx (Python %s) into %s",
        _TARGET_PYTHON,
        dest,
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    if proc.returncode != 0 or not list(dest.glob("*.whl")):
        raise RuntimeError(
            "Failed to download SDK wheels for sandbox install.\n"
            f"command: {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    (dest / _CACHE_REQ_STAMP).write_text(stamp_text, encoding="utf-8")


def stage_sdk_wheels(workspace_dir: Path) -> Path:
    """Ensure offline wheels are available under *workspace_dir*/.sdk_wheels."""
    _pip_download(_WHEEL_CACHE)
    dest = sdk_wheel_dir(workspace_dir)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(
        _WHEEL_CACHE,
        dest,
        ignore=shutil.ignore_patterns(_CACHE_REQ_STAMP),
    )
    return dest


def stage_sdk_requirements(workspace_dir: Path) -> Path:
    """Copy the frozen SDK requirements into *workspace_dir*."""
    dest = workspace_dir.resolve() / SDK_REQUIREMENTS_FILENAME
    shutil.copyfile(SDK_REQUIREMENTS_FILE, dest)
    return dest


def sdk_pip_install_command(workspace: str, *, offline: bool) -> str:
    """Return the pip command that installs the SDK pins inside a sandbox.

    *workspace* is where the staged workspace (requirements and, when
    *offline*, ``.sdk_wheels``) appears inside the sandbox.
    """
    req_file = f"{workspace}/{SDK_REQUIREMENTS_FILENAME}"
    if offline:
        # Install offline to avoid slow/unreliable in-VM PyPI downloads
        # (exit 137 during sbx create).
        wheels = f"{workspace}/{SDK_WHEEL_DIRNAME}"
        return (
            f"cd '{workspace}' && "
            "pip3 install --break-system-packages --no-index "
            f"--find-links='{wheels}' -r '{req_file}'"
        )
    return (
        f"cd '{workspace}' && "
        "pip3 install --break-system-packages --retries 10 --timeout 120 "
        f"-r '{req_file}'"
    )
