"""Ephemeral per-task workspace preparation and artifact collection."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from agent.sandbox.constants import (
    MANIFEST_FILENAME,
    RUN_FILENAME,
    RUNTIME_ENV_FILENAME,
)
from agent.utils.skills import resolve_skills_root

SANDBOX_RUN_DIRNAME = ".sandbox_run"
SKILLS_DIRNAME = "skills"
# Standardized session artifacts only — agent CLI/SDK workspaces stay ephemeral.
COLLECTED_FILES = ("messages.jsonl", "nika.jsonl", "submission.json")
# Host run.json keeps eval fields (problem_names, failure_domain). The sandbox
# copy is an isolation boundary: only Session metadata agents need in-VM.
SANDBOX_RUN_JSON_ALLOWLIST = frozenset(
    {
        "session_id",
        "scenario_name",
        "backend",
        "status",
    }
)


@dataclass
class SandboxWorkspace:
    session_dir: Path
    workspace_dir: Path

    @property
    def manifest_path(self) -> Path:
        return self.workspace_dir / MANIFEST_FILENAME


def sandbox_workspace_dir(session_dir: str | Path) -> Path:
    return Path(session_dir).resolve() / SANDBOX_RUN_DIRNAME


def _write_sandbox_run_json(run_src: Path, run_dst: Path) -> None:
    """Write allowlisted session meta for the sandbox workspace."""
    raw = json.loads(run_src.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected object in {run_src}, got {type(raw).__name__}")
    filtered = {key: raw[key] for key in SANDBOX_RUN_JSON_ALLOWLIST if key in raw}
    run_dst.write_text(json.dumps(filtered, indent=2), encoding="utf-8")


def prepare_workspace(
    *,
    session_dir: str | Path,
    manifest: dict,
    runtime_env: dict[str, str],
) -> SandboxWorkspace:
    """Create an isolated workspace with manifest, skills, and runtime env."""
    session_path = Path(session_dir).resolve()
    workspace = sandbox_workspace_dir(session_path)
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    (workspace / MANIFEST_FILENAME).write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    (workspace / RUNTIME_ENV_FILENAME).write_text(
        json.dumps(runtime_env, indent=2),
        encoding="utf-8",
    )

    # Agents see this workspace as NIKA_SESSION_DIR; write only allowlisted
    # run.json fields so injected failure labels stay on the host.
    run_src = session_path / RUN_FILENAME
    if run_src.is_file():
        _write_sandbox_run_json(run_src, workspace / RUN_FILENAME)

    skills_src = resolve_skills_root()
    skills_dst = workspace / SKILLS_DIRNAME
    if skills_src.is_dir():
        shutil.copytree(skills_src, skills_dst, dirs_exist_ok=True)

    return SandboxWorkspace(session_dir=session_path, workspace_dir=workspace)


def collect_artifacts(workspace: SandboxWorkspace) -> None:
    """Copy standardized agent outputs from the sandbox workspace to the session dir.

    Agent workspaces (``codex_workspace``, ``claude_workspace``, SDK variants)
    are intentionally not retained — same session layout as BYO agents.
    """
    session_dir = workspace.session_dir
    for name in COLLECTED_FILES:
        src = workspace.workspace_dir / name
        if src.is_file():
            shutil.copy2(src, session_dir / name)

    manifest_src = workspace.manifest_path
    if manifest_src.is_file():
        shutil.copy2(manifest_src, session_dir / MANIFEST_FILENAME)


def cleanup_workspace(workspace: SandboxWorkspace) -> None:
    """Remove the ephemeral workspace directory."""
    if workspace.workspace_dir.is_dir():
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)
