#!/usr/bin/env python3
"""Stage a portable Kubernetes MCP runtime into the k8s_lab controller tree.

rancher/k3s is scratch-like (no system Python / glibc loader). This script
builds a managed CPython + libs + mcp/kubernetes tree, then packs it into a
single ``bundle.tar.gz`` that Kathara copies quickly. ``controller.startup``
extracts and launches it.

Usage:
    uv run python scripts/stage_k8s_mcp_bundle.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PKG = REPO_ROOT / "src" / "nika" / "service" / "k8s_mcp_server"
STAGE_ROOT = (
    REPO_ROOT
    / "src"
    / "nika"
    / "net_env"
    / "kathara"
    / "kubernetes"
    / "k8s_lab"
    / "controller"
    / "opt"
    / "nika-k8s-mcp"
)
BUILD_ROOT = REPO_ROOT / "artifacts" / "nika-k8s-mcp-build"
RUNTIME_DIR = BUILD_ROOT / "runtime"
LIB_DIR = BUILD_ROOT / "lib"
PKGS_DIR = BUILD_ROOT / "pkgs"
SERVER_DST = PKGS_DIR / "nika" / "service" / "k8s_mcp_server"
BUNDLE_TAR = STAGE_ROOT / "bundle.tar.gz"

PYTHON_KEY = "cpython-3.12.12-linux-x86_64-gnu"
DEPS = (
    "mcp==1.28.0",
    "kubernetes>=31.0.0",
    "uvicorn>=0.30.0",
    "starlette>=0.38.0",
    "httpx>=0.27.0",
)


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=REPO_ROOT, env=env)


def _resolve_managed_python() -> Path:
    env = {**os.environ, "UV_PYTHON_PREFERENCE": "only-managed"}
    _run(["uv", "python", "install", PYTHON_KEY], env=env)
    managed_root = Path.home() / ".local" / "share" / "uv" / "python" / PYTHON_KEY
    for name in ("python3.12", "python3", "python"):
        candidate = managed_root / "bin" / name
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError(f"Managed Python not found under {managed_root}")


def _copy_portable_python(python_bin: Path) -> Path:
    install_root = python_bin.parent.parent
    if RUNTIME_DIR.exists():
        shutil.rmtree(RUNTIME_DIR)
    shutil.copytree(
        install_root,
        RUNTIME_DIR,
        symlinks=False,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    staged_bin = RUNTIME_DIR / "bin" / python_bin.name
    if not staged_bin.exists():
        candidates = sorted((RUNTIME_DIR / "bin").glob("python3*"))
        if not candidates:
            raise RuntimeError(f"No python binary under {RUNTIME_DIR / 'bin'}")
        staged_bin = candidates[0]
    return staged_bin


def _bundle_shared_libs(python_bin: Path) -> Path:
    if LIB_DIR.exists():
        shutil.rmtree(LIB_DIR)
    LIB_DIR.mkdir(parents=True)

    ldd = subprocess.run(
        ["ldd", str(python_bin)],
        check=True,
        capture_output=True,
        text=True,
    )
    linker: Path | None = None
    for line in ldd.stdout.splitlines():
        line = line.strip()
        if "ld-linux" in line and "=>" not in line:
            path = Path(line.split()[0])
            if path.is_file():
                linker = path
                shutil.copy2(path, LIB_DIR / path.name, follow_symlinks=True)
            continue
        if "=>" not in line:
            continue
        rhs = line.split("=>", 1)[1].strip().split()[0]
        if not rhs.startswith("/"):
            continue
        src = Path(rhs)
        if src.is_file():
            shutil.copy2(src, LIB_DIR / src.name, follow_symlinks=True)

    if linker is None:
        default_linker = Path("/lib64/ld-linux-x86-64.so.2")
        if default_linker.is_file():
            linker = default_linker
            shutil.copy2(
                default_linker, LIB_DIR / default_linker.name, follow_symlinks=True
            )
    if linker is None:
        raise RuntimeError("Could not locate ld-linux dynamic linker via ldd")
    return LIB_DIR / linker.name


def _collect_extension_libs(pkgs_dir: Path) -> None:
    for so_path in pkgs_dir.rglob("*.so"):
        try:
            ldd = subprocess.run(
                ["ldd", str(so_path)],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            continue
        for line in ldd.stdout.splitlines():
            if "=>" not in line:
                continue
            rhs = line.split("=>", 1)[1].strip().split()[0]
            if not rhs.startswith("/"):
                continue
            src = Path(rhs)
            dest = LIB_DIR / src.name
            if src.is_file() and not dest.exists():
                shutil.copy2(src, dest, follow_symlinks=True)


def _install_deps(python_bin: Path) -> None:
    if PKGS_DIR.exists():
        shutil.rmtree(PKGS_DIR)
    PKGS_DIR.mkdir(parents=True)
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python_bin),
            "--target",
            str(PKGS_DIR),
            *DEPS,
        ]
    )
    _collect_extension_libs(PKGS_DIR)


def _copy_server_sources() -> None:
    if not SRC_PKG.is_dir():
        raise FileNotFoundError(f"Missing server package: {SRC_PKG}")
    if SERVER_DST.exists():
        shutil.rmtree(SERVER_DST)
    SERVER_DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        SRC_PKG,
        SERVER_DST,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    for marker in (
        PKGS_DIR / "nika" / "__init__.py",
        PKGS_DIR / "nika" / "service" / "__init__.py",
    ):
        marker.write_text('"""NIKA namespace markers for the staged MCP bundle."""\n')


def _write_scripts(python_bin: Path, linker_name: str) -> None:
    rel_python = Path("runtime") / "bin" / python_bin.name
    start = BUILD_ROOT / "start.sh"
    start.write_text(
        f"""#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
export PYTHONPATH="$ROOT/pkgs${{PYTHONPATH:+:$PYTHONPATH}}"
export KUBECONFIG="${{KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}}"
export NIKA_K8S_APISERVER="${{NIKA_K8S_APISERVER:-https://127.0.0.1:6443}}"
export NIKA_K8S_MCP_BIND="${{NIKA_K8S_MCP_BIND:-0.0.0.0}}"
export NIKA_K8S_MCP_PORT="${{NIKA_K8S_MCP_PORT:-18765}}"
exec "$ROOT/lib/{linker_name}" --library-path "$ROOT/lib" \\
  "$ROOT/{rel_python}" -m nika.service.k8s_mcp_server.main \\
  --host "$NIKA_K8S_MCP_BIND" --port "$NIKA_K8S_MCP_PORT"
"""
    )
    start.chmod(0o755)

    health = BUILD_ROOT / "healthcheck.sh"
    health.write_text(
        f"""#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
exec "$ROOT/lib/{linker_name}" --library-path "$ROOT/lib" \\
  "$ROOT/{rel_python}" -c \\
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:18765/health', timeout=3).read().decode())"
"""
    )
    health.chmod(0o755)


def _pack_into_controller_tree() -> None:
    STAGE_ROOT.mkdir(parents=True, exist_ok=True)
    # Remove expanded trees from the Kathara machine dir (slow multi-file copy).
    for name in ("runtime", "lib", "pkgs"):
        path = STAGE_ROOT / name
        if path.exists():
            shutil.rmtree(path)
    if BUNDLE_TAR.exists():
        BUNDLE_TAR.unlink()

    with tarfile.open(BUNDLE_TAR, "w:gz") as tar:
        for name in ("runtime", "lib", "pkgs", "start.sh", "healthcheck.sh"):
            src = BUILD_ROOT / name
            tar.add(src, arcname=name)

    bootstrap = STAGE_ROOT / "extract_and_start.sh"
    bootstrap.write_text(
        """#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
if [ ! -x "$ROOT/start.sh" ] || [ ! -d "$ROOT/runtime" ]; then
  tar -xzf "$ROOT/bundle.tar.gz" -C "$ROOT"
  chmod +x "$ROOT/start.sh" "$ROOT/healthcheck.sh" 2>/dev/null || true
fi
exec "$ROOT/start.sh"
"""
    )
    bootstrap.chmod(0o755)

    health_wrapper = STAGE_ROOT / "healthcheck.sh"
    health_wrapper.write_text(
        """#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")" && pwd)"
if [ ! -x "$ROOT/lib/ld-linux-x86-64.so.2" ]; then
  echo "bundle not extracted" >&2
  exit 1
fi
PY=
for cand in python3.12 python3 python; do
  if [ -x "$ROOT/runtime/bin/$cand" ]; then PY="$ROOT/runtime/bin/$cand"; break; fi
done
if [ -z "$PY" ]; then
  echo "no bundled python" >&2
  exit 1
fi
exec "$ROOT/lib/ld-linux-x86-64.so.2" --library-path "$ROOT/lib" "$PY" -c \\
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:18765/health', timeout=3).read().decode())"
"""
    )
    health_wrapper.chmod(0o755)

    (STAGE_ROOT / "README.md").write_text(
        "# Staged Kubernetes MCP bundle\n\n"
        "Generated by `scripts/stage_k8s_mcp_bundle.py`.\n"
        "Kathara copies `bundle.tar.gz` + bootstrap scripts; "
        "controller.startup extracts and launches the server.\n"
    )


def main() -> int:
    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    managed_python = _resolve_managed_python()
    print(f"Using managed python: {managed_python}", flush=True)
    staged_python = _copy_portable_python(managed_python)
    print(f"Build runtime python: {staged_python}", flush=True)
    linker = _bundle_shared_libs(staged_python)
    print(f"Bundled dynamic linker: {linker}", flush=True)
    _install_deps(staged_python)
    _copy_server_sources()
    _write_scripts(staged_python, linker.name)
    _pack_into_controller_tree()
    size_mb = BUNDLE_TAR.stat().st_size / (1024 * 1024)
    print(f"Packed {BUNDLE_TAR} ({size_mb:.1f} MiB)", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"stage failed: {exc}", file=sys.stderr)
        raise SystemExit(exc.returncode) from exc
