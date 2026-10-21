import os
import stat
import sys
from pathlib import Path

from dotenv import load_dotenv

# config.py lives at <repo>/src/nika/config.py
_PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = _PKG_DIR.parent.parent

# MCP servers are spawned as subprocesses with an unrelated cwd; load .env from repo root.
load_dotenv(REPO_ROOT / ".env")

# install.sh puts clab and gnmic next to this interpreter. Find them there,
# ahead of older host copies, even when the venv is not activated.
_ENV_BIN = Path(sys.prefix) / "bin"
if sys.prefix != sys.base_prefix and str(_ENV_BIN) not in os.environ.get(
    "PATH", ""
).split(os.pathsep):
    os.environ["PATH"] = os.pathsep.join([str(_ENV_BIN), os.environ.get("PATH", "")])

RUNTIME_DIR = REPO_ROOT / "runtime"
SESSIONS_DIR = RUNTIME_DIR / "sessions"
SESSIONS_DB = RUNTIME_DIR / "sessions.db"
BENCHMARK_RUNS_DIR = RUNTIME_DIR / "benchmark_runs"
# Containerlab working directories. Containerlab exposes the topology and lab
# paths to every node (CLAB_LABEL_* variables), so they live outside the
# checkout, on local disk, under a name that does not identify NIKA. The path is
# fixed (not $TMPDIR), so every process of the user finds the same labs.
CLAB_RUNTIME_DIR = Path("/tmp") / f"labs-{os.getuid()}"


def clab_runtime_root(*, create: bool = False) -> Path | None:
    """:data:`CLAB_RUNTIME_DIR` if it is a private directory of this user.

    Another local user could pre-create the predictable path, or make it a
    symlink; NIKA neither writes there nor deletes through it. With *create*,
    make it (mode 0700) when missing and raise when it is not ours; otherwise
    return ``None`` for a missing or foreign path.
    """
    root = CLAB_RUNTIME_DIR
    if create:
        root.mkdir(mode=0o700, exist_ok=True)
    try:
        info = root.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
        return root
    if create:
        raise RuntimeError(f"{root} is not a directory owned by this user.")
    return None


def clab_workdir(lab_name: str) -> Path:
    """Create and return the Containerlab working directory of *lab_name*."""
    root = clab_runtime_root(create=True)
    assert root is not None
    workdir = root / lab_name
    workdir.mkdir(exist_ok=True)
    return workdir


RESULTS_DIR = REPO_ROOT / "results"
BENCHMARK_DIR = REPO_ROOT / "benchmark"
# Current nika-bench release. Locally built nika/* images are tagged with it.
BENCHMARK_VERSION = "0.2.0"

# Migration recognizes this legacy name. Runtime result paths come from YAML or CLI.
ENV_RESULT_DIR = "NIKA_RESULT_DIR"


def resolve_results_root(result_dir: str | Path | None = None) -> Path:
    """Return the directory under which session folders are created.

    Precedence: explicit *result_dir* (CLI) → ``nika.result_dir`` in run config →
    ``results/`` at the repository root. Relative paths resolve from the repo root.
    """
    raw = str(result_dir).strip() if result_dir is not None else ""
    if not raw:
        try:
            from nika.run_config.loader import get_run_config

            raw = (get_run_config().nika.result_dir or "").strip()
        except Exception:
            raw = ""
    if not raw:
        return RESULTS_DIR
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def pkg_path(*parts: str) -> Path:
    """Return a path under the nika package root."""
    return _PKG_DIR.joinpath(*parts)
