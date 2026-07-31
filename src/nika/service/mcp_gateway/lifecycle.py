"""Start and stop the host-side MCP HTTP gateway."""

from __future__ import annotations

import os
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Literal

import uvicorn

from nika.service.mcp_gateway.app import create_gateway_app, reset_gateway_mcp_state
from nika.service.mcp_gateway.k8s_upstream import (
    K8S_MCP_SERVER_NAME,
    resolve_k8s_mcp_upstream,
    scenario_needs_k8s_mcp,
)
from nika.service.mcp_gateway.session_registry import (
    clear_sessions,
    register_session,
    unregister_session,
)
from nika.utils.net import pick_free_port

ENV_GATEWAY_URL = "NIKA_MCP_GATEWAY_URL"
ENV_GATEWAY_AGENT_URL = "NIKA_MCP_GATEWAY_AGENT_URL"
ENV_GATEWAY_HOST = "NIKA_MCP_GATEWAY_HOST"
ENV_GATEWAY_PORT = "NIKA_MCP_GATEWAY_PORT"

SANDBOX_GATEWAY_BIND_HOST = "0.0.0.0"
SANDBOX_GATEWAY_AGENT_HOST = "host.docker.internal"

PolicyMode = Literal["two_phase", "unified"]

_manager_lock = threading.Lock()
_active_manager: "McpGatewayManager | None" = None


@dataclass
class McpGatewayManager:
    host: str
    port: int
    _server: uvicorn.Server | None = None
    _thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        config = uvicorn.Config(
            create_gateway_app(),
            host=self.host,
            port=self.port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run,
            name="nika-mcp-gateway",
            daemon=True,
        )
        self._thread.start()
        self._wait_until_ready()

    def _wait_until_ready(self, timeout_sec: float = 10.0) -> None:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((self.host, self.port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.05)
        raise TimeoutError(
            f"MCP gateway did not become ready at {self.host}:{self.port}"
        )

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._server = None
        self._thread = None


def set_gateway_agent_url(manager: McpGatewayManager, *, agent_host: str) -> str:
    """Expose a container-reachable gateway URL via ``NIKA_MCP_GATEWAY_AGENT_URL``."""
    agent_url = f"http://{agent_host}:{manager.port}"
    os.environ[ENV_GATEWAY_AGENT_URL] = agent_url
    return agent_url


def start_gateway(
    *, host: str | None = None, port: int | None = None
) -> McpGatewayManager:
    """Start the MCP gateway and return its manager."""
    global _active_manager
    bind_host = host or os.environ.get(ENV_GATEWAY_HOST, "127.0.0.1")
    port_raw = port if port is not None else os.environ.get(ENV_GATEWAY_PORT, "0")
    bind_port = pick_free_port(bind_host) if str(port_raw) == "0" else int(port_raw)

    manager = McpGatewayManager(host=bind_host, port=bind_port)
    manager.start()
    with _manager_lock:
        _active_manager = manager
    os.environ[ENV_GATEWAY_URL] = manager.base_url
    return manager


def stop_gateway() -> None:
    """Stop the active MCP gateway if running."""
    global _active_manager
    with _manager_lock:
        manager = _active_manager
        _active_manager = None
    if manager is not None:
        manager.stop()
    reset_gateway_mcp_state()
    os.environ.pop(ENV_GATEWAY_URL, None)
    os.environ.pop(ENV_GATEWAY_AGENT_URL, None)
    clear_sessions()


@contextmanager
def mcp_gateway_for_session(
    session_id: str,
    *,
    scenario_name: str = "",
    policy_mode: PolicyMode = "two_phase",
    host: str | None = None,
    port: int | None = None,
    sandbox: bool = False,
    sandbox_agent_host: str = SANDBOX_GATEWAY_AGENT_HOST,
) -> Iterator[McpGatewayManager]:
    """Start gateway, register *session_id*, expose URL via env, then clean up."""
    bind_host = host
    if sandbox and bind_host is None:
        bind_host = SANDBOX_GATEWAY_BIND_HOST
    manager = start_gateway(host=bind_host, port=port)
    if sandbox:
        set_gateway_agent_url(manager, agent_host=sandbox_agent_host)
    remote_upstreams: dict[str, str] = {}
    if scenario_name and scenario_needs_k8s_mcp(scenario_name):
        remote_upstreams[K8S_MCP_SERVER_NAME] = resolve_k8s_mcp_upstream(session_id)
    register_session(
        session_id,
        scenario_name=scenario_name,
        policy_mode=policy_mode,
        remote_upstreams=remote_upstreams,
    )
    try:
        yield manager
    finally:
        unregister_session(session_id)
        stop_gateway()
