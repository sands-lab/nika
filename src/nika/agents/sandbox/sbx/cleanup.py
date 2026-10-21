"""Best-effort Docker Sandbox cleanup for session teardown.

Used when the agent worker was hard-killed (case timeout, Ctrl+C SIGKILL)
and never reached ``SandboxManager.open_session``'s ``finally`` block.
Parent-side ``close_session`` owns this path (via ``SbxBackend.cleanup_session``).
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Mapping
from typing import Any

from nika.agents.sandbox.backend import SandboxCleanupResult

logger = logging.getLogger(__name__)


def cleanup_sbx_for_session(
    session_meta: Mapping[str, Any] | None,
) -> SandboxCleanupResult | None:
    """Remove the sbx sandbox (and opaque workspace) owned by *session_meta*.

    Only targets ``sanitize_sandbox_name(agent_session_id)`` for this session.
    Never lists or removes other sandboxes, so concurrent benchmark runs are
    left alone.

    Returns a result when a sandbox name could be derived, else ``None``.
    """
    if not session_meta:
        return None

    from nika.agents.sandbox.sbx.client import run_sbx_optional, sbx_available
    from nika.agents.sandbox.sbx.policy import sanitize_sandbox_name
    from nika.agents.sandbox.workspace import opaque_agent_workspace_dir
    from nika.utils.agent_session_id import resolve_agent_session_id

    agent_sid = resolve_agent_session_id(session_meta)
    if not agent_sid:
        return None

    sandbox_name = sanitize_sandbox_name(agent_sid)
    workspace = opaque_agent_workspace_dir(agent_sid)
    removed_sandbox = False
    removed_workspace = False

    if sbx_available():
        proc = run_sbx_optional(["rm", "--force", sandbox_name])
        combined = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()
        combined_l = combined.lower()
        missing = any(
            marker in combined_l
            for marker in ("not found", "no such", "does not exist", "unknown")
        )
        if proc.returncode == 0:
            # sbx often exits 0 for an already-gone name; only treat clearly
            # affirmative output as "we removed something" for logging.
            removed_sandbox = any(
                marker in combined_l
                for marker in ("removed", "deleted", "destroy", "stopped")
            )
        elif not missing:
            logger.warning(
                "sbx rm --force %s failed (code %s): %s",
                sandbox_name,
                proc.returncode,
                combined or "(no output)",
            )

    if workspace.is_dir():
        shutil.rmtree(workspace, ignore_errors=True)
        removed_workspace = not workspace.exists()

    return SandboxCleanupResult(
        sandbox_name=sandbox_name,
        removed_sandbox=removed_sandbox,
        removed_workspace=removed_workspace,
    )
