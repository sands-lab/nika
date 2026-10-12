"""Ephemeral per-task workspace preparation and artifact collection."""

from __future__ import annotations

import json
import os
import shutil
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from agent.sandbox.constants import (
    MANIFEST_FILENAME,
    RUN_FILENAME,
    RUNTIME_ENV_FILENAME,
)
from agent.protocols import DIAGNOSIS, PHASES
from agent.utils.loggers import MESSAGES_FILENAME
from agent.utils.skills import resolve_skills_root, skills_enabled
from nika.config import RUNTIME_DIR

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


def open_workspace_file(path: Path) -> BinaryIO:
    """Open an agent-writable workspace file for reading on the host.

    The agent can replace any file in its workspace, including with a symlink
    to a host path or a FIFO. Refuse both, so host reads only ever see a
    regular file the sandbox wrote. *path* must sit directly in the workspace
    root (the mount point), which the agent cannot replace.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    handle = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        raise OSError(f"Not a regular file: {path}")
    return handle


def read_workspace_text(path: Path) -> str:
    """Read an agent-writable workspace file (see :func:`open_workspace_file`)."""
    with open_workspace_file(path) as handle:
        return handle.read().decode("utf-8")


def write_workspace_text(path: Path, text: str) -> None:
    """Write a host file into the workspace without following a planted symlink.

    Removes whatever the agent left at *path* (unlinking a symlink, not its
    target) and creates the file exclusively, so a symlink recreated in
    between makes the write fail instead of redirecting it.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


@dataclass
class SandboxWorkspace:
    session_dir: Path
    workspace_dir: Path

    @property
    def manifest_path(self) -> Path:
        return self.workspace_dir / MANIFEST_FILENAME


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
            try:
                handle = open_workspace_file(self._source)
            except OSError:
                # Missing yet, or not a regular file the sandbox wrote.
                return
            with handle:
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
    agent_session_id: str,
    workspace_dir: str | Path | None = None,
) -> SandboxWorkspace:
    """Create an isolated workspace with manifest, skills, and runtime env.

    The workspace defaults to ``runtime/agent_workspaces/{agent_session_id}/``
    so the bind-mount path does not leak the human-readable trial dirname.

    The host keeps writing ``messages.jsonl`` in *session_dir*: CLI workers log
    there directly, and in-VM SDK traces reach it through :class:`TraceMirror`.
    """
    session_path = Path(session_dir).resolve()
    workspace = (
        Path(workspace_dir).resolve()
        if workspace_dir is not None
        else opaque_agent_workspace_dir(agent_session_id)
    )
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
            agent_session_id=agent_session_id,
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
    try:
        manifest = read_workspace_text(workspace.manifest_path)
    except OSError:
        return
    (workspace.session_dir / MANIFEST_FILENAME).write_text(manifest, encoding="utf-8")


def cleanup_workspace(workspace: SandboxWorkspace) -> None:
    """Remove the ephemeral workspace directory."""
    if workspace.workspace_dir.is_dir():
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)
