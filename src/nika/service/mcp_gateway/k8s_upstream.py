"""Discover the in-node Kubernetes MCP upstream for a NIKA session."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any

from nika.service.k8s_mcp_server import DEFAULT_PORT
from nika.service.kathara.docker_utils import get_machine_container
from nika.utils.session_store import SessionStore

K8S_MCP_SERVER_NAME = "k8s_mcp_server"
CONTROLLER_HOST = "controller"
HEALTH_PATH = "/health"


def container_ipv4(container: Any) -> str:
    """Return a host-reachable IPv4 address for a Kathara machine container."""
    if hasattr(container, "reload"):
        try:
            container.reload()
        except Exception:  # noqa: BLE001 - best-effort refresh
            pass

    networks = (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
    for net in networks.values():
        ip = (net or {}).get("IPAddress") or ""
        if ip:
            return ip
    ip = (container.attrs.get("NetworkSettings") or {}).get("IPAddress") or ""
    if ip:
        return ip

    # Bridged Kathara nodes often leave NetworkSettings.IPAddress empty while
    # eth1 still has the Docker-bridge address the host can reach.
    for iface in ("eth1", "eth0"):
        try:
            exit_code, output = container.exec_run(
                [
                    "sh",
                    "-c",
                    f"ip -4 -o addr show {iface} 2>/dev/null | awk '{{print $4}}' | cut -d/ -f1 | head -1",
                ]
            )
        except Exception:  # noqa: BLE001
            continue
        if exit_code != 0:
            continue
        text = (
            output.decode("utf-8", errors="replace")
            if isinstance(output, (bytes, bytearray))
            else str(output)
        ).strip()
        if text and not text.startswith("127."):
            return text

    raise RuntimeError(
        f"Container {getattr(container, 'name', container)} has no Docker IPv4 address"
    )


def k8s_mcp_upstream_url(*, lab_name: str, port: int = DEFAULT_PORT) -> str:
    container = get_machine_container(lab_name=lab_name, host_name=CONTROLLER_HOST)
    ip = container_ipv4(container)
    return f"http://{ip}:{port}"


def wait_k8s_mcp_healthy(
    base_url: str,
    *,
    timeout_sec: float = 60.0,
    poll_sec: float = 1.0,
) -> None:
    health_url = f"{base_url.rstrip('/')}{HEALTH_PATH}"
    deadline = time.monotonic() + timeout_sec
    last_error = "timeout"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=3) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            if '"status": "ok"' in body or '"status":"ok"' in body:
                return
            last_error = f"unexpected body: {body[:200]!r}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(poll_sec)
    raise TimeoutError(
        f"Kubernetes MCP at {health_url} not healthy within {timeout_sec}s: {last_error}"
    )


def resolve_k8s_mcp_upstream(
    session_id: str,
    *,
    port: int = DEFAULT_PORT,
    wait: bool = True,
    timeout_sec: float = 60.0,
) -> str:
    """Return ``http://<controller-docker-ip>:port`` for *session_id*'s lab."""
    row = SessionStore().get_session(session_id)
    lab_name = row.get("lab_name")
    if not lab_name:
        raise RuntimeError(f"Session {session_id!r} has no lab_name")
    base_url = k8s_mcp_upstream_url(lab_name=lab_name, port=port)
    if wait:
        wait_k8s_mcp_healthy(base_url, timeout_sec=timeout_sec)
    return base_url


def scenario_needs_k8s_mcp(scenario_name: str) -> bool:
    from nika.service.mcp_server.registry import KUBERNETES_KEYWORDS, _scenario_tokens

    return bool(_scenario_tokens(scenario_name) & KUBERNETES_KEYWORDS)
