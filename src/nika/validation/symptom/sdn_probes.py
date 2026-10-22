"""SDN flow-rule symptom probes (test-path only).

A faulty rule on one fabric switch only hits the ECMP share hashed onto it, so
the probes send many cross-rack UDP flows with distinct source ports and count
how many arrive. The loss threshold is half of that expected share, and every
predicate is also evaluated on the healthy lab (baseline and sham).

Shadowing: a drop rule for one rack prefix on the switch. Flows to that
prefix are lost (all of them through a leaf, the ECMP share through a spine)
and the rule's counter matches the lost packets, while flows from the same
source to another rack through the same switch are the control path.

Controller loss (crash, blocked or mismatched southbound port): the fabric is
programmed proactively and the switches fail secure, so installed flows keep
forwarding. What breaks is the control loop: every switch reports its
controller channel down, and a flow requested through the controller never
reaches a switch. Cross-rack traffic over the installed flows is the control.

Loop: both ends of one leaf-spine link bounce IPv4 back out the ingress port.
Flows hashed onto that link are lost, and each lost packet crosses the link
until its TTL expires, so the link carries many packets per offered flow. The
control sends from the same source to a rack that does not use the link.
"""

from __future__ import annotations

import re
import shlex
import time
from typing import Any

from nika.net_env.sdn_l3_clos.fabric_manager.apply import onos_flow_snapshot
from nika.net_env.sdn_l3_clos.topology_model import ONOS_OOB_IP, ONOS_REST_PORT
from nika.net_env.verify import ping_stats
from nika.problems.base import build_verify_result
from nika.problems.forwarding_encapsulation_policy.flow_rules import (
    _device_id_for_switch,
    _onos_post_flow,
)
from nika.problems.management_orchestration_plane.sdn import _switch_controller_state

_PORT = 19611
_FLOWS = 256
_LOG = "/tmp/probe-shadow.log"


# A healthy sweep may lose this many flows to host noise.
_HEALTHY_LOSS_PERCENT = 1.0
# Loop evidence: packets the faulted link carries per offered flow.
_LOOP_PACKETS_PER_FLOW = 5.0


def _min_loss_percent(model: Any, switch: str) -> float:
    """Half the share of flows that cross ``switch``; a leaf carries them all."""
    share = 1.0 if switch in model.leaves else 1.0 / max(1, len(model.spines))
    return 50.0 * share


def _ends(problem: Any, params: Any) -> dict[str, Any]:
    """Source, a web server in the shadowed rack, and a control web server."""
    model = problem.net_env.model
    switch = params.host_name
    leaf = model.leaf_id(switch) if switch in model.leaves else None
    shadowed = int(problem.shadowed_prefix(params).split(".")[2])
    source = next(
        (
            e
            for e in model.client_endpoints()
            if e.leaf_id != shadowed and leaf in (None, e.leaf_id)
        ),
        None,
    )
    target = next((e for e in model.web_endpoints() if e.leaf_id == shadowed), None)
    control = next(
        (
            e
            for e in model.web_endpoints()
            if source is not None and e.leaf_id not in (shadowed, source.leaf_id)
        ),
        None,
    )
    if source is None or target is None or control is None:
        return {}
    return {"source": source, "target": target, "control": control}


def _drop_packets(problem: Any, params: Any) -> int | None:
    for line in problem._shadow_lines(params):
        match = re.search(r"\bn_packets=(\d+)", line)
        if match:
            return int(match.group(1))
    return None


def _flows(problem: Any, source: Any, target: Any) -> dict[str, Any]:
    runtime = problem.runtime
    listener = (
        "import socket\n"
        "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
        f"s.bind(('0.0.0.0',{_PORT}))\n"
        f"log=open({_LOG!r},'a',buffering=1)\n"
        "while True:\n"
        "  d,_=s.recvfrom(256)\n"
        "  log.write(d.decode('ascii')+'\\n')\n"
    )
    started = problem.__dict__.setdefault("_audit_shadow_listeners", set())
    if target.name not in started:
        started.add(target.name)
        runtime.exec(
            target.name,
            f"nohup python3 -u -c {shlex.quote(listener)} >/dev/null 2>&1 </dev/null &",
        )
        for _ in range(20):
            bound = runtime.exec(target.name, f"ss -Hlun 'sport = :{_PORT}'")
            if bound.strip():
                break
            time.sleep(0.25)
    label = f"s{time.monotonic_ns()}"
    sender = (
        "import socket,time\n"
        f"for i in range({_FLOWS}):\n"
        "  s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
        "  s.bind(('0.0.0.0',31000+i))\n"
        f"  s.sendto(f'{label}:{{i}}'.encode(),({target.ip!r},{_PORT}))\n"
        "  s.close()\n"
        "  time.sleep(0.005)\n"
    )
    runtime.exec(source.name, f"python3 -c {shlex.quote(sender)}", timeout=30)
    time.sleep(1.0)
    got = runtime.exec(target.name, f"grep -c '^{label}:' {_LOG} 2>/dev/null || true")
    received = int((got.strip().splitlines() or ["0"])[-1] or 0)
    return {
        "source": source.name,
        "target": target.name,
        "sent": _FLOWS,
        "received": received,
        "loss_percent": 100 * (_FLOWS - received) / _FLOWS,
    }


def _sample(problem: Any, params: Any) -> dict[str, Any] | None:
    ends = _ends(problem, params)
    if not ends:
        return None
    before = _drop_packets(problem, params)
    path = _flows(problem, ends["source"], ends["target"])
    after = _drop_packets(problem, params)
    control = _flows(problem, ends["source"], ends["control"])
    return {
        "path": path,
        "control": control,
        "min_loss_percent": _min_loss_percent(problem.net_env.model, params.host_name),
        "lost_packets": path["sent"] - path["received"],
        "drop_packets_before": before,
        "drop_packets_after": after,
    }


def flow_shadow_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    sample = _sample(problem, params)
    if sample is None:
        return False, {"error": "no_cross_rack_probe_path"}
    healthy = (
        sample["path"]["loss_percent"] <= _HEALTHY_LOSS_PERCENT
        and sample["control"]["loss_percent"] <= _HEALTHY_LOSS_PERCENT
    )
    return healthy, sample


def flow_shadow(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Flows to the shadowed prefix stop arriving; other prefixes still do."""
    sample = _sample(problem, params)
    if sample is None:
        return False, {"error": "no_cross_rack_probe_path"}
    control_ok = sample["control"]["loss_percent"] <= _HEALTHY_LOSS_PERCENT
    before, after = sample["drop_packets_before"], sample["drop_packets_after"]
    # Unlike a loop, each lost packet is counted once by the rule and nowhere else.
    counted = (
        before is not None
        and after is not None
        and after - before >= 0.9 * sample["lost_packets"]
    )
    verified = (
        sample["path"]["loss_percent"] >= sample["min_loss_percent"]
        and control_ok
        and counted
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={**sample, "control_ok": control_ok, "counted": counted},
    )


def _link_packets(problem: Any, node: str, intf: str) -> int | None:
    out = problem.runtime.exec(
        node,
        f"cat /sys/class/net/{intf}/statistics/rx_packets "
        f"/sys/class/net/{intf}/statistics/tx_packets 2>/dev/null",
    )
    counts = [int(v) for v in out.split() if v.isdigit()]
    return sum(counts) if len(counts) == 2 else None


def _loop_sample(problem: Any, params: Any) -> dict[str, Any] | None:
    """Flows into the looped leaf, the link's packet count, and a control."""
    model = problem.net_env.model
    pair = (params.host_name, params.host_name_2)
    leaf = next((s for s in pair if s in model.leaves), None)
    spine = next((s for s in pair if s in model.spines), None)
    if leaf is None or spine is None:
        return None
    ports = dict(zip(pair, problem._resolve_ports(params), strict=True))
    leaf_id = model.leaf_id(leaf)
    targets = sorted(model.endpoints_on_leaf(leaf_id), key=lambda e: e.role != "web")
    others = [e for e in model.endpoints if e.leaf_id != leaf_id]
    source = next((e for e in others if e.role == "client"), None)
    control = next(
        (
            e
            for e in others
            if source is not None and e.role == "web" and e.leaf_id != source.leaf_id
        ),
        None,
    )
    if not targets or source is None or control is None:
        return None
    before = _link_packets(problem, spine, ports[spine])
    path = _flows(problem, source, targets[0])
    after = _link_packets(problem, spine, ports[spine])
    link_packets = after - before if before is not None and after is not None else None
    return {
        "path": path,
        "control": _flows(problem, source, control),
        "link": f"{spine}:{ports[spine]}-{leaf}:{ports[leaf]}",
        "link_packets": link_packets,
        "link_packets_per_flow": (
            None if link_packets is None else link_packets / path["sent"]
        ),
        "min_loss_percent": _min_loss_percent(model, spine),
    }


def flow_loop_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    sample = _loop_sample(problem, params)
    if sample is None:
        return False, {"error": "no_leaf_spine_loop_path"}
    problem._audit_loop_baseline = sample
    healthy = (
        sample["path"]["loss_percent"] <= _HEALTHY_LOSS_PERCENT
        and sample["control"]["loss_percent"] <= _HEALTHY_LOSS_PERCENT
        and sample["link_packets_per_flow"] is not None
    )
    return healthy, sample


def flow_loop(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Flows hashed onto the looped link are lost and multiply on that link."""
    sample = _loop_sample(problem, params)
    if sample is None:
        return False, {"error": "no_leaf_spine_loop_path"}
    base = getattr(problem, "_audit_loop_baseline", None) or {}
    base_rate = base.get("link_packets_per_flow")
    rate = sample["link_packets_per_flow"]
    amplified = (
        rate is not None
        and rate >= _LOOP_PACKETS_PER_FLOW
        and (base_rate is None or rate >= _LOOP_PACKETS_PER_FLOW * base_rate)
    )
    control_ok = sample["control"]["loss_percent"] <= _HEALTHY_LOSS_PERCENT
    verified = (
        sample["path"]["loss_percent"] >= sample["min_loss_percent"]
        and amplified
        and control_ok
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            **sample,
            "baseline_link_packets_per_flow": base_rate,
            "amplified": amplified,
            "control_ok": control_ok,
        },
    )


# A documentation address (RFC 5737) that no lab traffic uses.
_PROGRAM_PROBE_DST = "192.0.2.250"
_PROGRAM_WAIT_SEC = 20.0
_DISCONNECT_WAIT_SEC = 90.0


def _switch_connections(problem: Any) -> tuple[dict[str, bool], dict[str, str]]:
    """Each switch's own view of its controller channel.

    Empty or unparsable output is a query error, not a disconnect.
    """
    states: dict[str, bool] = {}
    errors: dict[str, str] = {}
    for switch, item in _switch_controller_state(
        problem.runtime, problem.net_env
    ).items():
        value = item["is_connected"].strip('"').lower()
        if value in {"true", "false"}:
            states[switch] = value == "true"
        else:
            errors[switch] = item["target"] or "empty controller observation"
    return states, errors


def _program_flow(problem: Any) -> dict[str, Any]:
    """Request a harmless flow through the controller and look for it on a leaf.

    The probe rule matches only a documentation address and is removed again
    through the controller, so it never carries lab traffic.
    """
    runtime = problem.runtime
    switch = problem.net_env.model.leaves[0]
    device = _device_id_for_switch(switch)
    _onos_post_flow(
        runtime,
        device,
        {
            "priority": 1,
            "timeout": 0,
            "isPermanent": True,
            "deviceId": device,
            "treatment": {"instructions": []},
            "selector": {
                "criteria": [
                    {"type": "ETH_TYPE", "ethType": "0x0800"},
                    {"type": "IPV4_DST", "ip": f"{_PROGRAM_PROBE_DST}/32"},
                ]
            },
        },
    )
    started = time.monotonic()
    installed = False
    while time.monotonic() - started < _PROGRAM_WAIT_SEC:
        flows = runtime.exec(
            switch, f"ovs-ofctl -O OpenFlow13 dump-flows {switch} 2>/dev/null"
        )
        if f"nw_dst={_PROGRAM_PROBE_DST}" in flows:
            installed = True
            break
        time.sleep(1.0)
    waited = round(time.monotonic() - started, 1)
    for flow in onos_flow_snapshot(runtime).get("flows", []):
        criteria = (flow.get("selector") or {}).get("criteria") or []
        if any(_PROGRAM_PROBE_DST in str(c.get("ip", "")) for c in criteria):
            runtime.exec(
                "fabric_mgr",
                "curl -s -u onos:rocks -X DELETE "
                f"'http://{ONOS_OOB_IP}:{ONOS_REST_PORT}/onos/v1/flows/"
                f"{flow['deviceId']}/{flow['id']}' >/dev/null || true",
                timeout=30,
            )
    return {"switch": switch, "installed": installed, "waited_sec": waited}


def _forwarding(problem: Any) -> dict[str, Any]:
    """Cross-rack ping over the flows that are already installed."""
    model = problem.net_env.model
    source = model.client_endpoints()[0]
    target = next(e for e in model.web_endpoints() if e.leaf_id != source.leaf_id)
    ping = ping_stats(problem.runtime, source.name, target.ip, count=10)
    return {"path": f"{source.name}->{target.name}", "loss_percent": ping.loss_percent}


def controller_serving(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    states, errors = _switch_connections(problem)
    program = _program_flow(problem)
    forwarding = _forwarding(problem)
    ok = bool(
        states
        and not errors
        and all(states.values())
        and program["installed"]
        and forwarding["loss_percent"] == 0.0
    )
    return ok, {
        "switch_connected": states,
        "query_errors": errors,
        "program": program,
        "forwarding": forwarding,
    }


def controller_lost(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Switches lose their controller and new flows stop reaching them."""
    states: dict[str, bool] = {}
    deadline = time.monotonic() + _DISCONNECT_WAIT_SEC
    while True:
        states, errors = _switch_connections(problem)
        if errors:
            return False, {"error": "switch_controller_query_failed", "details": errors}
        if (states and not any(states.values())) or time.monotonic() > deadline:
            break
        time.sleep(2.0)
    disconnected = sorted(name for name, up in states.items() if not up)
    program = _program_flow(problem)
    forwarding = _forwarding(problem)
    control_ok = forwarding["loss_percent"] == 0.0
    verified = bool(
        states
        and len(disconnected) == len(states)
        and not program["installed"]
        and control_ok
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={
            "switch_connected": states,
            "disconnected": disconnected,
            "program": program,
            "forwarding": forwarding,
            "control_ok": control_ok,
        },
    )
