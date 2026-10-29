"""Run a frozen release on the NIKA commit that originally executed it.

Current NIKA lists and validates release YAML. Releases that pin ``orchestrator``
in ``RELEASE.yaml`` execute in a checkout of that git tag or commit, not in the
current tree.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from nika.config import CACHE_DIR, REPO_ROOT, resolve_results_root
from nika.run_config.loader import ENV_RUN_CONFIG

OFFICIAL_REMOTE = "https://github.com/sands-lab/nika.git"
ORCHESTRATORS_DIR = CACHE_DIR / "orchestrators"
_GIT_TIMEOUT_SEC = 120
_SYNC_TIMEOUT_SEC = 600


def _release_error(message: str) -> Exception:
    from nika.workflows.benchmark.release import ReleaseError

    return ReleaseError(message)


def orchestrator_ref(manifest: dict[str, Any] | None) -> str | None:
    """Return the git tag or commit pinned on a ``RELEASE.yaml`` mapping."""
    if not manifest:
        return None
    raw = manifest.get("orchestrator")
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def uses_foreign_orchestrator(manifest: dict[str, Any] | None) -> bool:
    """True when this tree is not the pinned executor for the release."""
    return orchestrator_ref(manifest) is not None and not _is_current_orchestrator(
        manifest
    )


def _git_env() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def _git_commit(cwd: Path, rev: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", f"{rev}^{{commit}}"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SEC,
            env=_git_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha if len(sha) == 40 else None


def _is_current_orchestrator(manifest: dict[str, Any] | None) -> bool:
    ref = orchestrator_ref(manifest)
    if ref is None:
        return True
    head = _git_commit(REPO_ROOT, "HEAD")
    pinned = _git_commit(REPO_ROOT, ref)
    return head is not None and pinned is not None and head == pinned


def _looks_like_commit_sha(ref: str) -> bool:
    return len(ref) == 40 and all(c in "0123456789abcdef" for c in ref.lower())


def resolve_orchestrator_commit(ref: str) -> str:
    """Resolve ``ref`` (tag or SHA) to a 40-character commit."""
    sha = _git_commit(REPO_ROOT, ref)
    if sha:
        return sha
    if _looks_like_commit_sha(ref):
        return ref.lower()
    try:
        proc = subprocess.run(
            [
                "git",
                "ls-remote",
                OFFICIAL_REMOTE,
                f"refs/tags/{ref}",
                f"refs/tags/{ref}^{{}}",
            ],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SEC,
            env=_git_env(),
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _release_error(
            f"cannot resolve orchestrator {ref!r}: {exc}. "
            f"Create git tag {ref} or fetch {OFFICIAL_REMOTE}."
        ) from exc
    refs: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        commit, _, name = line.partition("\t")
        if name:
            refs[name.strip()] = commit.strip()
    sha = refs.get(f"refs/tags/{ref}^{{}}") or refs.get(f"refs/tags/{ref}")
    if sha:
        return sha
    raise _release_error(
        f"orchestrator {ref!r} is not a local git commit and {OFFICIAL_REMOTE} "
        f"has no tag of that name. Tag the executor with "
        f"`git tag -a {ref} <commit>` and fetch it."
    )


def _remove_checkout(dest: Path) -> None:
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(dest)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SEC,
            env=_git_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)


def materialize_orchestrator(commit: str) -> Path:
    """Return a checkout of ``commit``, creating a worktree or clone if needed."""
    dest = ORCHESTRATORS_DIR / commit
    if (dest / "pyproject.toml").is_file() and _git_commit(dest, "HEAD") == commit:
        _ensure_project_env(dest)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    _remove_checkout(dest)
    if _git_commit(REPO_ROOT, commit):
        try:
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(dest), commit],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SEC,
                env=_git_env(),
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = ""
            if isinstance(exc, subprocess.CalledProcessError):
                detail = (exc.stderr or exc.stdout or "").strip()
            raise _release_error(
                f"cannot create orchestrator worktree at {dest} for {commit[:12]}: "
                f"{detail or exc}"
            ) from exc
    else:
        _clone_official(dest, commit)
    _ensure_project_env(dest)
    return dest


def _clone_official(dest: Path, commit: str) -> None:
    try:
        subprocess.run(
            ["git", "clone", "--no-checkout", OFFICIAL_REMOTE, str(dest)],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SEC,
            env=_git_env(),
            check=True,
        )
        subprocess.run(
            ["git", "fetch", "--depth", "1", "origin", commit],
            cwd=dest,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SEC,
            env=_git_env(),
            check=True,
        )
        subprocess.run(
            ["git", "checkout", commit],
            cwd=dest,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SEC,
            env=_git_env(),
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or exc.stdout or "").strip()
        raise _release_error(
            f"cannot clone orchestrator {commit[:12]} from {OFFICIAL_REMOTE}: "
            f"{detail or exc}"
        ) from exc


def _ensure_project_env(root: Path) -> None:
    if (root / ".venv" / "bin" / "nika").is_file():
        return
    uv = shutil.which("uv")
    if not uv:
        raise _release_error(
            f"uv is required to install the orchestrator checkout at {root}"
        )
    try:
        subprocess.run(
            [uv, "sync"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_SYNC_TIMEOUT_SEC,
            env=_git_env(),
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = ""
        if isinstance(exc, subprocess.CalledProcessError):
            detail = (exc.stderr or exc.stdout or "").strip()
        raise _release_error(
            f"uv sync failed in orchestrator checkout {root}: {detail or exc}"
        ) from exc


def _delegate_argv(argv: list[str]) -> list[str]:
    """Drop current-tree ``--run-config`` and make ``--result_dir`` absolute."""
    out: list[str] = []
    raw_result: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--run-config":
            i += 2
            continue
        if arg.startswith("--run-config="):
            i += 1
            continue
        if arg == "--result_dir" and i + 1 < len(argv):
            raw_result = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--result_dir="):
            raw_result = arg.split("=", 1)[1]
            i += 1
            continue
        out.append(arg)
        i += 1
    out.extend(["--result_dir", str(resolve_results_root(raw_result))])
    return out


def delegate_release_run(manifest: dict[str, Any], *, label: str) -> int | None:
    """Re-exec this CLI in the pinned checkout. Return None to run locally."""
    if not uses_foreign_orchestrator(manifest):
        return None
    ref = orchestrator_ref(manifest)
    assert ref is not None
    commit = resolve_orchestrator_commit(ref)
    root = materialize_orchestrator(commit)
    uv = shutil.which("uv")
    if not uv:
        raise _release_error("uv is required to run a pinned release orchestrator")
    print(
        f"Release {label} runs on orchestrator {ref} ({commit[:12]}) at {root}",
        file=sys.stderr,
    )
    env = _git_env()
    env.pop("VIRTUAL_ENV", None)
    # Current nika.yaml can have fields the pinned schema rejects.
    env.pop(ENV_RUN_CONFIG, None)
    orch_cfg = root / "config" / "nika.yaml"
    if orch_cfg.is_file():
        env[ENV_RUN_CONFIG] = str(orch_cfg)
    cmd = [
        uv,
        "run",
        "--project",
        str(root),
        "nika",
        *_delegate_argv(list(sys.argv[1:])),
    ]
    return subprocess.call(cmd, cwd=str(REPO_ROOT), env=env)
