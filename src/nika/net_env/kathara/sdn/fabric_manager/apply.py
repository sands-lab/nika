"""Apply and reconcile L3 Clos forwarding via ONOS (live OF sessions)."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from nika.net_env.kathara.sdn.fabric_manager.forwarding_rules import (
    build_forwarding_rules,
)
from nika.net_env.kathara.sdn.topology_model import (
    ONOS_OF_PORT,
    ONOS_OOB_IP,
    ONOS_REST_PORT,
    ClosFabricModel,
    dpid_for_leaf,
    dpid_for_spine,
)
from nika.runtime.base import LabRuntime
from nika.utils.logger import system_logger

logger = system_logger

_ONOS_AUTH = "onos:rocks"
_REST_APP = "org.onosproject.rest"


def _exec(runtime: LabRuntime, host: str, cmd: str, timeout: float = 60.0) -> str:
    return runtime.exec(host, cmd, timeout=timeout)


def _onos_url(path: str) -> str:
    return f"http://{ONOS_OOB_IP}:{ONOS_REST_PORT}{path}"


def _onos_get(runtime: LabRuntime, path: str, *, timeout: float = 30.0) -> str:
    return _exec(
        runtime,
        "fabric_mgr",
        f"curl -s -u {_ONOS_AUTH} --connect-timeout 3 '{_onos_url(path)}' || echo '{{}}'",
        timeout=timeout,
    )


def _onos_json(runtime: LabRuntime, path: str) -> Any:
    raw = _onos_get(runtime, path)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _onos_request(
    runtime: LabRuntime,
    method: str,
    path: str,
    data: dict[str, Any] | None = None,
    *,
    timeout: float = 60.0,
) -> str:
    if data is None:
        return _exec(
            runtime,
            "fabric_mgr",
            f"curl -s -u {_ONOS_AUTH} -X {method} '{_onos_url(path)}' || true",
            timeout=timeout,
        )
    payload = base64.b64encode(json.dumps(data).encode()).decode()
    cmd = (
        f"echo {payload} | base64 -d > /tmp/onos_body.json && "
        f"curl -s -u {_ONOS_AUTH} -H 'Content-Type: application/json' "
        f"-X {method} '{_onos_url(path)}' --data-binary @/tmp/onos_body.json || true"
    )
    return _exec(runtime, "fabric_mgr", cmd, timeout=timeout)


def wait_for_onos(runtime: LabRuntime, *, timeout_sec: float = 180.0) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        out = _onos_get(runtime, "/onos/v1/devices")
        if "devices" in out and "FAIL" not in out:
            return True
        time.sleep(3)
    return False


def activate_onos_apps(runtime: LabRuntime) -> None:
    """Keep discovery + FlowRule/Group providers; disable reactive L2 fwd."""
    for app in (
        "org.onosproject.drivers",
        "org.onosproject.openflow-base",
        "org.onosproject.openflow",
        "org.onosproject.lldpprovider",
        "org.onosproject.hostprovider",
    ):
        _onos_request(runtime, "POST", f"/onos/v1/applications/{app}/active")
    for app in (
        "org.onosproject.fwd",
        "org.onosproject.reactive",
    ):
        _onos_request(runtime, "DELETE", f"/onos/v1/applications/{app}/active")


def onos_topology_snapshot(runtime: LabRuntime) -> dict[str, Any]:
    return {
        "devices": _onos_json(runtime, "/onos/v1/devices"),
        "links": _onos_json(runtime, "/onos/v1/links"),
        "hosts": _onos_json(runtime, "/onos/v1/hosts"),
    }


def onos_flow_snapshot(runtime: LabRuntime) -> dict[str, Any]:
    return _onos_json(runtime, "/onos/v1/flows")


def onos_group_snapshot(runtime: LabRuntime) -> dict[str, Any]:
    return _onos_json(runtime, "/onos/v1/groups")


def wait_for_of_sessions(
    runtime: LabRuntime, model: ClosFabricModel, *, timeout_sec: float = 180.0
) -> bool:
    expected = set(model.expected_device_ids())
    min_links = max(1, model.expected_leaf_spine_link_count() // 2)
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        snap = onos_topology_snapshot(runtime)
        devices = snap.get("devices", {}).get("devices", [])
        available = {
            d.get("id")
            for d in devices
            if d.get("available") and d.get("id") in expected
        }
        link_count = len(snap.get("links", {}).get("links", []))
        if available == expected and link_count >= min_links:
            return True
        time.sleep(3)
    return False


def ensure_controllers_attached(runtime: LabRuntime, model: ClosFabricModel) -> None:
    for switch in model.leaves + model.spines:
        _exec(
            runtime,
            switch,
            f"ovs-vsctl set-controller {switch} "
            f"tcp:{ONOS_OOB_IP}:{ONOS_OF_PORT} || true",
        )


def _set_dpid(runtime: LabRuntime, switch: str, dpid_hex: str) -> None:
    _exec(
        runtime,
        switch,
        f"ovs-vsctl set bridge {switch} other-config:datapath-id={dpid_hex}",
    )


def _ofport_map(
    runtime: LabRuntime, switch: str, model: ClosFabricModel
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for port in model.ports.get(switch, []):
        raw = _exec(
            runtime,
            switch,
            f"ovs-vsctl get Interface {port.name} ofport 2>/dev/null || echo -1",
        ).strip()
        if raw.lstrip("-").isdigit() and int(raw) > 0:
            mapping[port.name] = raw
    return mapping


def _resolve_ofport(port_map: dict[str, str], token: str) -> str:
    token = token.strip()
    if token.upper() == "IN_PORT" or token == "in_port":
        return "IN_PORT"
    if token.isdigit():
        return token
    if token in port_map:
        return port_map[token]
    # ethN without map entry
    raise KeyError(f"unknown OpenFlow port token {token!r}")


def _parse_match(match: str, port_map: dict[str, str]) -> list[dict[str, Any]] | None:
    """Convert ovs-ofctl match string to ONOS selector criteria. None = skip."""
    criteria: list[dict[str, Any]] = []
    for part in match.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "ip":
            criteria.append({"type": "ETH_TYPE", "ethType": "0x800"})
        elif part == "arp":
            criteria.append({"type": "ETH_TYPE", "ethType": "0x806"})
        elif part.startswith("nw_dst="):
            ip = part.split("=", 1)[1]
            if "/" not in ip:
                ip = f"{ip}/32"
            criteria.append({"type": "IPV4_DST", "ip": ip})
        elif part.startswith("in_port="):
            port = _resolve_ofport(port_map, part.split("=", 1)[1])
            criteria.append({"type": "IN_PORT", "port": port})
        elif part.startswith("arp_tpa=") or part.startswith("arp_op="):
            # Nicira / detailed ARP matches are skipped (endpoints use static GW neigh).
            return None
        else:
            return None
    return criteria or None


def _parse_actions(
    actions: str, port_map: dict[str, str]
) -> list[dict[str, Any]] | None:
    """Convert ovs-ofctl actions to ONOS treatment instructions. None = skip."""
    if "move:" in actions or "load:" in actions:
        return None
    instructions: list[dict[str, Any]] = []
    if actions.strip() == "drop":
        return instructions
    for part in actions.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "dec_ttl":
            instructions.append({"type": "L3MODIFICATION", "subtype": "DEC_TTL"})
        elif part.startswith("mod_dl_src:"):
            instructions.append(
                {
                    "type": "L2MODIFICATION",
                    "subtype": "ETH_SRC",
                    "mac": part.split(":", 1)[1],
                }
            )
        elif part.startswith("mod_dl_dst:"):
            instructions.append(
                {
                    "type": "L2MODIFICATION",
                    "subtype": "ETH_DST",
                    "mac": part.split(":", 1)[1],
                }
            )
        elif part.startswith("group:"):
            instructions.append(
                {"type": "GROUP", "groupId": int(part.split(":", 1)[1])}
            )
        elif part.startswith("output:"):
            for port_tok in part.split(":", 1)[1].split(","):
                instructions.append(
                    {
                        "type": "OUTPUT",
                        "port": _resolve_ofport(port_map, port_tok),
                    }
                )
        elif part == "in_port":
            instructions.append({"type": "OUTPUT", "port": "IN_PORT"})
        else:
            return None
    return instructions


def _onos_batch(
    runtime: LabRuntime,
    ops: list[tuple[str, str, dict[str, Any] | None]],
    *,
    timeout: float = 300.0,
) -> str:
    """Run many ONOS REST calls inside fabric_mgr (one Kathara exec)."""
    payload = base64.b64encode(json.dumps(ops).encode()).decode()
    script = r"""
import base64, json, urllib.request, sys
auth = base64.b64encode(b"onos:rocks").decode()
ops = json.loads(base64.b64decode(sys.argv[1]))
base = sys.argv[2]
out = []
for method, path, body in ops:
    url = base + path
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Basic " + auth)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            out.append({"path": path, "status": resp.status})
    except Exception as exc:  # noqa: BLE001
        code = getattr(getattr(exc, "code", None), "real", None) or getattr(exc, "code", None)
        out.append({"path": path, "error": str(exc), "status": code})
print(json.dumps(out))
"""
    b64_script = base64.b64encode(script.encode()).decode()
    cmd = (
        f"echo {b64_script} | base64 -d > /tmp/onos_batch.py && "
        f"python3 /tmp/onos_batch.py {payload} '{_onos_url('')}'"
    )
    return _exec(runtime, "fabric_mgr", cmd, timeout=timeout)


def _group_app_cookie(group_id: int) -> str:
    # ONOS appCookie must be a hex token (0x...).
    return f"0x4e100000{int(group_id) & 0xFFFF:04x}"


def _clear_rest_flows_groups(runtime: LabRuntime, device_ids: list[str]) -> None:
    ops: list[tuple[str, str, dict[str, Any] | None]] = []
    for device_id in device_ids:
        for flow in _onos_json(runtime, f"/onos/v1/flows/{device_id}").get("flows", []):
            if flow.get("appId") != _REST_APP:
                continue
            fid = flow.get("id")
            if fid is None:
                continue
            ops.append(("DELETE", f"/onos/v1/flows/{device_id}/{fid}", None))
        for group in _onos_json(runtime, f"/onos/v1/groups/{device_id}").get(
            "groups", []
        ):
            cookie = group.get("appCookie")
            if not cookie:
                continue
            ops.append(("DELETE", f"/onos/v1/groups/{device_id}/{cookie}", None))
    if ops:
        _onos_batch(runtime, ops)


def _install_onos_group_body(
    group: dict[str, Any], port_map: dict[str, str]
) -> dict[str, Any]:
    buckets = []
    for bucket in group["buckets"]:
        instructions = _parse_actions(bucket["actions"], port_map)
        if instructions is None:
            raise ValueError(f"unsupported group bucket actions: {bucket['actions']}")
        buckets.append({"weight": 1, "treatment": {"instructions": instructions}})
    return {
        "type": "SELECT",
        "appCookie": _group_app_cookie(int(group["group_id"])),
        "groupId": str(group["group_id"]),
        "buckets": buckets,
    }


def _install_onos_flow_body(
    flow: dict[str, Any], port_map: dict[str, str]
) -> dict[str, Any] | None:
    criteria = _parse_match(flow["match"], port_map)
    instructions = _parse_actions(flow["actions"], port_map)
    if criteria is None or instructions is None:
        return None
    return {
        "priority": int(flow.get("priority", 1000)),
        "timeout": 0,
        "isPermanent": True,
        "deviceId": flow["device_id"],
        "treatment": {"instructions": instructions},
        "selector": {"criteria": criteria},
    }


def _fabric_programmed(runtime: LabRuntime, model: ClosFabricModel) -> bool:
    if not model.leaves:
        return False
    state = observed_switch_state(runtime, model.leaves[0])
    return "group_id=" in state.get("groups", "") and (
        "nw_dst=" in state.get("flows", "") or "ip,nw_dst" in state.get("flows", "")
    )


def _controllers_attached(runtime: LabRuntime, model: ClosFabricModel) -> bool:
    if not model.leaves:
        return False
    ctrl = _exec(
        runtime,
        model.leaves[0],
        f"ovs-vsctl get-controller {model.leaves[0]} 2>/dev/null || true",
    )
    return ONOS_OOB_IP in ctrl and str(ONOS_OF_PORT) in ctrl


def apply_forwarding(runtime: LabRuntime, model: ClosFabricModel) -> dict[str, Any]:
    """Install proactive L3+ECMP rules through ONOS while OF sessions stay up."""
    rules = build_forwarding_rules(model)

    for leaf_id, leaf in enumerate(model.leaves, start=1):
        _set_dpid(runtime, leaf, dpid_for_leaf(leaf_id))
    for spine_id, spine in enumerate(model.spines, start=1):
        _set_dpid(runtime, spine, dpid_for_spine(spine_id))

    port_maps = {
        switch: _ofport_map(runtime, switch, model)
        for switch in model.leaves + model.spines
    }

    device_ids = sorted(
        {
            *(f["device_id"] for f in rules["flows"]),
            *(g["device_id"] for g in rules["groups"]),
        }
    )
    _clear_rest_flows_groups(runtime, device_ids)

    group_ops: list[tuple[str, str, dict[str, Any] | None]] = []
    for group in rules["groups"]:
        try:
            body = _install_onos_group_body(group, port_maps[group["switch"]])
            group_ops.append(("POST", f"/onos/v1/groups/{group['device_id']}", body))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ONOS group build failed on %s: %s", group["switch"], exc)
    if group_ops:
        result = _onos_batch(runtime, group_ops)
        logger.info("ONOS group install batch: %s", result[:500])

    deadline = time.time() + 90
    expected_groups = len(group_ops)
    while time.time() < deadline:
        groups = onos_group_snapshot(runtime).get("groups", [])
        rest_groups = [g for g in groups if g.get("appId") == _REST_APP]
        pending = [g for g in rest_groups if g.get("state") not in ("ADDED",)]
        added = [g for g in rest_groups if g.get("state") == "ADDED"]
        if expected_groups == 0:
            break
        if len(added) >= expected_groups and not pending:
            break
        time.sleep(1)
    else:
        logger.warning("some ONOS groups still pending before flow install")

    flow_ops: list[tuple[str, str, dict[str, Any] | None]] = []
    skipped = 0
    for flow in rules["flows"]:
        try:
            body = _install_onos_flow_body(flow, port_maps[flow["switch"]])
            if body is None:
                skipped += 1
                continue
            flow_ops.append(("POST", f"/onos/v1/flows/{flow['device_id']}", body))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ONOS flow build failed on %s: %s", flow["switch"], exc)
    if flow_ops:
        result = _onos_batch(runtime, flow_ops)
        logger.info("ONOS flow install batch: %s", result[:500])
    if skipped:
        logger.info(
            "skipped %s Nicira/unsupported flows (static GW ARP covers hosts)",
            skipped,
        )

    return rules


def reconcile_fabric(
    runtime: LabRuntime,
    model: ClosFabricModel,
    *,
    wait_onos: bool = True,
) -> dict[str, Any]:
    """Discover topology on live ONOS, program forwarding, keep OF attached."""
    if _fabric_programmed(runtime, model) and _controllers_attached(runtime, model):
        return build_forwarding_rules(model)

    if wait_onos:
        if not wait_for_onos(runtime):
            logger.warning("ONOS REST not ready; applying rules anyway")
        else:
            activate_onos_apps(runtime)
            ensure_controllers_attached(runtime, model)
            if not wait_for_of_sessions(runtime, model, timeout_sec=120):
                logger.warning("ONOS OF discovery incomplete; applying rules anyway")
    else:
        ensure_controllers_attached(runtime, model)

    return apply_forwarding(runtime, model)


def prune_groups_for_down_link(
    runtime: LabRuntime,
    model: ClosFabricModel,
    *,
    leaf: str,
    spine: str,
) -> dict[str, Any]:
    """Drop ECMP buckets that would use a down leaf–spine link (both directions)."""
    from nika.net_env.kathara.sdn.topology_model import rack_prefix

    rules = build_forwarding_rules(model)
    failed_prefix = rack_prefix(model.leaf_id(leaf))
    port_maps = {
        switch: _ofport_map(runtime, switch, model)
        for switch in model.leaves + model.spines
    }
    ops: list[tuple[str, str, dict[str, Any] | None]] = []
    for group in rules["groups"]:
        if group["switch"] != leaf and group.get("prefix") != failed_prefix:
            continue
        group["buckets"] = [b for b in group["buckets"] if b.get("spine") != spine]
        if not group["buckets"]:
            continue
        device_id = group["device_id"]
        cookie = _group_app_cookie(int(group["group_id"]))
        ops.append(("DELETE", f"/onos/v1/groups/{device_id}/{cookie}", None))
        try:
            body = _install_onos_group_body(group, port_maps[group["switch"]])
            ops.append(("POST", f"/onos/v1/groups/{device_id}", body))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ONOS group prune failed on %s: %s", group["switch"], exc)
    if ops:
        _onos_batch(runtime, ops)
    return rules


def observed_switch_state(runtime: LabRuntime, switch: str) -> dict[str, str]:
    flows = _exec(
        runtime,
        switch,
        f"ovs-ofctl -O OpenFlow13 dump-flows {switch} 2>/dev/null || true",
    )
    groups = _exec(
        runtime,
        switch,
        f"ovs-ofctl -O OpenFlow13 dump-groups {switch} 2>/dev/null || true",
    )
    ports = _exec(
        runtime,
        switch,
        f"ovs-ofctl -O OpenFlow13 dump-ports {switch} 2>/dev/null || true",
    )
    return {"flows": flows, "groups": groups, "ports": ports}


__all__ = [
    "ONOS_OF_PORT",
    "activate_onos_apps",
    "apply_forwarding",
    "ensure_controllers_attached",
    "onos_flow_snapshot",
    "onos_group_snapshot",
    "onos_topology_snapshot",
    "observed_switch_state",
    "prune_groups_for_down_link",
    "reconcile_fabric",
    "wait_for_of_sessions",
    "wait_for_onos",
]
