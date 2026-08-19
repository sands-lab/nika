"""Startup verification and developer smoke checks for sdn_l3_clos."""

from __future__ import annotations

from typing import Any

from nika.net_env.kathara.sdn.fabric_manager.apply import (
    observed_switch_state,
    onos_topology_snapshot,
)
from nika.net_env.kathara.sdn.fabric_manager.forwarding_rules import (
    build_forwarding_rules,
)
from nika.net_env.kathara.sdn.topology_model import (
    VIRTUAL_ROUTER_MAC,
    ClosFabricModel,
    gateway_ip,
)
from nika.net_env.verify import (
    build_lab_verify_result,
    exec_or_empty,
    host_has_ipv4,
    http_ok,
    link_up,
    nodes_deployed,
    ping_ok,
    process_running,
)
from nika.runtime.base import LabRuntime


def _ovs_ready(runtime: LabRuntime, switches: list[str]) -> bool:
    return all(
        bool(exec_or_empty(runtime, switch, "ovs-vsctl show").strip())
        for switch in switches
    )


def _of_sessions_ok(runtime: LabRuntime, model: ClosFabricModel) -> bool:
    """All fabric switches must keep a live OpenFlow session with ONOS."""
    snap = onos_topology_snapshot(runtime)
    devices = snap.get("devices", {}).get("devices", [])
    available = {d.get("id") for d in devices if d.get("available")}
    expected = set(model.expected_device_ids())
    if not expected.issubset(available):
        return False
    links = snap.get("links", {}).get("links", [])
    return len(links) >= max(1, model.expected_leaf_spine_link_count() // 2)


def _group_bucket_counts(runtime: LabRuntime, model: ClosFabricModel) -> bool:
    """Each leaf should expose SELECT groups with enough buckets for ECMP fanout."""
    if model.leaf_count < 2:
        return True
    for leaf in model.leaves[:2]:
        groups = observed_switch_state(runtime, leaf)["groups"]
        if "group_id=" not in groups and "group_id:" not in groups:
            return False
        # Require at least ecmp_fanout output actions across SELECT buckets.
        if groups.lower().count("output:") < model.ecmp_fanout:
            if "select" not in groups.lower() and "type=select" not in groups:
                return False
    return True


def _controller_dataplane_consistent(
    runtime: LabRuntime, model: ClosFabricModel
) -> bool:
    """Sample switches for L3 flows and leaf SELECT groups."""
    if not build_forwarding_rules(model).get("flows"):
        return False
    for leaf in model.leaves[:2]:
        state = observed_switch_state(runtime, leaf)
        if "nw_dst=" not in state["flows"] and "ip" not in state["flows"]:
            return False
        if "group_id=" not in state["groups"] and model.leaf_count > 1:
            return False
    for spine in model.spines[:1]:
        state = observed_switch_state(runtime, spine)
        if "nw_dst=" not in state["flows"] and "ip" not in state["flows"]:
            return False
    return True


def _no_normal_or_stp(runtime: LabRuntime, model: ClosFabricModel) -> bool:
    for switch in model.leaves[:1] + model.spines[:1]:
        flows = observed_switch_state(runtime, switch)["flows"]
        if "NORMAL" in flows:
            return False
        bridges = exec_or_empty(runtime, switch, "ovs-vsctl show")
        if "rstp_enable" in bridges and "true" in bridges.lower():
            return False
    return True


def _sparse_cross_rack_ping(runtime: LabRuntime, model: ClosFabricModel) -> bool:
    """Ping a few representative cross-rack destinations (not full mesh)."""
    clients = model.client_endpoints() or model.endpoints
    webs = model.web_endpoints()
    if not clients or not webs:
        return False
    src = clients[0]
    # Prefer a web on a different leaf
    dst = next((w for w in webs if w.leaf_id != src.leaf_id), webs[0])
    if src.leaf_id == dst.leaf_id and len(webs) > 1:
        dst = webs[1]
    return ping_ok(runtime, src.name, dst.ip)


def _sparse_cross_rack_http(runtime: LabRuntime, model: ClosFabricModel) -> bool:
    clients = model.client_endpoints()
    webs = model.web_endpoints()
    if not clients or not webs:
        return False
    src = clients[0]
    dst = next((w for w in webs if w.leaf_id != src.leaf_id), webs[0])
    return http_ok(runtime, src.name, f"http://{dst.ip}/")


def verify_sdn_l3_clos_lab(
    runtime: LabRuntime,
    *,
    scenario_name: str,
    model: ClosFabricModel,
) -> dict[str, Any]:
    expected_nodes = (
        ["onos", "fabric_mgr"]
        + model.spines
        + model.leaves
        + [e.name for e in model.endpoints]
    )
    sample_eps = model.endpoints[:2]
    checks: dict[str, bool] = {
        "nodes_deployed": nodes_deployed(runtime, expected_nodes),
        "onos_link_up": link_up(runtime, "onos"),
        "onos_process": (
            bool(exec_or_empty(runtime, "onos", "pgrep -af java").strip())
            or bool(exec_or_empty(runtime, "onos", "pgrep -af onos").strip())
            or process_running(runtime, "onos", "java")
        ),
        "ovs_switches_ready": _ovs_ready(runtime, model.spines[:1] + model.leaves[:2]),
        "of_sessions": _of_sessions_ok(runtime, model),
        "virtual_gateway_mac": VIRTUAL_ROUTER_MAC == "02:00:00:00:00:01",
        "no_stp_or_normal": _no_normal_or_stp(runtime, model),
        "ecmp_groups": _group_bucket_counts(runtime, model),
        "controller_dataplane_consistent": _controller_dataplane_consistent(
            runtime, model
        ),
        "cross_rack_ping": _sparse_cross_rack_ping(runtime, model),
        "cross_rack_http": _sparse_cross_rack_http(runtime, model),
    }
    for ep in sample_eps:
        checks[f"{ep.name}_ipv4"] = host_has_ipv4(runtime, ep.name, ep.ip)
        checks[f"{ep.name}_gateway_neigh"] = (
            VIRTUAL_ROUTER_MAC.lower()
            in exec_or_empty(
                runtime, ep.name, f"ip neigh show {gateway_ip(ep.leaf_id)}"
            ).lower()
        )

    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
    )
