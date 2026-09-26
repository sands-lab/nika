"""MCP server catalog grouped by backend and functional role."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

Backend = Literal["kathara", "containerlab"]
Role = Literal[
    "host", "routing", "switch", "telemetry", "task", "kubernetes", "observability"
]
ENV_SESSION_BACKEND = "NIKA_SESSION_BACKEND"

# Keyword tokens (from scenario name and net-env TAGS) that trigger optional servers.
ROUTING_KEYWORDS = frozenset({"bgp", "ebgp", "ospf", "rip", "frr", "routing", "rpki"})
IOSXR_KEYWORDS = frozenset({"iosxr", "xrd"})
ROUTEROS_KEYWORDS = frozenset({"mikrotik", "routeros"})
# P4/BMv2 only — OVS SDN scenarios use kathara_sdn_mcp_server.
SWITCH_KEYWORDS = frozenset({"p4", "bmv2", "bloom", "mpls", "int", "counter"})
SDN_KEYWORDS = frozenset({"sdn"})
TELEMETRY_KEYWORDS = frozenset({"telemetry"})
KUBERNETES_KEYWORDS = frozenset({"kubernetes", "k3s", "k8s"})


@dataclass(frozen=True)
class MCPServerSpec:
    """One in-process MCP server exposed to troubleshooting agents.

    ``module`` is the Python module whose ``mcp`` attribute is the FastMCP app.
    """

    name: str
    backend: Backend | None
    role: Role
    module: str


MCP_SERVER_SPECS: dict[str, MCPServerSpec] = {
    # Common — any lab backend
    "kathara_base_mcp_server": MCPServerSpec(
        name="kathara_base_mcp_server",
        backend=None,
        role="host",
        module="nika.mcp.servers.common.host_server",
    ),
    "pingmesh_mcp_server": MCPServerSpec(
        name="pingmesh_mcp_server",
        backend=None,
        role="host",
        module="nika.mcp.servers.common.pingmesh_server",
    ),
    "packet_capture_mcp_server": MCPServerSpec(
        name="packet_capture_mcp_server",
        backend=None,
        role="observability",
        module="nika.mcp.servers.common.packet_capture_server",
    ),
    "task_mcp_server": MCPServerSpec(
        name="task_mcp_server",
        backend=None,
        role="task",
        module="nika.mcp.servers.common.task_server",
    ),
    # Kathara — specialised device APIs
    "kathara_frr_mcp_server": MCPServerSpec(
        name="kathara_frr_mcp_server",
        backend="kathara",
        role="routing",
        module="nika.mcp.servers.kathara.frr_server",
    ),
    "kathara_iosxr_mcp_server": MCPServerSpec(
        name="kathara_iosxr_mcp_server",
        backend="kathara",
        role="routing",
        module="nika.mcp.servers.kathara.iosxr_server",
    ),
    "kathara_routeros_mcp_server": MCPServerSpec(
        name="kathara_routeros_mcp_server",
        backend="kathara",
        role="routing",
        module="nika.mcp.servers.kathara.routeros_server",
    ),
    "kathara_bmv2_mcp_server": MCPServerSpec(
        name="kathara_bmv2_mcp_server",
        backend="kathara",
        role="switch",
        module="nika.mcp.servers.kathara.bmv2_server",
    ),
    "kathara_sdn_mcp_server": MCPServerSpec(
        name="kathara_sdn_mcp_server",
        backend="kathara",
        role="switch",
        module="nika.mcp.servers.kathara.sdn_server",
    ),
    "kathara_telemetry_mcp_server": MCPServerSpec(
        name="kathara_telemetry_mcp_server",
        backend="kathara",
        role="telemetry",
        module="nika.mcp.servers.kathara.telemetry_server",
    ),
    # Host-side Kubernetes MCP (session kubeconfig → published API port)
    "k8s_mcp_server": MCPServerSpec(
        name="k8s_mcp_server",
        backend="kathara",
        role="kubernetes",
        module="nika.mcp.k8s.server",
    ),
    # Containerlab — specialised device APIs
    "containerlab_srl_mcp_server": MCPServerSpec(
        name="containerlab_srl_mcp_server",
        backend="containerlab",
        role="routing",
        module="nika.mcp.servers.containerlab.srl_server",
    ),
}

# Stable server names used in agent tool prefixes (``{name}_tool``).
MCP_SERVER_PREFIXES: tuple[str, ...] = tuple(f"{name}_" for name in MCP_SERVER_SPECS)

DIAGNOSIS_HOST_SERVER = "kathara_base_mcp_server"
DIAGNOSIS_PINGMESH_SERVER = "pingmesh_mcp_server"
DIAGNOSIS_PACKET_CAPTURE_SERVER = "packet_capture_mcp_server"
SUBMISSION_SERVER = "task_mcp_server"
K8S_MCP_SERVER = "k8s_mcp_server"


def _sandbox_execution() -> bool:
    return os.environ.get("NIKA_SANDBOX_EXECUTION") == "1"


def _scenario_tokens(scenario_name: str) -> set[str]:
    parts = [scenario_name.lower()]
    if not _sandbox_execution():
        try:
            from nika.net_env.net_env_pool import scenario_tags

            parts.extend(tag.lower() for tag in scenario_tags(scenario_name))
        except ValueError:
            pass
    combined = " ".join(parts)
    return set(combined.replace("_", " ").replace("-", " ").split())


def _resolve_diagnosis_backend(
    scenario_name: str,
    backend: str | None,
) -> str:
    if backend:
        return backend
    if _sandbox_execution():
        return os.environ.get(ENV_SESSION_BACKEND, "").strip() or "kathara"
    try:
        from nika.net_env.net_env_pool import scenario_supported_backends

        supported = scenario_supported_backends(scenario_name)
        if len(supported) == 1:
            return supported[0]
    except ValueError:
        pass
    return "kathara"


def _k8s_mcp_enabled() -> bool:
    """Return whether Kubernetes MCP should be registered for agents."""
    try:
        from nika.run_config.loader import get_run_config

        access = (get_run_config().nika.k8s.access or "auto").strip().lower()
    except Exception:  # noqa: BLE001 - config may be unavailable in sandbox
        access = "auto"
    return access != "kubectl_only"


def select_diagnosis_servers(
    scenario_name: str,
    *,
    backend: str | None = None,
) -> list[str]:
    """Return MCP server names needed for diagnosis on *scenario*."""
    backend = _resolve_diagnosis_backend(scenario_name, backend)

    tokens = _scenario_tokens(scenario_name)
    servers = [
        DIAGNOSIS_HOST_SERVER,
        DIAGNOSIS_PINGMESH_SERVER,
        DIAGNOSIS_PACKET_CAPTURE_SERVER,
    ]

    if backend == "containerlab" and tokens & ROUTING_KEYWORDS:
        servers.append("containerlab_srl_mcp_server")
    elif backend != "containerlab" and tokens & IOSXR_KEYWORDS:
        servers.append("kathara_iosxr_mcp_server")
    elif backend != "containerlab" and tokens & ROUTEROS_KEYWORDS:
        servers.append("kathara_routeros_mcp_server")
    elif backend != "containerlab" and "rpki" in tokens:
        servers.append("kathara_frr_mcp_server")
    if tokens & SWITCH_KEYWORDS:
        servers.append("kathara_bmv2_mcp_server")
    if tokens & SDN_KEYWORDS:
        servers.append("kathara_sdn_mcp_server")
    if tokens & TELEMETRY_KEYWORDS:
        servers.append("kathara_telemetry_mcp_server")
    if tokens & KUBERNETES_KEYWORDS and _k8s_mcp_enabled():
        servers.append(K8S_MCP_SERVER)

    return servers
