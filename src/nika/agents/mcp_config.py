"""Host-side MCP client config of a session and the submission phase advance.

Agents never build this config: the host writes the session's MCP servers into
the task (see :mod:`nika.agents.sandbox.mcp_manifest`) and agents read it with
:func:`agent.utils.mcp_client.session_mcp_config`.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from agent.protocols import SUBMISSION
from agent.runtime.contract import SESSION_HEADER, SESSION_TOKEN_HEADER
from agent.utils.mcp_client import filter_phase_servers
from nika.agents.sandbox.config import (
    ENV_GATEWAY_AGENT_URL,
    ENV_GATEWAY_SERVERS,
    ENV_GATEWAY_SESSION_TOKEN,
    ENV_GATEWAY_URL,
    SANDBOX_GATEWAY_HOST_BRIDGE,
)
from nika.benchmark_engine.protocol import AgentEndpoints, GatewayRequest
from nika.mcp.registry import select_diagnosis_servers
from nika.telemetry import telemetry_for_session

__all__ = [
    "MCPServerConfig",
    "SESSION_HEADER",
    "SESSION_TOKEN_HEADER",
    "agent_facing_mcp_session_id",
    "begin_submission_mcp_phase",
    "client_mcp_config",
    "engine_gateway",
    "load_session_mcp_config",
    "select_diagnosis_servers",
    "select_session_servers",
    "session_http_headers",
]

# Per-session secret for the gateway phase-advance endpoint (host-only).
PHASE_TOKEN_HEADER = "NIKA-Phase-Token"
# The frontend receives the trial gateway's phase token here.
ENV_GATEWAY_PHASE_TOKEN = "NIKA_MCP_GATEWAY_PHASE_TOKEN"


def agent_facing_mcp_session_id(session_id: str) -> str:
    """Return the opaque agent handle for MCP headers when available.

    Callers may pass the canonical SessionStore id or the opaque id (no store
    row). Missing ``agent_session_id`` falls back to ``session_id`` for
    in-flight / legacy sessions.
    """
    sid = (session_id or "").strip()
    if not sid:
        return sid
    try:
        from nika.utils.agent_session_id import resolve_agent_session_id
        from nika.utils.session_store import SessionStore

        return resolve_agent_session_id(SessionStore().get_session(sid))
    except FileNotFoundError:
        return sid


@contextmanager
def engine_gateway(
    session_id: str, session_dir: str | Path, request: GatewayRequest
) -> Iterator[AgentEndpoints]:
    """Open the trial's MCP gateway on its benchmark engine for one agent run.

    The gateway's URLs, tokens, and served MCP servers are exported to this
    process's environment until the context closes, where the agent's MCP
    config and the submission phase advance read them.
    """
    from nika.benchmark_engine import benchmark_engine
    from nika.benchmark_engine.protocol import TrialHandle

    keys = (
        ENV_GATEWAY_URL,
        ENV_GATEWAY_AGENT_URL,
        ENV_GATEWAY_PHASE_TOKEN,
        ENV_GATEWAY_SESSION_TOKEN,
        ENV_GATEWAY_SERVERS,
    )
    # Before opening: an in-process gateway exports its own URL when it starts.
    prior = {key: os.environ.get(key) for key in keys}
    engine = benchmark_engine()
    handle = TrialHandle(session_id=session_id, trial_dir=str(session_dir))
    endpoints = engine.agent_endpoints(handle, request)
    try:
        os.environ.update(
            {
                ENV_GATEWAY_URL: endpoints.gateway_url,
                ENV_GATEWAY_AGENT_URL: endpoints.agent_url,
                # Host orchestration advances the phase; sandbox env filters
                # NIKA_*TOKEN.
                ENV_GATEWAY_PHASE_TOKEN: endpoints.phase_token,
                ENV_GATEWAY_SESSION_TOKEN: endpoints.session_token,
                ENV_GATEWAY_SERVERS: ",".join(endpoints.servers),
            }
        )
        yield endpoints
    finally:
        try:
            engine.close_agent_endpoints(handle)
        finally:
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def select_session_servers(
    scenario_name: str,
    *,
    backend: str | None = None,
    telemetry: Iterable[str] = (),
) -> list[str]:
    """Return all MCP server names for a troubleshooting session.

    Inside :func:`engine_gateway`, these are the servers the benchmark
    engine's gateway serves, since a release may serve other tools than this
    NIKA.
    """
    from nika.mcp.registry import SUBMISSION_SERVER

    served = os.environ.get(ENV_GATEWAY_SERVERS, "").strip()
    if served:
        return served.split(",")
    servers = select_diagnosis_servers(
        scenario_name, backend=backend, telemetry=telemetry
    )
    if SUBMISSION_SERVER not in servers:
        servers.append(SUBMISSION_SERVER)
    return servers


def session_http_headers(session_id: str) -> dict[str, str]:
    agent_sid = agent_facing_mcp_session_id(session_id)
    return {
        SESSION_HEADER: agent_sid,
        SESSION_TOKEN_HEADER: gateway_session_token(agent_sid),
    }


def gateway_session_token(session_id: str) -> str:
    """Return the token the MCP gateway requires from *session_id*'s clients.

    A gateway in this process holds it in its session registry; a remote
    gateway's token comes from the attach response through the environment.
    """
    from nika.mcp.gateway.session_registry import get_session

    entry = get_session(session_id)
    if entry is not None:
        return entry.access_token
    return os.environ.get(ENV_GATEWAY_SESSION_TOKEN, "").strip()


def _gateway_base_url() -> str:
    base = os.environ.get(ENV_GATEWAY_URL, "").strip().rstrip("/")
    if not base:
        raise RuntimeError(
            f"{ENV_GATEWAY_URL} is not set. Start the MCP gateway before building HTTP config."
        )
    return base


def mcp_read_timeout_seconds() -> float:
    """``nika.mcp.read_timeout_sec`` (0 disables the timeout)."""
    from nika.run_config.loader import get_run_config

    return float(get_run_config().nika.mcp.read_timeout_sec)


class MCPServerConfig:
    def __init__(self, session_id: str):
        if not session_id:
            raise ValueError("session_id is required to start MCP servers.")
        self.session_id = session_id

    def load_http_config(
        self, server_names: list[str], *, base_url: str | None = None
    ) -> dict:
        """Return HTTP MCP client config for *server_names*.

        *base_url* is the gateway as the client reaches it (default: the
        host-facing gateway, ``NIKA_MCP_GATEWAY_URL``).
        """
        base = (base_url or _gateway_base_url()).rstrip("/")
        return {
            name: {
                "transport": "http",
                "url": f"{base}/mcp/{name}/mcp",
                "headers": session_http_headers(self.session_id),
            }
            for name in server_names
        }

    def load_session_http_config(
        self,
        scenario_name: str,
        *,
        backend: str | None = None,
        base_url: str | None = None,
        telemetry: Iterable[str] = (),
    ) -> dict:
        """Return HTTP MCP config for all servers needed by the session."""
        server_names = select_session_servers(
            scenario_name, backend=backend, telemetry=telemetry
        )
        return self.load_http_config(server_names, base_url=base_url)


def _session_deployment(session_id: str) -> tuple[str | None, tuple[str, ...]]:
    """Return the lab backend and telemetry stored for *session_id*, if it exists."""
    try:
        from nika.runtime.factory import resolve_backend
        from nika.utils.session_store import SessionStore

        row = SessionStore().get_session(session_id)
    except (FileNotFoundError, OSError):
        return None, ()
    return resolve_backend(row), telemetry_for_session(row)


def load_session_mcp_config(
    session_id: str,
    scenario_name: str,
    *,
    backend: str | None = None,
    phase: str | None = None,
) -> dict:
    """Session-scoped HTTP MCP config for a client on the host.

    With *phase*, only that phase's servers are returned (see
    :func:`~agent.utils.mcp_client.filter_phase_servers`).
    """
    # ISP scenarios support both lab backends, and telemetry is chosen per
    # session. The gateway only mounts servers for what was actually started.
    stored_backend, telemetry = _session_deployment(session_id)
    servers = MCPServerConfig(session_id=session_id).load_session_http_config(
        scenario_name, backend=backend or stored_backend, telemetry=telemetry
    )
    return servers if phase is None else filter_phase_servers(servers, phase)


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
    # Linux hosts typically cannot resolve host.docker.internal (microVMs can).
    if SANDBOX_GATEWAY_HOST_BRIDGE in base and not _host_can_resolve(
        SANDBOX_GATEWAY_HOST_BRIDGE
    ):
        base = base.replace(SANDBOX_GATEWAY_HOST_BRIDGE, "127.0.0.1")
    return base


def client_mcp_config(servers: dict) -> dict:
    """Point session MCP servers at the gateway as this host process reaches it.

    Sandbox task configs address the gateway as the sandbox reaches it
    (``host.docker.internal``, or an E2B tunnel relay on the sandbox's
    loopback). NIKA's unstructured submission runs on the host, so it dials
    ``NIKA_MCP_GATEWAY_URL``, the host-facing gateway (local or on a remote lab
    host).
    """
    base = _rewrite_gateway_base_for_client(
        os.environ.get(ENV_GATEWAY_URL, "").strip().rstrip("/")
    )
    if not base:
        return servers
    return {
        name: {**cfg, "url": base + cfg["url"][cfg["url"].rindex(f"/mcp/{name}/") :]}
        for name, cfg in servers.items()
    }


def _phase_timeout_sec() -> float:
    try:
        return mcp_read_timeout_seconds()
    except Exception:  # noqa: BLE001 - fall back when no run config is loadable
        return 120.0


def _http_advance_submission_phase(
    session_id: str,
    base: str,
    *,
    token: str,
    diagnosis_report: str = "",
) -> dict:
    """Freeze the report and advance the phase on the trial's gateway."""
    url = f"{base}/gateway/sessions/{session_id}/phase"
    payload = {"phase": SUBMISSION, "diagnosis_report": diagnosis_report}
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
        # The lab host freezes the report and builds the submission catalog; a
        # busy lab host can take well over 10 s.
        timeout = _phase_timeout_sec() or None
        with urllib.request.urlopen(request, timeout=timeout) as response:
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

    The gateway's phase-advance endpoint requires a per-session secret that
    never enters a sandbox, so an agent cannot fetch the fault ontology and
    resource catalog mid-diagnosis. The sandbox manager calls this between the
    agent's diagnosis and submission steps.
    """
    # Agents / HTTP paths use the opaque handle; freeze/advance accept either key.
    mcp_session_id = agent_facing_mcp_session_id(session_id)
    # The gateway runs on the benchmark engine (or a NIKA Remote lab host); it
    # freezes the report there.
    base = _rewrite_gateway_base_for_client(
        os.environ.get(ENV_GATEWAY_URL, "").strip().rstrip("/")
    )
    token = os.environ.get(ENV_GATEWAY_PHASE_TOKEN, "").strip()
    if not base or not token:
        raise RuntimeError(
            f"{ENV_GATEWAY_URL} and {ENV_GATEWAY_PHASE_TOKEN} must be set for "
            "an MCP phase advance."
        )
    return _http_advance_submission_phase(
        mcp_session_id, base, token=token, diagnosis_report=diagnosis_report
    )
