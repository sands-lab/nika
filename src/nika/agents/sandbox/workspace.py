"""Ephemeral per-task workspace preparation and artifact collection."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import BinaryIO

from agent.protocols import DIAGNOSIS, PHASES
from agent.runtime.contract import (
    RUNTIME_ENV_FILENAME,
    SESSION_TOKEN_HEADER,
    TASK_FILENAME,
)
from agent.utils.loggers import MESSAGES_FILENAME
from agent.utils.skills import (
    DEFAULT_SKILLS_ROOT,
    SKILLS_DIRNAME,
    TEST_SKILL_NAME,
    resolve_test_skill_dir,
)
from nika.agents.sandbox.constants import MANIFEST_FILENAME

logger = logging.getLogger(__name__)

# Host-only live-test override. Never copied into the sandbox.
TEST_INCLUDE_SKILL_ENV = "NIKA_TEST_INCLUDE_SKILL"

# Freeze events are host-written; a sandbox copy could replace the report that
# ``submit()`` and the evaluator read.
_HOST_ONLY_EVENTS = frozenset({"diagnosis_frozen"})
_BOOKEND_EVENTS = frozenset({"agent_done", "agent_error"})
# The manifest's MCP gateway token. It is dead once the session ends, but
# results are shared, so collected manifests carry a placeholder.
_SESSION_TOKEN_RE = re.compile(rf'("{SESSION_TOKEN_HEADER}":\s*")[^"]*"')


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


def write_workspace_bytes(path: Path, data: bytes) -> None:
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
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def write_workspace_text(path: Path, text: str) -> None:
    """UTF-8 variant of :func:`write_workspace_bytes`."""
    write_workspace_bytes(path, text.encode("utf-8"))


@dataclass
class SandboxWorkspace:
    session_dir: Path
    workspace_dir: Path

    @property
    def task_path(self) -> Path:
        return self.workspace_dir / TASK_FILENAME


def agent_workspaces_root() -> Path:
    """Host directory that holds the agent workspaces.

    Sandboxes may see a workspace at its host path (sbx bind-mounts it), so
    the path must not name the host project or the trial.
    """
    return Path(tempfile.gettempdir()) / f"agent-workspaces-{os.getuid()}"


def opaque_agent_workspace_dir(agent_session_id: str) -> Path:
    """Workspace path that does not embed benchmark case_key / trial_id."""
    return (agent_workspaces_root() / agent_session_id).resolve()


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
        self.refresh: Callable[[], None] | None = None
        self._offset = 0
        self._phase = DIAGNOSIS
        # Phases whose bookends the host wrote itself (stopped at the budget).
        self._host_closed: set[str] = set()
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
            and not (
                event.get("phase") in self._host_closed
                and event.get("event") in _BOOKEND_EVENTS
            )
        )

    def sync(self) -> None:
        """Append complete new workspace lines to the host trace."""
        with self._lock:
            if self.refresh is not None:
                try:
                    self.refresh()
                except Exception as exc:  # noqa: BLE001 - retried on the next poll
                    logger.debug("Trace refresh failed: %s", exc)
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

    def close_phase_on_host(self, phase: str) -> None:
        """Keep the agent's own end of *phase* out; the host records it."""
        with self._lock:
            self._host_closed.add(phase)
        self.sync()

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
            target=self._loop, name="trace-mirror", daemon=True
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
    task_json: str,
    runtime_env: dict[str, str],
    agent_session_id: str,
    enable_skills: bool,
    workspace_dir: str | Path | None = None,
) -> SandboxWorkspace:
    """Create an isolated workspace with the task, skills, and runtime env.

    The workspace defaults to :func:`opaque_agent_workspace_dir` so the
    bind-mount path names neither the host project nor the trial.

    The host keeps writing ``messages.jsonl`` in *session_dir*; the agent's
    trace reaches it through :class:`TraceMirror`.
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

    (workspace / TASK_FILENAME).write_text(task_json, encoding="utf-8")
    (workspace / RUNTIME_ENV_FILENAME).write_text(
        json.dumps(runtime_env, indent=2),
        encoding="utf-8",
    )

    if enable_skills and DEFAULT_SKILLS_ROOT.is_dir():
        shutil.copytree(
            DEFAULT_SKILLS_ROOT, workspace / SKILLS_DIRNAME, dirs_exist_ok=True
        )
        _stage_test_skill(workspace / SKILLS_DIRNAME)

    return SandboxWorkspace(session_dir=session_path, workspace_dir=workspace)


def _include_test_skill() -> bool:
    return os.environ.get(TEST_INCLUDE_SKILL_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _stage_test_skill(skills_root: Path) -> None:
    """Plant the integration-test skill into a host-staged skills tree.

    Live sandbox tests for agents that load the shared library set
    :data:`TEST_INCLUDE_SKILL_ENV`. ``copytree`` follows ``.claude/skills``
    (a symlink in the package), so plant into both the canonical tree and
    that copied Claude discovery path.
    """
    if not _include_test_skill():
        return
    src = resolve_test_skill_dir()
    if src is None:
        return
    dests = [skills_root / "skills" / TEST_SKILL_NAME]
    claude_skills = skills_root / ".claude" / "skills"
    if claude_skills.is_dir() and not claude_skills.is_symlink():
        dests.append(claude_skills / TEST_SKILL_NAME)
    for dest in dests:
        if dest.exists():
            continue
        shutil.copytree(src, dest)


def trace_mirror(workspace: SandboxWorkspace) -> TraceMirror:
    """Mirror for the workspace trace of the sandboxed agent."""
    return TraceMirror(
        workspace.workspace_dir / MESSAGES_FILENAME,
        workspace.session_dir / MESSAGES_FILENAME,
    )


def collect_artifacts(workspace: SandboxWorkspace) -> None:
    """Copy host-trusted outputs from the sandbox workspace to the session dir.

    Only the task is copied (as ``sandbox_manifest.json``). Agent workspaces
    (``codex_workspace``, ``claude_workspace``, SDK variants) are not retained,
    and agent-writable files never replace host files: ``messages.jsonl`` is
    mirrored with :class:`TraceMirror`, and ``submission.json`` is written by
    MCP ``submit()`` on the host after validation.
    """
    try:
        manifest = read_workspace_text(workspace.task_path)
    except OSError:
        return
    write_results_manifest(workspace.session_dir, manifest)


def write_results_manifest(session_dir: Path, manifest: str) -> None:
    """Write the sandbox *manifest* text to *session_dir* without its MCP token."""
    manifest = _SESSION_TOKEN_RE.sub(r'\1***REDACTED***"', manifest)
    (session_dir / MANIFEST_FILENAME).write_text(manifest, encoding="utf-8")


def cleanup_workspace(workspace: SandboxWorkspace) -> None:
    """Remove the ephemeral workspace directory."""
    if workspace.workspace_dir.is_dir():
        shutil.rmtree(workspace.workspace_dir, ignore_errors=True)
