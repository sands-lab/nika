"""Stage the frozen Python packages of an agent environment.

Each agent names a requirement set (``AgentSpec.requirements``) under
``requirements/``: exact pins of its packages for the sandbox's Python. The host
downloads the wheels once per set into ``.nika_cache/agent-wheels/<set>``
and copies them into the workspace, where the sandbox installs them offline.
"""

from __future__ import annotations

import logging
import os
import shlex
import platform
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from packaging.specifiers import SpecifierSet

from nika.agents.sandbox.locks import host_file_lock
from nika.config import REPO_ROOT

logger = logging.getLogger(__name__)

# Names inside the agent workspace.
WHEEL_DIRNAME = ".wheels"
REQUIREMENTS_FILENAME = "requirements.txt"
REQUIREMENTS_DIR = Path(__file__).resolve().parent / "requirements"
_WHEEL_CACHE = REPO_ROOT / ".nika_cache" / "agent-wheels"
_CACHE_REQ_STAMP = ".requirements.txt"
_TARGET_PYTHON = "3.14"
# Virtualenv for an agent whose ``requires_python`` excludes the template's
# Python; uv (preinstalled in the templates) provides a matching interpreter.
AGENT_VENV = "$HOME/.agent-venv"
_VENV_PYTHON = f'"{AGENT_VENV}/bin/python"'


# glibc of the sandbox templates is 2.43; wheels built for older glibc run too.
_MANYLINUX_VERSIONS = ("2_17", "2_28", "2_34")


def _target_platforms() -> list[str]:
    """Wheel platform tags for the microVM, which shares the host architecture."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("aarch64", "arm64"):
        arch = "aarch64"
    else:
        raise RuntimeError(f"Unsupported host architecture for agent wheels: {machine}")
    # pip matches tags exactly, so the legacy alias of 2_17 is listed too.
    return [f"manylinux2014_{arch}"] + [
        f"manylinux_{version}_{arch}" for version in _MANYLINUX_VERSIONS
    ]


def requirements_file(name: str) -> Path:
    path = REQUIREMENTS_DIR / f"{name}.txt"
    if not path.is_file():
        raise RuntimeError(f"Missing agent requirement set: {path}")
    return path


def _cache_stamp_text(name: str) -> str:
    """Pins plus target platform, so a cache built for another arch is refreshed."""
    req_text = requirements_file(name).read_text(encoding="utf-8")
    return f"# platform: {' '.join(_target_platforms())}\n{req_text}"


def load_requirements(name: str) -> tuple[str, ...]:
    """Return the frozen ``pkg==version`` pins of requirement set *name*."""
    path = requirements_file(name)
    reqs: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            raise RuntimeError(
                f"Agent requirements must be exact pins (pkg==version), got: "
                f"{line!r} in {path}"
            )
        reqs.append(line)
    if not reqs:
        raise RuntimeError(f"Agent requirement set is empty: {path}")
    return tuple(reqs)


def wheel_dir(workspace_dir: Path) -> Path:
    return workspace_dir.resolve() / WHEEL_DIRNAME


def _host_pip() -> list[str]:
    """Command that runs pip on the host (uv venvs often omit pip).

    A system interpreter with pip, else pip run by ``uvx``.
    """
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
            return [candidate, "-m", "pip"]
    if uvx := shutil.which("uvx"):
        return [uvx, "--from", "pip", "pip"]
    raise RuntimeError(
        "No host pip is available to download agent wheels. Install pip for "
        "/usr/bin/python3 or uv, or disable nika.sandbox.offline_sdk_wheels to "
        "install from PyPI in-sandbox."
    )


def _wheel_name_index(dest: Path) -> str:
    return " ".join(p.name.lower() for p in dest.glob("*.whl"))


def _requirement_wheel_present(names: str, req: str) -> bool:
    name, _, version = req.partition("==")
    # Wheel filenames use underscores in the distribution segment.
    needle = f"{name.lower().replace('-', '_')}-{version.lower()}-"
    return needle in names


def _cache_matches_requirements(dest: Path, name: str, stamp_text: str) -> bool:
    stamp = dest / _CACHE_REQ_STAMP
    if not stamp.is_file() or stamp.read_text(encoding="utf-8") != stamp_text:
        return False
    existing = list(dest.glob("*.whl"))
    if not existing:
        return False
    names = _wheel_name_index(dest)
    return all(
        _requirement_wheel_present(names, req) for req in load_requirements(name)
    )


def _pip_download(dest: Path, name: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    stamp_text = _cache_stamp_text(name)
    if _cache_matches_requirements(dest, name, stamp_text):
        logger.debug(
            "Reusing %d cached agent wheels in %s",
            len(list(dest.glob("*.whl"))),
            dest,
        )
        return
    if any(dest.iterdir()):
        # Incomplete or stale cache (pins or platform changed) — refresh.
        shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)

    cmd = [
        *_host_pip(),
        "download",
        "--dest",
        str(dest),
        "--python-version",
        _TARGET_PYTHON,
        *(arg for tag in _target_platforms() for arg in ("--platform", tag)),
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
        str(requirements_file(name)),
    ]
    # Avoid picking up the active uv venv which may lack pip.
    env = {**os.environ, "VIRTUAL_ENV": "", "PYTHONPATH": ""}
    logger.info(
        "Downloading frozen agent wheels (Python %s) into %s",
        _TARGET_PYTHON,
        dest,
    )
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)
    if proc.returncode != 0 or not list(dest.glob("*.whl")):
        raise RuntimeError(
            "Failed to download agent wheels for sandbox install.\n"
            f"command: {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    (dest / _CACHE_REQ_STAMP).write_text(stamp_text, encoding="utf-8")


def stage_wheels(workspace_dir: Path, name: str) -> Path:
    """Copy the wheels of requirement set *name* into *workspace_dir*/.wheels."""
    cache = _WHEEL_CACHE / name
    dest = wheel_dir(workspace_dir)
    if dest.exists():
        shutil.rmtree(dest)
    # Parallel trials share the cache: one refreshes it while the others wait.
    with host_file_lock(_WHEEL_CACHE / f".{name}.lock"):
        _pip_download(cache, name)
        shutil.copytree(cache, dest, ignore=shutil.ignore_patterns(_CACHE_REQ_STAMP))
    return dest


def stage_requirements(workspace_dir: Path, name: str) -> Path:
    """Copy requirement set *name* into *workspace_dir* as ``requirements.txt``."""
    dest = workspace_dir.resolve() / REQUIREMENTS_FILENAME
    shutil.copyfile(requirements_file(name), dest)
    return dest


def pip_install_command(workspace: str, *, offline: bool) -> str:
    """Return the pip command that installs the staged pins inside a sandbox.

    *workspace* is where the staged workspace (requirements and, when
    *offline*, ``.wheels``) appears inside the sandbox.
    """
    req_file = f"{workspace}/{REQUIREMENTS_FILENAME}"
    if offline:
        # Install offline to avoid slow/unreliable in-VM PyPI downloads
        # (exit 137 during sbx create).
        wheels = f"{workspace}/{WHEEL_DIRNAME}"
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


def packages_install_command(packages: Sequence[str]) -> str:
    """Return the pip command that installs extra *packages* from PyPI."""
    return (
        "pip3 install --break-system-packages --retries 10 --timeout 120 "
        + " ".join(shlex.quote(package) for package in packages)
    )


def needs_agent_venv(requires_python: str | None) -> bool:
    """Whether *requires_python* excludes the sandbox template's Python."""
    if not requires_python:
        return False
    return not SpecifierSet(requires_python).contains(_TARGET_PYTHON)


def venv_install_commands(
    requires_python: str,
    *,
    requirements_file: str | None = None,
    packages: Sequence[str] = (),
    no_cache: bool = False,
) -> list[str]:
    """Commands that create :data:`AGENT_VENV` and install the agent into it.

    uv picks or downloads an interpreter matching *requires_python*, then
    installs *requirements_file* and *packages* from PyPI: the offline wheels
    are built for the template's Python only.
    """
    install = "UV_HTTP_TIMEOUT=120 uv pip install"
    if no_cache:
        install += " --no-cache"
    install += f" --python {_VENV_PYTHON}"
    commands = [f'uv venv --python {shlex.quote(requires_python)} "{AGENT_VENV}"']
    if requirements_file:
        commands.append(f"{install} -r {shlex.quote(requirements_file)}")
    if packages:
        commands.append(
            f"{install} " + " ".join(shlex.quote(package) for package in packages)
        )
    return commands


def runner_path_prefix(requires_python: str | None) -> str:
    """Shell prefix that puts the agent venv first on ``PATH``, if it has one."""
    if not needs_agent_venv(requires_python):
        return ""
    return f'PATH="{AGENT_VENV}/bin:$PATH" '
