import os
from pathlib import Path

from dotenv import load_dotenv

# config.py lives at <repo>/src/nika/config.py
_PKG_DIR = Path(__file__).resolve().parent
REPO_ROOT = _PKG_DIR.parent.parent
_REPO_ROOT = REPO_ROOT  # backward-compatible alias

# MCP servers are spawned as subprocesses with an unrelated cwd; load .env from repo root.
load_dotenv(REPO_ROOT / ".env")

RUNTIME_DIR = REPO_ROOT / "runtime"
SESSIONS_DIR = RUNTIME_DIR / "sessions"
SESSIONS_DB = RUNTIME_DIR / "sessions.db"
BENCHMARK_RUNS_DIR = RUNTIME_DIR / "benchmark_runs"
RESULTS_DIR = REPO_ROOT / "results"
BENCHMARK_DIR = REPO_ROOT / "benchmark"
MCP_SERVER_DIR = _PKG_DIR / "service" / "mcp_server"

ENV_RESULT_DIR = "NIKA_RESULT_DIR"


def resolve_results_root(result_dir: str | Path | None = None) -> Path:
    """Return the directory under which session folders are created.

    Precedence: explicit *result_dir* (CLI) → ``NIKA_RESULT_DIR`` in ``.env`` → ``RESULTS_DIR``.
    Relative paths resolve from the repository root.
    """
    raw = (str(result_dir).strip() if result_dir is not None else "") or os.environ.get(
        ENV_RESULT_DIR, ""
    ).strip()
    if not raw:
        return RESULTS_DIR
    path = Path(raw)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def pkg_path(*parts: str) -> Path:
    """Return a path under the nika package root."""
    return _PKG_DIR.joinpath(*parts)
