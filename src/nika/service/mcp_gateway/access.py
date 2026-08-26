"""Execution-enforced diagnosis access policies and audit records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nika.runtime.spec import NodeRole


# MCP tool names are globally unique today.  The entries name every argument
# that selects a lab node; the gateway checks all supplied targets before the
# server receives the call.
TOOL_NODE_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "ping_pair": ("host_a", "host_b"),
    "traceroute": ("host_name",),
    "systemctl_ops": ("host_name",),
    "get_host_net_config": ("host_name",),
    "get_tc_statistics": ("host_name",),
    "netstat": ("host_name",),
    "ip_addr_statistics": ("host_name",),
    "ethtool": ("host_name",),
    "curl_web_test": ("host_name",),
    "iperf_test": ("client_host_name", "server_host_name"),
    "active_tcp_probe": ("source", "destination"),
    "packet_capture_start": ("device",),
    "cat_file": ("host_name",),
    "exec_shell": ("host_name",),
    "exec_shell_dual": ("host1", "host2"),
    "frr_get_bgp_conf": ("router_name",),
    "frr_show_running_config": ("router_name",),
    "frr_show_ip_route": ("router_name",),
    "frr_get_ospf_conf": ("router_name",),
    "frr_exec": ("router_name",),
    "frr_get_routing_state": ("device",),
    "frr_get_rpki_status": ("device",),
    "srl_exec_cli": ("device_name",),
    "srl_get_bgp_as": ("device_name",),
    "srl_show_running_config": ("device_name",),
    "srl_show_bgp_summary": ("device_name",),
    "srl_show_ip_route": ("device_name",),
    "p4_get_runtime_state": ("switch_name",),
    "sdn_get_fabric_state": ("switch_name", "source"),
    "sdn_endpoint_reachability": ("source",),
}


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str = ""
    targets: tuple[str, ...] = ()


def policy_snapshot(*, role: str, policy: Any, node_roles: dict[str, str]) -> dict:
    return {
        "role": role,
        "diagnosis": {
            "tools": list(policy.tools),
            "node_roles": list(policy.node_roles),
            "node_ids": list(policy.node_ids),
        },
        "nodes": {name: node_roles[name] for name in sorted(node_roles)},
        "submission": {"tools": ["submit"]},
    }


def _matches(value: str, allowed: list[str]) -> bool:
    return "*" in allowed or value in allowed


def decide_diagnosis_access(
    *,
    policy: dict,
    tool_name: str,
    arguments: dict[str, Any],
    node_roles: dict[str, str],
) -> AccessDecision:
    if not _matches(tool_name, list(policy.get("tools") or [])):
        return AccessDecision(False, "tool_not_allowed")
    names: list[str] = []
    for key in TOOL_NODE_ARGUMENTS.get(tool_name, ()):
        value = arguments.get(key)
        if value is not None and str(value).strip():
            names.append(str(value))
    for name in names:
        role = node_roles.get(name)
        if role is None:
            return AccessDecision(False, "unknown_target", tuple(names))
        if name not in set(policy.get("node_ids") or []) and not _matches(
            role, list(policy.get("node_roles") or [])
        ):
            return AccessDecision(False, "node_not_allowed", tuple(names))
    return AccessDecision(True, targets=tuple(names))


def node_roles_for_session(session_id: str) -> dict[str, str]:
    """Load scenario-declared NodeRole identities without querying ground truth."""
    from nika.utils.session_store import SessionStore
    from nika.problems.rca.inventory import load_offline_net_env

    row = SessionStore().get_session(session_id)
    params = row.get("scenario_params") or {}
    env = load_offline_net_env(
        str(row.get("scenario_name") or ""),
        str(row.get("scenario_topo_size") or params.get("topo_size") or ""),
        topo=params.get("topo"),
        igp=params.get("igp"),
        bgp_mode=params.get("bgp_mode"),
    )
    return {
        name: identity.role.value
        for name, identity in getattr(env, "machine_identities", {}).items()
        if identity.role in set(NodeRole)
    }
