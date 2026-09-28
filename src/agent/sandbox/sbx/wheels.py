"""Stage and install SDK Python wheels for sbx shell sandboxes."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from agent.sandbox.sbx.client import run_sbx_checked
from nika.config import _REPO_ROOT

logger = logging.getLogger(__name__)

SDK_WHEEL_DIRNAME = ".sdk_wheels"
SDK_REQUIREMENTS_FILENAME = "requirements-sdk.txt"
_SBX_DIR = Path(__file__).resolve().parent
SDK_REQUIREMENTS_FILE = _SBX_DIR / SDK_REQUIREMENTS_FILENAME
_WHEEL_CACHE = _REPO_ROOT / ".nika_cache" / "sbx-sdk-wheels"
_CACHE_REQ_STAMP = ".requirements-sdk.txt"
_TARGET_PYTHON = "3.14"
_TARGET_PLATFORM = "manylinux_2_17_x86_64"


def load_sdk_requirements() -> tuple[str, ...]:
    """Return frozen ``pkg==version`` pins from sbx ``requirements-sdk.txt``."""
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


def _cache_matches_requirements(dest: Path, req_text: str) -> bool:
    stamp = dest / _CACHE_REQ_STAMP
    if not stamp.is_file() or stamp.read_text(encoding="utf-8") != req_text:
        return False
    existing = list(dest.glob("*.whl"))
    if not existing:
        return False
    names = _wheel_name_index(dest)
    return all(_requirement_wheel_present(names, req) for req in SDK_PIP_PACKAGES)


def _pip_download(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    req_text = SDK_REQUIREMENTS_FILE.read_text(encoding="utf-8")
    if _cache_matches_requirements(dest, req_text):
        logger.debug(
            "Reusing %d cached SDK wheels in %s",
            len(list(dest.glob("*.whl"))),
            dest,
        )
        return
    if any(dest.iterdir()):
        # Incomplete or stale cache (pins changed) — refresh.
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
        _TARGET_PLATFORM,
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
    (dest / _CACHE_REQ_STAMP).write_text(req_text, encoding="utf-8")


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


def _stage_requirements(workspace_dir: Path) -> Path:
    dest = workspace_dir.resolve() / SDK_REQUIREMENTS_FILENAME
    shutil.copyfile(SDK_REQUIREMENTS_FILE, dest)
    return dest


def _pip_install_inner(*, workspace_dir: Path, offline: bool) -> str:
    req_file = _stage_requirements(workspace_dir)
    if offline:
        wheels = sdk_wheel_dir(workspace_dir)
        if not any(wheels.glob("*.whl")):
            raise RuntimeError(f"No staged SDK wheels found under {wheels}")
        # Workspace is bind-mounted at the same host path; install offline to
        # avoid slow/unreliable in-VM PyPI downloads (exit 137 during create).
        return (
            f"cd '{workspace_dir}' && "
            "pip3 install --break-system-packages --no-index "
            f"--find-links='{wheels}' -r '{req_file}'"
        )
    return (
        f"cd '{workspace_dir}' && "
        "pip3 install --break-system-packages --retries 10 --timeout 120 "
        f"-r '{req_file}'"
    )


def install_sdk_packages_in_sandbox(
    *,
    sandbox_name: str,
    workspace_dir: Path,
    offline: bool,
) -> None:
    """Install SDK Python deps inside an existing sbx shell sandbox."""
    if offline:
        logger.info("Installing SDK packages from offline wheels in %s", sandbox_name)
    else:
        logger.info("Installing frozen SDK packages from PyPI in %s", sandbox_name)
    inner = _pip_install_inner(workspace_dir=workspace_dir, offline=offline)
    run_sbx_checked(["exec", sandbox_name, "bash", "-lc", inner])


def install_sdk_wheels_in_sandbox(
    *,
    sandbox_name: str,
    workspace_dir: Path,
) -> None:
    """Install staged wheels inside an existing sbx shell sandbox."""
    install_sdk_packages_in_sandbox(
        sandbox_name=sandbox_name,
        workspace_dir=workspace_dir,
        offline=True,
    )
