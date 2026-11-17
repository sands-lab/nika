"""Per-trial MCP gateways of a benchmark engine.

Release 0.1.0 started one gateway for each agent run, with that run's session
as its only registered session (``mcp_gateway_for_session``). A benchmark
engine serves several trials from one process, so it keeps the same shape: one
gateway per trial, each on its own port. The only additions are the tokens the
frontend's harnesses present and the tool-call records the frontend reads.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass

from nika.service.mcp_gateway.lifecycle import (
    SANDBOX_GATEWAY_AGENT_HOST,
    SANDBOX_GATEWAY_BIND_HOST,
    McpGatewayManager,
)
from nika.service.mcp_gateway.session_registry import (
    register_session,
    unregister_session,
)
from nika.utils.net import pick_free_port

DEFAULT_BIND_HOST = "127.0.0.1"

# FastMCP server objects are module globals; building a gateway app resets
# and reattaches them, so builds run one at a time.
_build_lock = threading.Lock()


@dataclass
class GatewayLease:
    manager: McpGatewayManager
    agent_url: str
    access_token: str
    phase_token: str


class GatewayLeases:
    """The open trial gateways of one engine process, by session id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._leases: dict[str, GatewayLease] = {}

    def open(
        self,
        session_id: str,
        *,
        scenario_name: str,
        session_dir: str,
        backend: str | None,
        bind_host: str | None = None,
        sandbox: bool = False,
        agent_host: str | None = None,
        agent_session_id: str | None = None,
    ) -> GatewayLease:
        """Start *session_id*'s gateway, or return the one already open.

        With *agent_session_id*, MCP requests and phase advances name the trial
        by it, and a request naming *session_id* is refused.
        """
        with self._lock:
            lease = self._leases.get(session_id)
            if lease is not None:
                return lease
            host = bind_host or (
                SANDBOX_GATEWAY_BIND_HOST if sandbox else DEFAULT_BIND_HOST
            )
            manager = McpGatewayManager(
                host=host, port=pick_free_port(host), backend=backend
            )
            with _build_lock:
                manager.start()
            agent_url = (
                f"http://{agent_host or SANDBOX_GATEWAY_AGENT_HOST}:{manager.port}"
                if sandbox
                else manager.base_url
            )
            lease = GatewayLease(
                manager=manager,
                agent_url=agent_url,
                access_token=secrets.token_urlsafe(32),
                phase_token=secrets.token_urlsafe(32),
            )
            register_session(
                session_id,
                scenario_name=scenario_name,
                policy_mode="two_phase",
                session_dir=session_dir,
                access_token=lease.access_token,
                phase_token=lease.phase_token,
                agent_session_id=agent_session_id,
            )
            self._leases[session_id] = lease
            return lease

    def close(self, session_id: str) -> bool:
        """Stop *session_id*'s gateway; return whether one was open."""
        with self._lock:
            lease = self._leases.pop(session_id, None)
        if lease is None:
            return False
        unregister_session(session_id)
        lease.manager.stop()
        return True
