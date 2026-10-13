"""Shared MCP client helpers for troubleshooting agents."""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path

from agent.sandbox.config import (
    ENV_GATEWAY_URL,
    ENV_SANDBOX_EXECUTION,
    ENV_SESSION_DIR,
    SANDBOX_GATEWAY_HOST_BRIDGE,
)
from agent.mcp_names import SUBMISSION_SERVER
from agent.protocols import PHASES, SUBMISSION
from agent.sandbox.manifest import manifest_mcp_servers
from agent.sandbox.sbx.exec import sandbox_name_from_env

SESSION_HEADER = "NIKA-Session-Id"
# Per-session secret for the gateway phase-advance endpoint (host-only).
PHASE_TOKEN_HEADER = "NIKA-Phase-Token"
# Host-side remote runs receive the remote gateway's phase token here.
ENV_GATEWAY_PHASE_TOKEN = "NIKA_MCP_GATEWAY_PHASE_TOKEN"


def _session_backend(session_id: str) -> str | None:
    """Return the lab backend stored for *session_id*, if that session exists."""
    try:
        from nika.runtime.factory import resolve_backend
        from nika.utils.session_store import SessionStore

        row = SessionStore().get_session(session_id)
    except (FileNotFoundError, OSError, ImportError, ModuleNotFoundError):
        return None
    return resolve_backend(row)


def filter_phase_servers(servers: dict, phase: str) -> dict:
    """Keep only the MCP servers an agent may use in *phase*.

    The gateway enforces phase access too, but keeping the task server out of
    the diagnosis config also keeps the fault catalog out of the agent's tool
    inventory, and keeps diagnosis servers out of the submission config.
    """
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")
    if phase == SUBMISSION:
        return {name: cfg for name, cfg in servers.items() if name == SUBMISSION_SERVER}
    return {name: cfg for name, cfg in servers.items() if name != SUBMISSION_SERVER}


def load_session_mcp_config(
    session_id: str,
    scenario_name: str,
    *,
    backend: str | None = None,
    session_dir: str | Path | None = None,
    phase: str | None = None,
) -> dict:
    """Return session-scoped HTTP MCP config.

    With *phase*, only that phase's servers are returned (see
    :func:`filter_phase_servers`); without it, every session server.
    """
    servers = _load_session_mcp_config(
        session_id, scenario_name, backend=backend, session_dir=session_dir
    )
    return servers if phase is None else filter_phase_servers(servers, phase)


def _load_session_mcp_config(
    session_id: str,
    scenario_name: str,
    *,
    backend: str | None,
    session_dir: str | Path | None,
) -> dict:
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
    elif backend is None:
        # ISP scenarios support both lab backends. The gateway only mounts
        # servers for the backend that was actually started.
        backend = _session_backend(session_id)
    from agent.utils.mcp_servers import MCPServerConfig

    return MCPServerConfig(session_id=session_id).load_session_http_config(
        scenario_name,
        backend=backend,
    )


def _host_can_resolve(hostname: str) -> bool:
    try:
        socket.getaddrinfo(hostname, None)
        return True
    except OSError:
        return False


def _rewrite_gateway_base_for_client(base: str) -> str:
    """Map sandbox-facing URLs to something the current process can dial."""
    if not base:
        return base
    if base.startswith("http://0.0.0.0:"):
        base = "http://127.0.0.1:" + base.removeprefix("http://0.0.0.0:")
    elif base.startswith("https://0.0.0.0:"):
        base = "https://127.0.0.1:" + base.removeprefix("https://0.0.0.0:")
    # CLI agents orchestrate on the host with NIKA_SANDBOX_EXECUTION=1, but
    # Linux hosts typically cannot resolve host.docker.internal (microVMs can).
    if SANDBOX_GATEWAY_HOST_BRIDGE in base and not _host_can_resolve(
        SANDBOX_GATEWAY_HOST_BRIDGE
    ):
        base = base.replace(SANDBOX_GATEWAY_HOST_BRIDGE, "127.0.0.1")
    return base


def _http_advance_submission_phase(
    session_id: str,
    base: str,
    *,
    token: str,
    diagnosis_report: str = "",
) -> dict:
    """Freeze (when report provided) and advance phase on a remote gateway."""
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
            PHASE_TOKEN_HEADER: token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError(
                    f"MCP phase advance failed with HTTP {response.status}"
                )
            payload = json.load(response)
            context = payload.get("submission_context")
            if (
                not isinstance(context, dict)
                or not {"diagnosis_report", "fault_ontology", "resources"}
                <= context.keys()
            ):
                raise RuntimeError("MCP phase advance returned no submission context")
            return context
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"MCP phase advance failed: HTTP {exc.code}: {body}"
        ) from exc


def begin_submission_mcp_phase(session_id: str, diagnosis_report: str = "") -> dict:
    """Freeze diagnosis, advance the gateway, and return the submission context.

    Host-only. The gateway's phase-advance endpoint requires a per-session
    secret that never enters a sandbox, so an agent cannot fetch the fault
    ontology and resource catalog mid-diagnosis. In-sandbox SDK agents hand
    their report to the host runner (:mod:`agent.sandbox.runner`), which calls
    this function between the diagnosis and submission steps.
    """
    if os.environ.get(ENV_SANDBOX_EXECUTION) == "1" and not sandbox_name_from_env():
        raise RuntimeError(
            "The MCP phase advance runs on the host; sandboxed agents return "
            "their diagnosis report to the host runner instead."
        )
    from agent.utils.mcp_servers import agent_facing_mcp_session_id

    # Agents / HTTP paths use the opaque handle; freeze/advance accept either key.
    mcp_session_id = agent_facing_mcp_session_id(session_id)

    use_http = False
    try:
        from nika.remote.config import is_remote_enabled

        use_http = is_remote_enabled()
    except Exception:  # noqa: BLE001 - remote package optional at import time
        use_http = False

    if use_http:
        # The gateway lives on the remote server; it freezes the report there.
        base = _rewrite_gateway_base_for_client(
            os.environ.get(ENV_GATEWAY_URL, "").strip().rstrip("/")
        )
        token = os.environ.get(ENV_GATEWAY_PHASE_TOKEN, "").strip()
        if not base or not token:
            raise RuntimeError(
                f"{ENV_GATEWAY_URL} and {ENV_GATEWAY_PHASE_TOKEN} must be set for "
                "a remote MCP phase advance."
            )
        return _http_advance_submission_phase(
            mcp_session_id, base, token=token, diagnosis_report=diagnosis_report
        )

    from nika.mcp.gateway.phase import advance_mcp_phase
    from nika.mcp.gateway.session_registry import resolve_canonical_session_id
    from nika.workflows.agent.submission import (
        freeze_diagnosis,
        load_submission_context,
    )

    freeze_diagnosis(mcp_session_id, diagnosis_report)
    advance_mcp_phase(mcp_session_id, SUBMISSION)
    return load_submission_context(resolve_canonical_session_id(mcp_session_id))
