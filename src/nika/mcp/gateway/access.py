"""Execution-enforced diagnosis access policies and audit records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from nika.benchmark_engine.protocol import AccessPolicy, AgentAccess
from nika.runtime.spec import NodeRole


# MCP tool names are globally unique today.  The entries name every argument
# that selects a lab node (string or list of strings); the gateway checks all
# supplied targets before the server receives the call.  Tools that touch no
# lab node map to ``()``.  A tool missing from this map is denied whenever the
# role policy restricts nodes (fail closed).
TOOL_NODE_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "curl_web_test": ("host_name",),
    "iperf_test": ("client_host_name", "server_host_name"),
    "active_tcp_probe": ("source", "destination"),
    "packet_capture_start": ("device",),
    # Capture ids are only issued by packet_capture_start, which is checked.
    "packet_capture_stop": (),
    "packet_capture_inspect": (),
    "exec_shell": ("host_name",),
    "device_read": ("host_name",),
    "device_interface_state": ("host_name",),
    "device_protocol_counters": ("host_name",),
    "frr_get_rpki_status": ("device",),
    "iosxr_exec": ("router_name",),
    "routeros_exec": ("router_name",),
    "srl_exec_cli": ("device_name",),
    "sonic_exec_cli": ("device_name",),
    "sdn_onos_rest": (),
    "p4rt_exec": (),
    "lb_get_state": ("gateway",),
    "bmv2_get_ports": ("switch",),
    "int_query_telemetry": (),
    **{
        name: ()
        for name in (
            "flow_coverage",
            "flow_timeline",
            "flow_current_load",
            "flow_top_talkers",
            "flow_conversations",
            "flow_records",
            "nfdump",
        )
    },
    "prometheus_query": (),
    "syslog_query": (),
    "k8s_list_events": (),
}

# Tools whose target node is fixed by the service layer rather than an argument.
_K8S_CONTROL_NODE = ("controller",)
TOOL_IMPLICIT_TARGETS: dict[str, tuple[str, ...]] = {
    "sdn_onos_rest": ("onos",),
    "p4rt_exec": ("fabric_mgr",),
    "int_query_telemetry": ("collector",),
    **{
        name: ("telemetry_collector",)
        for name in (
            "flow_coverage",
            "flow_timeline",
            "flow_current_load",
            "flow_top_talkers",
            "flow_conversations",
            "flow_records",
            "nfdump",
        )
    },
    "prometheus_query": ("telemetry_collector",),
    "syslog_query": ("telemetry_collector",),
    **{
        name: _K8S_CONTROL_NODE
        for name in TOOL_NODE_ARGUMENTS
        if name.startswith("k8s_")
    },
}

# List arguments whose omission means "every discovered node".  A restricted
# policy requires them to be explicit so the default fan-out cannot escape it.
TOOL_DEFAULT_ALL_ARGUMENTS: dict[str, tuple[str, ...]] = {}


# Direct shell and device CLI access is separate from service-backed implicit access.
DIRECT_EXEC_DENIED_ROLES = frozenset({NodeRole.INFRASTRUCTURE.value})
DIRECT_EXEC_TOOLS = frozenset(
    {
        "exec_shell",
        "device_read",
        "device_interface_state",
        "device_protocol_counters",
        "iosxr_exec",
        "routeros_exec",
        "srl_exec_cli",
        "sonic_exec_cli",
    }
)


SHELL_NODE_ROLES = frozenset({"host", "server", "service", "controller"})


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
            "shell_node_roles": list(policy.shell_node_roles),
        },
        "nodes": {name: node_roles[name] for name in sorted(node_roles)},
        "submission": {"tools": ["submit"]},
    }


def _matches(value: str, allowed: list[str]) -> bool:
    return "*" in allowed or value in allowed


def _argument_targets(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else [value]
    return [str(item) for item in values if item is not None and str(item).strip()]


def decide_diagnosis_access(
    *,
    policy: dict,
    tool_name: str,
    arguments: dict[str, Any],
    node_roles: dict[str, str],
) -> AccessDecision:
    if not _matches(tool_name, list(policy.get("tools") or [])):
        return AccessDecision(False, "tool_not_allowed")
    allowed_ids = set(policy.get("node_ids") or [])
    allowed_roles = list(policy.get("node_roles") or [])
    unrestricted = "*" in allowed_roles
    if tool_name not in TOOL_NODE_ARGUMENTS and not unrestricted:
        return AccessDecision(False, "tool_targets_unknown")
    names: list[str] = []
    for key in TOOL_NODE_ARGUMENTS.get(tool_name, ()):
        value = arguments.get(key)
        if not unrestricted and key in TOOL_DEFAULT_ALL_ARGUMENTS.get(tool_name, ()):
            if not _argument_targets(value):
                return AccessDecision(False, "explicit_targets_required")
        names.extend(_argument_targets(value))
    if tool_name in DIRECT_EXEC_TOOLS:
        for name in names:
            if (
                node_roles.get(name) in DIRECT_EXEC_DENIED_ROLES
                and name not in allowed_ids
            ):
                return AccessDecision(
                    False, "node_role_direct_exec_denied", tuple(names)
                )
    # Under ``*`` every value passes, including IP addresses and names the
    # scenario did not declare (the tool reports its own errors). A restricted
    # policy can only vouch for declared nodes, so it denies anything else,
    # explicit IP addresses included.
    for name in () if unrestricted else names:
        role = node_roles.get(name)
        if role is None:
            return AccessDecision(False, "unknown_target", tuple(names))
        if name not in allowed_ids and not _matches(role, allowed_roles):
            return AccessDecision(False, "node_not_allowed", tuple(names))
    if tool_name == "exec_shell":
        shell_roles = (
            set(policy.get("shell_node_roles", SHELL_NODE_ROLES)) & SHELL_NODE_ROLES
        )
        if not names or any(node_roles.get(name) not in shell_roles for name in names):
            return AccessDecision(False, "shell_role_not_allowed", tuple(names))
    implicit = TOOL_IMPLICIT_TARGETS.get(tool_name, ())
    targets = (*names, *implicit)
    if not unrestricted:
        for name in implicit:
            role = node_roles.get(name)
            if role is None or (
                name not in allowed_ids and not _matches(role, allowed_roles)
            ):
                return AccessDecision(False, "node_not_allowed", targets)
    return AccessDecision(True, targets=targets)


def node_roles_for_session(session_id: str) -> dict[str, str]:
    """Load scenario-declared NodeRole identities without querying ground truth."""
    from nika.utils.session_store import SessionStore
    from nika.problems.rca.inventory import load_session_offline_net_env

    row = SessionStore().get_session(session_id)
    recorded = (row.get("metadata") or {}).get("machine_identities")
    if recorded:
        from nika.runtime.spec import MachineInventory

        return {
            name: identity.role.value
            for name, identity in MachineInventory.from_dict(recorded).nodes.items()
        }
    env = load_session_offline_net_env(row)
    return {
        name: identity.role.value
        for name, identity in getattr(env, "machine_identities", {}).items()
        if identity.role in set(NodeRole)
    }


def configured_access() -> AgentAccess:
    """The access policy that the run config's ``agent.access.role`` selects."""
    from nika.run_config.loader import get_run_config

    access = get_run_config().agent.access
    return AgentAccess(
        role=access.role,
        policy=AccessPolicy(**access.roles[access.role].model_dump()),
    )
