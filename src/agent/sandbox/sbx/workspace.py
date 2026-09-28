"""Ephemeral per-task workspace preparation and artifact collection."""

from __future__ import annotations

import json
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from agent.sandbox.constants import (
    MANIFEST_FILENAME,
    RUN_FILENAME,
    RUNTIME_ENV_FILENAME,
)
from agent.protocols import DIAGNOSIS, PHASES
from agent.utils.loggers import MESSAGES_FILENAME
from agent.utils.skills import resolve_skills_root, skills_enabled
from nika.config import RUNTIME_DIR

SANDBOX_RUN_DIRNAME = ".sandbox_run"
AGENT_WORKSPACES_DIRNAME = "agent_workspaces"
SKILLS_DIRNAME = "skills"
# Freeze events are host-written; a sandbox copy could replace the report that
# ``submit()`` and the evaluator read.
_HOST_ONLY_EVENTS = frozenset({"diagnosis_frozen"})
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
    """Legacy path under the host trial dir (in-flight / tests may still use)."""
    return Path(session_dir).resolve() / SANDBOX_RUN_DIRNAME


def opaque_agent_workspace_dir(agent_session_id: str) -> Path:
    """Workspace path that does not embed benchmark case_key / trial_id."""
    return (RUNTIME_DIR / AGENT_WORKSPACES_DIRNAME / agent_session_id).resolve()


def _write_sandbox_run_json(
    run_src: Path,
    run_dst: Path,
    *,
    agent_session_id: str,
) -> None:
    """Write allowlisted session meta for the sandbox workspace.

    Always emit the opaque ``agent_session_id`` as ``session_id`` so agents never
    see a readable case_key trial id even when the host run.json still has it.
    """
    raw = json.loads(run_src.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"Expected object in {run_src}, got {type(raw).__name__}")
    filtered = {key: raw[key] for key in SANDBOX_RUN_JSON_ALLOWLIST if key in raw}
    filtered["session_id"] = agent_session_id
    run_dst.write_text(json.dumps(filtered, indent=2), encoding="utf-8")


class TraceMirror:
    """Copy an in-sandbox ``messages.jsonl`` into the host session trace.

    In-VM SDK agents write their trace inside the agent-writable workspace. The
    host keeps its own ``messages.jsonl`` and appends the workspace lines as
    they arrive (so inspect can tail it live), dropping host-only events and
    events tagged with a phase other than the one the host is running. Lines
    are read by byte offset, so rewriting already mirrored lines has no effect.
    """

    POLL_SECONDS = 1.0

    def __init__(self, source: Path, dest: Path) -> None:
        self._source = source
        self._dest = dest
        self._offset = 0
        self._phase = DIAGNOSIS
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _keep(self, line: str) -> bool:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return False
        return (
            isinstance(event, dict)
            and event.get("event") not in _HOST_ONLY_EVENTS
            and event.get("phase") == self._phase
        )

    def sync(self) -> None:
        """Append complete new workspace lines to the host trace."""
        with self._lock:
            if not self._source.is_file():
                return
            with self._source.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                if size < self._offset:
                    # Truncated in the sandbox: mirror only what comes next.
                    self._offset = size
                    return
                handle.seek(self._offset)
                chunk = handle.read(size - self._offset)
            end = chunk.rfind(b"\n")
            if end < 0:
                return
            self._offset += end + 1
            lines = chunk[: end + 1].decode("utf-8", errors="replace").splitlines()
            kept = [line for line in lines if line.strip() and self._keep(line)]
            if kept:
                with self._dest.open("a", encoding="utf-8") as out:
                    out.write("\n".join(kept) + "\n")

    def set_phase(self, phase: str) -> None:
        """Finish mirroring the current phase, then accept *phase* events."""
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")
        self.sync()
        with self._lock:
            self._phase = phase

    def _loop(self) -> None:
        while not self._stop.wait(self.POLL_SECONDS):
            self.sync()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="nika-trace-mirror", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self.sync()


def prepare_workspace(
    *,
    session_dir: str | Path,
    manifest: dict,
    runtime_env: dict[str, str],
    agent_session_id: str | None = None,
    workspace_dir: str | Path | None = None,
) -> SandboxWorkspace:
    """Create an isolated workspace with manifest, skills, and runtime env.

    When ``agent_session_id`` is provided (new sessions), the workspace lives under
    ``runtime/agent_workspaces/{agent_session_id}/`` so the bind-mount path does
    not leak the human-readable trial dirname. Legacy callers omit it and keep
    ``{session_dir}/.sandbox_run``.

    The host keeps writing ``messages.jsonl`` in *session_dir*: CLI workers log
    there directly, and in-VM SDK traces reach it through :class:`TraceMirror`.
    """
    session_path = Path(session_dir).resolve()
    opaque = (agent_session_id or "").strip()
    if workspace_dir is not None:
        workspace = Path(workspace_dir).resolve()
    elif opaque:
        workspace = opaque_agent_workspace_dir(opaque)
    else:
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
        _write_sandbox_run_json(
            run_src,
            workspace / RUN_FILENAME,
            agent_session_id=opaque or str(manifest.get("session_id") or ""),
        )

    skills_src = resolve_skills_root()
    skills_dst = workspace / SKILLS_DIRNAME
    if skills_enabled() and skills_src.is_dir():
        shutil.copytree(skills_src, skills_dst, dirs_exist_ok=True)

    # Older runs left a live symlink here; the host trace must be a real file.
    host_trace = session_path / MESSAGES_FILENAME
    if host_trace.is_symlink():
        host_trace.unlink()
    return SandboxWorkspace(session_dir=session_path, workspace_dir=workspace)


def trace_mirror(workspace: SandboxWorkspace) -> TraceMirror:
    """Mirror for the workspace trace of an in-VM SDK agent."""
    return TraceMirror(
        workspace.workspace_dir / MESSAGES_FILENAME,
        workspace.session_dir / MESSAGES_FILENAME,
    )


def collect_artifacts(workspace: SandboxWorkspace) -> None:
    """Copy host-trusted outputs from the sandbox workspace to the session dir.

    Only the manifest is copied. Agent workspaces (``codex_workspace``,
    ``claude_workspace``, SDK variants) are not retained, and agent-writable
    files never replace host files: ``messages.jsonl`` is host-written (or
    mirrored with :class:`TraceMirror`), and ``submission.json`` is written by
    MCP ``submit()`` on the host after validation.
    """
    manifest_src = workspace.manifest_path
    if manifest_src.is_file():
        shutil.copy2(manifest_src, workspace.session_dir / MANIFEST_FILENAME)


def cleanup_workspace(workspace: SandboxWorkspace) -> None:
    """Remove the ephemeral workspace directory."""
    if workspace.workspace_dir.is_dir():
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)
