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
from nika.service.mcp_gateway.access import node_roles_for_session, policy_snapshot
from nika.service.mcp_gateway.session_registry import (
    clear_sessions,
    register_session,
    unregister_session,
)
from nika.utils.net import pick_free_port

ENV_GATEWAY_URL = "NIKA_MCP_GATEWAY_URL"
ENV_GATEWAY_AGENT_URL = "NIKA_MCP_GATEWAY_AGENT_URL"

SANDBOX_GATEWAY_BIND_HOST = "0.0.0.0"
SANDBOX_GATEWAY_AGENT_HOST = "host.docker.internal"

PolicyMode = Literal["two_phase", "unified"]

_manager_lock = threading.Lock()
_active_manager: "McpGatewayManager | None" = None


@dataclass
class McpGatewayManager:
    host: str
    port: int
    backend: str | None = None
    _server: uvicorn.Server | None = None
    _thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        config = uvicorn.Config(
            create_gateway_app(backend=self.backend),
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
    *,
    host: str | None = None,
    port: int | None = None,
    backend: str | None = None,
) -> McpGatewayManager:
    """Start the MCP gateway and return its manager."""
    global _active_manager
    if host is None or port is None:
        try:
            from nika.run_config.loader import get_run_config

            mcp = get_run_config().nika.mcp
            if host is None:
                host = mcp.gateway_host
            if port is None:
                port = int(mcp.gateway_port)
        except Exception:  # noqa: BLE001
            if host is None:
                host = "127.0.0.1"
            if port is None:
                port = 0
    bind_host = host
    bind_port = pick_free_port(bind_host) if int(port) == 0 else int(port)

    manager = McpGatewayManager(host=bind_host, port=bind_port, backend=backend)
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
    _shutdown_manager(manager, clear_registry=True)


def _shutdown_manager(
    manager: McpGatewayManager | None,
    *,
    clear_registry: bool = False,
) -> None:
    """Stop *manager* without clobbering a sibling gateway in this process."""
    global _active_manager
    if manager is None:
        if clear_registry:
            reset_gateway_mcp_state()
            os.environ.pop(ENV_GATEWAY_URL, None)
            os.environ.pop(ENV_GATEWAY_AGENT_URL, None)
            clear_sessions()
        return
    with _manager_lock:
        if _active_manager is manager:
            _active_manager = None
    manager.stop()
    if os.environ.get(ENV_GATEWAY_URL) == manager.base_url:
        os.environ.pop(ENV_GATEWAY_URL, None)
        os.environ.pop(ENV_GATEWAY_AGENT_URL, None)
    reset_gateway_mcp_state(backend=manager.backend)
    if clear_registry:
        clear_sessions()


def _resolve_session_backend(scenario_name: str) -> str | None:
    if not scenario_name:
        return None
    try:
        from nika.net_env.isp.profiles import DEFAULT_BACKEND_FOR_ISP
        from nika.net_env.net_env_pool import resolve_scenario_backend

        return resolve_scenario_backend(
            scenario_name, default_when_ambiguous=DEFAULT_BACKEND_FOR_ISP
        )
    except ValueError:
        return None


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
    backend: str | None = None,
) -> Iterator[McpGatewayManager]:
    """Start gateway, register *session_id*, expose URL via env, then clean up."""
    bind_host = host
    if sandbox and bind_host is None:
        bind_host = SANDBOX_GATEWAY_BIND_HOST
    resolved_backend = backend or _resolve_session_backend(scenario_name)
    manager = start_gateway(host=bind_host, port=port, backend=resolved_backend)
    if sandbox:
        set_gateway_agent_url(manager, agent_host=sandbox_agent_host)
    from nika.run_config.loader import get_run_config
    from nika.utils.session_store import SessionStore

    access = get_run_config().agent.access
    role = access.role
    policy = access.roles[role]
    row = SessionStore().get_session(session_id)
    node_roles = node_roles_for_session(session_id)
    snapshot = policy_snapshot(role=role, policy=policy, node_roles=node_roles)
    session_dir = str(row["session_dir"])
    register_session(
        session_id,
        scenario_name=scenario_name,
        policy_mode=policy_mode,
        session_dir=session_dir,
        access_policy=snapshot["diagnosis"],
        node_roles=node_roles,
    )
    try:
        yield manager
    finally:
        unregister_session(session_id)
        _shutdown_manager(manager)
