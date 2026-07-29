"""MCP server catalog grouped by backend and functional role."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from nika.config import MCP_SERVER_DIR

Backend = Literal["kathara", "containerlab"]
Role = Literal["host", "routing", "switch", "telemetry", "task", "kubernetes"]
ENV_SESSION_BACKEND = "NIKA_SESSION_BACKEND"

# Keyword tokens (from scenario name and net-env TAGS) that trigger optional servers.
ROUTING_KEYWORDS = frozenset({"bgp", "ospf", "rip", "frr", "routing"})
SWITCH_KEYWORDS = frozenset({"p4", "bmv2", "sdn", "bloom", "mpls", "int", "counter"})
TELEMETRY_KEYWORDS = frozenset({"telemetry"})
KUBERNETES_KEYWORDS = frozenset({"kubernetes", "k3s", "k8s"})


@dataclass(frozen=True)
class MCPServerSpec:
    """One MCP server exposed to troubleshooting agents (in-process or remote)."""

    name: str
    backend: Backend | None
    role: Role
    module: str
    remote: bool = False

    @property
    def script_path(self) -> str:
        return str(MCP_SERVER_DIR / self.module)


MCP_SERVER_SPECS: dict[str, MCPServerSpec] = {
    # Common — any lab backend
    "kathara_base_mcp_server": MCPServerSpec(
        name="kathara_base_mcp_server",
        backend=None,
        role="host",
        module="common/host_server.py",
    ),
    "pingmesh_mcp_server": MCPServerSpec(
        name="pingmesh_mcp_server",
        backend=None,
        role="host",
        module="common/pingmesh_server.py",
    ),
    "task_mcp_server": MCPServerSpec(
        name="task_mcp_server",
        backend=None,
        role="task",
        module="common/task_server.py",
    ),
    # Kathara — specialised device APIs
    "kathara_frr_mcp_server": MCPServerSpec(
        name="kathara_frr_mcp_server",
        backend="kathara",
        role="routing",
        module="kathara/frr_server.py",
    ),
    "kathara_bmv2_mcp_server": MCPServerSpec(
        name="kathara_bmv2_mcp_server",
        backend="kathara",
        role="switch",
        module="kathara/bmv2_server.py",
    ),
    "kathara_telemetry_mcp_server": MCPServerSpec(
        name="kathara_telemetry_mcp_server",
        backend="kathara",
        role="telemetry",
        module="kathara/telemetry_server.py",
    ),
    # In-node Kubernetes MCP (controller process; gateway reverse-proxies)
    "k8s_mcp_server": MCPServerSpec(
        name="k8s_mcp_server",
        backend="kathara",
        role="kubernetes",
        module="k8s_mcp_server",
        remote=True,
    ),
    # Containerlab — specialised device APIs
    "containerlab_srl_mcp_server": MCPServerSpec(
        name="containerlab_srl_mcp_server",
        backend="containerlab",
        role="routing",
        module="containerlab/srl_server.py",
    ),
}

# Stable server names used in agent tool prefixes (``{name}_tool``).
MCP_SERVER_PREFIXES: tuple[str, ...] = tuple(f"{name}_" for name in MCP_SERVER_SPECS)

DIAGNOSIS_HOST_SERVER = "kathara_base_mcp_server"
DIAGNOSIS_PINGMESH_SERVER = "pingmesh_mcp_server"
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


def select_diagnosis_servers(
    scenario_name: str,
    *,
    backend: str | None = None,
) -> list[str]:
    """Return MCP server names needed for diagnosis on *scenario*."""
    backend = _resolve_diagnosis_backend(scenario_name, backend)

    tokens = _scenario_tokens(scenario_name)
    servers = [DIAGNOSIS_HOST_SERVER, DIAGNOSIS_PINGMESH_SERVER]

    if backend == "containerlab" and tokens & ROUTING_KEYWORDS:
        servers.append("containerlab_srl_mcp_server")
    elif backend != "containerlab" and tokens & ROUTING_KEYWORDS:
        servers.append("kathara_frr_mcp_server")
    if tokens & SWITCH_KEYWORDS:
        servers.append("kathara_bmv2_mcp_server")
    if tokens & TELEMETRY_KEYWORDS:
        servers.append("kathara_telemetry_mcp_server")
    if tokens & KUBERNETES_KEYWORDS:
        servers.append(K8S_MCP_SERVER)

    return servers


def get_spec(name: str) -> MCPServerSpec:
    try:
        return MCP_SERVER_SPECS[name]
    except KeyError as exc:
        raise KeyError(f"Unknown MCP server: {name!r}") from exc
