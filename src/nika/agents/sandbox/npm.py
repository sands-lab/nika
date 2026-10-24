"""Stage pinned npm CLIs into an agent workspace.

CLI harnesses such as OpenCode and DeepSeek Harness ship as npm packages. The
sandbox template already has Node, but its network policy does not reach the
npm registry, so the host installs each pin once into
``.nika_cache/agent-npm/<set>/`` and copies the prefix into the workspace.
The agent then runs the binaries from ``.npm-prefix/bin``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from nika.agents.sandbox.locks import host_file_lock
from nika.config import REPO_ROOT

logger = logging.getLogger(__name__)

NPM_DIRNAME = ".npm-prefix"
_NPM_CACHE = REPO_ROOT / ".nika_cache" / "agent-npm"
_CACHE_STAMP = ".packages.txt"


def npm_dir(workspace_dir: Path) -> Path:
    return workspace_dir.resolve() / NPM_DIRNAME


def npm_bin_dir(workspace_dir: Path) -> Path:
    return npm_dir(workspace_dir) / "bin"


def _pin_list(packages: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(p.strip() for p in packages if p.strip()))


def npm_set_id(packages: Sequence[str]) -> str:
    """Stable cache directory name for a pin list (order-independent)."""
    pins = _pin_list(packages)
    if not pins:
        return "empty"
    text = "__".join(pins)
    return "".join(c if c.isalnum() or c in "._@+-" else "_" for c in text)


def _stamp_text(packages: Sequence[str]) -> str:
    return "\n".join(_pin_list(packages)) + "\n"


def _npm_install(dest: Path, packages: Sequence[str]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError(
            "npm is required on the NIKA host to stage CLI harness packages "
            f"({', '.join(packages)}). Install Node.js 22+ and retry."
        )
    cmd = [
        npm,
        "install",
        "--global",
        "--prefix",
        str(dest),
        "--omit=dev",
        "--no-fund",
        "--no-audit",
        *packages,
    ]
    logger.info("Installing npm packages for sandbox CLIs: %s", " ".join(packages))
    proc = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "npm_config_update_notifier": "false"},
    )
    bin_dir = dest / "bin"
    if proc.returncode != 0 or not bin_dir.is_dir() or not any(bin_dir.iterdir()):
        raise RuntimeError(
            "Failed to install npm CLI packages for the sandbox.\n"
            f"command: {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    (dest / _CACHE_STAMP).write_text(_stamp_text(packages), encoding="utf-8")


def stage_npm(workspace_dir: Path, packages: Sequence[str]) -> Path | None:
    """Copy pinned npm CLIs into *workspace_dir*/.npm-prefix.

    Returns the prefix path, or ``None`` when *packages* is empty.
    """
    pins = _pin_list(packages)
    if not pins:
        return None
    set_id = npm_set_id(pins)
    cache = _NPM_CACHE / set_id
    dest = npm_dir(workspace_dir)
    if dest.exists():
        shutil.rmtree(dest)
    stamp = _stamp_text(pins)
    with host_file_lock(_NPM_CACHE / f".{set_id}.lock"):
        cached_stamp = cache / _CACHE_STAMP
        if (
            not cached_stamp.is_file()
            or cached_stamp.read_text(encoding="utf-8") != stamp
        ):
            if cache.exists():
                shutil.rmtree(cache)
            _npm_install(cache, pins)
        # Keep npm's relative bin shims. Dereferencing them copies ESM
        # entrypoints into ``bin/``, where Node cannot resolve the package
        # tree (``dsh`` then fails with ERR_MODULE_NOT_FOUND).
        shutil.copytree(
            cache,
            dest,
            symlinks=True,
            ignore=shutil.ignore_patterns(_CACHE_STAMP),
        )
    return dest
