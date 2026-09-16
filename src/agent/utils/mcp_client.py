"""Shared MCP client helpers for troubleshooting agents."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from agent.sandbox.config import (
    ENV_GATEWAY_AGENT_URL,
    ENV_GATEWAY_URL,
    ENV_SANDBOX_EXECUTION,
    ENV_SESSION_DIR,
)
from agent.sandbox.manifest import manifest_mcp_servers
from agent.protocols import DIAGNOSIS, SUBMISSION
from agent.utils.loggers import MESSAGES_FILENAME

SESSION_HEADER = "NIKA-Session-Id"


def load_session_mcp_config(
    session_id: str,
    scenario_name: str,
    *,
    backend: str | None = None,
    session_dir: str | Path | None = None,
) -> dict:
    """Return session-scoped HTTP MCP config (phase filtering is gateway-side)."""
    if session_dir is not None:
        baked = manifest_mcp_servers(session_dir)
        if baked is not None:
            return baked
    if os.environ.get(ENV_SANDBOX_EXECUTION) == "1":
        session_dir = os.environ.get(ENV_SESSION_DIR, "").strip()
        if session_dir:
            baked = manifest_mcp_servers(session_dir)
            if baked is not None:
                return baked
        if backend is None:
            backend = os.environ.get("NIKA_SESSION_BACKEND", "").strip() or None
    from agent.utils.mcp_servers import MCPServerConfig

    return MCPServerConfig(session_id=session_id).load_session_http_config(
        scenario_name,
        backend=backend,
    )


def _gateway_base_for_phase_advance() -> str:
    if os.environ.get(ENV_SANDBOX_EXECUTION) == "1":
        agent_url = os.environ.get(ENV_GATEWAY_AGENT_URL, "").strip().rstrip("/")
        if agent_url:
            return agent_url
    return os.environ.get(ENV_GATEWAY_URL, "").strip().rstrip("/")


def _freeze_diagnosis_in_workspace(report: str) -> None:
    """Append diagnosis_frozen to workspace messages.jsonl (no ``nika`` needed)."""
    session_dir = os.environ.get(ENV_SESSION_DIR, "").strip() or "."
    path = Path(session_dir) / MESSAGES_FILENAME
    if path.is_file():
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "diagnosis_frozen":
                return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": datetime.now().isoformat(),
                    "phase": DIAGNOSIS,
                    "event": "diagnosis_frozen",
                    "report": report,
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def _http_advance_submission_phase(
    session_id: str,
    base: str,
    *,
    diagnosis_report: str = "",
) -> None:
    """Freeze (when report provided) and advance phase on the host gateway."""
    url = f"{base}/gateway/sessions/{session_id}/phase"
    payload: dict[str, str] = {"phase": SUBMISSION}
    if diagnosis_report:
        payload["diagnosis_report"] = diagnosis_report
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            SESSION_HEADER: session_id,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"MCP phase advance failed with HTTP {response.status}"
                )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"MCP phase advance failed: HTTP {exc.code}: {body}"
        ) from exc


def begin_submission_mcp_phase(session_id: str, diagnosis_report: str = "") -> None:
    """Freeze diagnosis and advance the gateway before the submission step.

    Sandbox agents (no host ``nika`` / no shared process with the gateway) record
    the freeze in the workspace transcript for collection, then POST the report
    to the host gateway so ``submit()`` can read the host session trajectory.
    Host-side callers freeze locally via ``freeze_diagnosis`` and advance either
    in-process or over HTTP.
    """
    from agent.utils.mcp_servers import agent_facing_mcp_session_id

    # Agents / HTTP paths use the opaque handle; freeze/advance accept either key.
    mcp_session_id = agent_facing_mcp_session_id(session_id)

    if os.environ.get(ENV_SANDBOX_EXECUTION) == "1":
        # Never import host ``nika`` from the sandbox: freeze must go through the
        # gateway so the host trajectory is updated without mounting session_dir.
        _freeze_diagnosis_in_workspace(diagnosis_report)
        base = _gateway_base_for_phase_advance()
        if not base:
            raise RuntimeError(
                f"{ENV_GATEWAY_URL} / {ENV_GATEWAY_AGENT_URL} is not set for "
                "MCP phase advance."
            )
        _http_advance_submission_phase(
            mcp_session_id, base, diagnosis_report=diagnosis_report
        )
        return

    from nika.workflows.agent.submission import freeze_diagnosis

    freeze_diagnosis(mcp_session_id, diagnosis_report)
    base = _gateway_base_for_phase_advance()
    use_http = False
    try:
        from nika.remote.config import is_remote_enabled

        use_http = is_remote_enabled()
    except Exception:  # noqa: BLE001 - remote package optional at import time
        use_http = False

    if use_http:
        if not base:
            raise RuntimeError(
                f"{ENV_GATEWAY_URL} / {ENV_GATEWAY_AGENT_URL} is not set for "
                "MCP phase advance."
            )
        # Host already froze; omit report so the gateway accepts an idempotent advance.
        _http_advance_submission_phase(mcp_session_id, base)
        return

    from nika.mcp.gateway.phase import advance_mcp_phase

    advance_mcp_phase(mcp_session_id, SUBMISSION)
