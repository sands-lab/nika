"""Startup verification for p4_dc_fabric."""

from __future__ import annotations

from typing import Any

from nika.net_env.p4_dc_fabric.fabric_manager.apply import (
    load_intent,
    run_manager,
)
from nika.net_env.p4_dc_fabric.topology_model import (
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
from nika.net_env.verify_collection import (
    VerifyCommand,
    collect_verify_commands,
    ping_command,
    http_command,
)

_UDP_FLOWS = 48


def _grpc_ready(runtime: LabRuntime, switches: list[str]) -> bool:
    return all(
        process_running(runtime, name, "simple_switch_grpc") for name in switches
    )


def _oob_ok(runtime: LabRuntime, model: ClosFabricModel) -> bool:
    sample = model.leaves[:1] + model.spines[:1]
    return all(
        ping_ok(runtime, "fabric_mgr", model.switch_info[name].oob_ip)
        for name in sample
    )


def _interfaces_up(runtime: LabRuntime, model: ClosFabricModel) -> bool:
    for switch in model.leaves[:1] + model.spines[:1]:
        for port in model.ports[switch][:2]:
            if not link_up(runtime, switch, port.name):
                return False
    return True


def _p4runtime_consistent(
    runtime: LabRuntime, model: ClosFabricModel, *, sample: bool
) -> tuple[bool, dict[str, Any]]:
    names = list(model.fabric_switches())
    if sample:
        names = model.leaves[:2] + model.spines[:1]
    details: dict[str, Any] = {}
    ok = True
    payload = run_manager(
        runtime, "read", "--switches", *names, timeout=60 * max(1, len(names))
    )
    for name in names:
        switch = (payload.get("switches") or {}).get(name) or {}
        pipeline = switch.get("pipeline") or {}
        mismatches = switch.get("mismatches") or []
        row = {
            "pipeline_ok": bool(pipeline.get("ok")),
            "mismatches": mismatches,
            "ipv4_lpm": len(switch.get("ipv4_lpm") or []),
            "groups": len(switch.get("groups") or []),
            "members": len(switch.get("members") or []),
        }
        details[name] = row
        if mismatches or not pipeline.get("ok"):
            ok = False
    return ok, details


def _same_rack_ping(runtime: LabRuntime, model: ClosFabricModel) -> bool:
    webs = model.web_endpoints()
    clients = model.client_endpoints()
    if not webs or not clients:
        return False
    web = webs[0]
    client = next((c for c in clients if c.leaf_id == web.leaf_id), None)
    if client is None:
        return ping_ok(runtime, web.name, gateway_ip(web.leaf_id))
    return ping_ok(runtime, client.name, web.ip)


def _sparse_cross_rack_ping(runtime: LabRuntime, model: ClosFabricModel) -> bool:
    clients = model.client_endpoints() or model.endpoints
    webs = model.web_endpoints()
    if not clients or not webs:
        return False
    src = clients[0]
    dst = next((w for w in webs if w.leaf_id != src.leaf_id), webs[0])
    return ping_ok(runtime, src.name, dst.ip)


def _multi_rack_ping(runtime: LabRuntime, model: ClosFabricModel) -> bool:
    webs = model.web_endpoints()
    clients = model.client_endpoints() or model.endpoints
    if len(webs) < 2 or not clients:
        return False
    src = clients[0]
    pairs = [w for w in webs if w.leaf_id != src.leaf_id][:3]
    return all(ping_ok(runtime, src.name, w.ip) for w in pairs)


def _sparse_cross_rack_http(runtime: LabRuntime, model: ClosFabricModel) -> bool:
    clients = model.client_endpoints()
    webs = model.web_endpoints()
    if not clients or not webs:
        return False
    src = clients[0]
    dst = next((w for w in webs if w.leaf_id != src.leaf_id), webs[0])
    return http_ok(runtime, src.name, f"http://{dst.ip}/")


def _counter_packets(value: object) -> int:
    if isinstance(value, dict):
        return int(value.get("packets") or 0)
    return int(value or 0)


def _read_spine_ingress(
    runtime: LabRuntime, model: ClosFabricModel, leaf: str
) -> dict[str, int]:
    payload = run_manager(runtime, "counters", timeout=60)
    counters = payload.get("counters") or {}
    used: dict[str, int] = {}
    for spine in model.spines:
        port = model.port_to_peer(spine, leaf)
        if port is None:
            continue
        ingress = (counters.get(spine) or {}).get("ingress") or {}
        used[spine] = _counter_packets(
            ingress.get(str(port.bmv2_port), ingress.get(port.bmv2_port, 0))
        )
    return used


def _ecmp_multi_path(
    runtime: LabRuntime, model: ClosFabricModel
) -> tuple[bool, dict[str, Any]]:
    if model.spine_count < 2 or model.leaf_count < 2:
        return True, {"skipped": True}
    src = model.client_endpoints()[0]
    dst = next(w for w in model.web_endpoints() if w.leaf_id != src.leaf_id)
    leaf = f"leaf_{src.leaf_id}"
    before = _read_spine_ingress(runtime, model, leaf)
    gen = (
        "python3 - <<'PY'\n"
        "import socket\n"
        f"dst={dst.ip!r}\n"
        f"n={_UDP_FLOWS}\n"
        "for port in range(20000, 20000 + n):\n"
        "    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)\n"
        "    s.bind(('', port))\n"
        "    s.sendto(b'ecmp-probe', (dst, 9))\n"
        "    s.close()\n"
        "PY"
    )
    exec_or_empty(runtime, src.name, gen, timeout=30)
    after = _read_spine_ingress(runtime, model, leaf)
    deltas = {name: max(0, after.get(name, 0) - before.get(name, 0)) for name in after}
    used = [name for name, delta in deltas.items() if delta > 0]
    details = {"before": before, "after": after, "deltas": deltas, "used": used}
    ok = len(used) >= 2
    return ok, details


def _collect(runtime, model, *, full):
    commands = [
        VerifyCommand(switch, "pgrep -x simple_switch_grpc", 10)
        for switch in model.leaves[:2] + model.spines[:1]
    ]
    if full:
        for switch in model.leaves[:1] + model.spines[:1]:
            commands.append(
                VerifyCommand(
                    "fabric_mgr", ping_command(model.switch_info[switch].oob_ip), 15
                )
            )
            commands.extend(
                VerifyCommand(switch, f"cat /sys/class/net/{port.name}/operstate", 10)
                for port in model.ports[switch][:2]
            )
        commands.extend(
            VerifyCommand(ep.name, "ip -4 -o addr show dev eth0", 10)
            for ep in model.endpoints[:2]
        )
        clients, webs = (
            model.client_endpoints() or model.endpoints,
            model.web_endpoints(),
        )
        if clients and webs:
            web, source = webs[0], clients[0]
            same = next(
                (client for client in clients if client.leaf_id == web.leaf_id), None
            )
            commands.append(
                VerifyCommand(
                    same.name if same else web.name,
                    ping_command(web.ip if same else gateway_ip(web.leaf_id)),
                    15,
                )
            )
            destination = next(
                (web for web in webs if web.leaf_id != source.leaf_id), webs[0]
            )
            commands.append(
                VerifyCommand(source.name, ping_command(destination.ip), 15)
            )
            commands.append(
                VerifyCommand(source.name, http_command(f"http://{destination.ip}/"))
            )
            commands.extend(
                VerifyCommand(source.name, ping_command(web.ip), 15)
                for web in [web for web in webs if web.leaf_id != source.leaf_id][:3]
            )
    return collect_verify_commands(runtime, commands)


def verify_p4_dc_fabric_lab_startup(
    runtime: LabRuntime,
    *,
    scenario_name: str,
    model: ClosFabricModel,
) -> dict[str, Any]:
    expected_nodes = (
        ["fabric_mgr"] + model.spines + model.leaves + [e.name for e in model.endpoints]
    )
    p4_ok, p4_details = _p4runtime_consistent(runtime, model, sample=True)
    runtime = _collect(runtime, model, full=False)
    checks: dict[str, bool] = {
        "nodes_deployed": nodes_deployed(runtime, expected_nodes),
        "bmv2_grpc_ready": _grpc_ready(runtime, model.leaves[:2] + model.spines[:1]),
        "p4runtime_consistent": p4_ok,
    }
    return runtime.finish(
        build_lab_verify_result(
            scenario_name=scenario_name,
            verified=all(checks.values()),
            checks=checks,
            details={"p4runtime": p4_details},
        )
    )


def verify_p4_dc_fabric_lab(
    runtime: LabRuntime,
    *,
    scenario_name: str,
    model: ClosFabricModel,
) -> dict[str, Any]:
    expected_nodes = (
        ["fabric_mgr"] + model.spines + model.leaves + [e.name for e in model.endpoints]
    )
    sample = model.topo_size != "s"
    p4_ok, p4_details = _p4runtime_consistent(runtime, model, sample=sample)
    ecmp_ok, ecmp_details = _ecmp_multi_path(runtime, model)
    sample_eps = model.endpoints[:2]
    try:
        intent = load_intent(runtime)
        intent_ok = bool(intent.get("switches"))
    except Exception:
        intent = {}
        intent_ok = False

    live_runtime = runtime
    runtime = _collect(runtime, model, full=True)
    checks: dict[str, bool] = {
        "nodes_deployed": nodes_deployed(runtime, expected_nodes),
        "bmv2_grpc_ready": _grpc_ready(runtime, model.leaves[:2] + model.spines[:1]),
        "control_network": _oob_ok(runtime, model),
        "interfaces_up": _interfaces_up(runtime, model),
        "intent_present": intent_ok,
        "p4runtime_consistent": p4_ok,
        "same_rack_ping": _same_rack_ping(runtime, model),
        "cross_rack_ping": _sparse_cross_rack_ping(runtime, model),
        "multi_rack_ping": _multi_rack_ping(runtime, model),
        "cross_rack_http": _sparse_cross_rack_http(runtime, model),
        "ecmp_multi_path": ecmp_ok,
    }
    runtime = runtime.merged(
        collect_verify_commands(
            live_runtime,
            [
                VerifyCommand(ep.name, f"ip neigh show {gateway_ip(ep.leaf_id)}", 10)
                for ep in sample_eps
            ],
            nodes=runtime.nodes,
        )
    )
    for ep in sample_eps:
        checks[f"{ep.name}_ipv4"] = host_has_ipv4(runtime, ep.name, ep.ip)
        checks[f"{ep.name}_gateway_neigh"] = (
            VIRTUAL_ROUTER_MAC.lower()
            in exec_or_empty(
                runtime, ep.name, f"ip neigh show {gateway_ip(ep.leaf_id)}"
            ).lower()
        )

    return runtime.finish(
        build_lab_verify_result(
            scenario_name=scenario_name,
            verified=all(checks.values()),
            checks=checks,
            details={
                "p4runtime": p4_details,
                "ecmp": ecmp_details,
                "intent_pipeline": intent.get("pipeline"),
            },
        )
    )
