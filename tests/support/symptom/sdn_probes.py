"""SDN flow-rule symptom probes (test-path only).

A shadowing drop rule on one spine only hits the ECMP share hashed onto it, so
the probe sends many cross-rack UDP flows with distinct source ports. Same-rack
traffic on a leaf other than the target never crosses a spine and is the
control path.
"""

from __future__ import annotations

import re
import shlex
import time
from typing import Any

from nika.problems.base import build_verify_result

_PORT = 19611
_FLOWS = 256
_LOG = "/tmp/nika-audit-shadow.log"


def _ends(model: Any, switch: str) -> dict[str, Any]:
    leaf = model.leaf_id(switch) if switch in model.leaves else None
    clients = model.client_endpoints()
    source = next((e for e in clients if leaf in (None, e.leaf_id)), None)
    if source is None:
        return {}
    target = next(
        (e for e in model.web_endpoints() if e.leaf_id != source.leaf_id), None
    )
    control = next(
        (
            (a, b)
            for a in model.endpoints
            for b in model.endpoints
            if a.leaf_id == b.leaf_id and a.name < b.name and a.leaf_id != leaf
        ),
        None,
    )
    if target is None or control is None:
        return {}
    return {"source": source, "target": target, "control": control}


def _drop_packets(problem: Any, switch: str) -> int | None:
    flows = problem.runtime.exec(
        switch, f"ovs-ofctl -O OpenFlow13 dump-flows {switch} 2>/dev/null"
    )
    for line in flows.splitlines():
        if f"priority={problem._SHADOW_PRIORITY} " in line and "drop" in line:
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
    received = int(got.strip().splitlines()[-1] or 0)
    return {
        "source": source.name,
        "target": target.name,
        "sent": _FLOWS,
        "received": received,
        "loss_percent": 100 * (_FLOWS - received) / _FLOWS,
    }


def _sample(problem: Any, params: Any) -> dict[str, Any] | None:
    ends = _ends(problem.net_env.model, params.host_name)
    if not ends:
        return None
    before = _drop_packets(problem, params.host_name)
    path = _flows(problem, ends["source"], ends["target"])
    after = _drop_packets(problem, params.host_name)
    control = _flows(problem, *ends["control"])
    return {
        "path": path,
        "control": control,
        "drop_packets_before": before,
        "drop_packets_after": after,
    }


def flow_shadow_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    sample = _sample(problem, params)
    if sample is None:
        return False, {"error": "no_cross_rack_probe_path"}
    healthy = (
        sample["path"]["received"] == _FLOWS and sample["control"]["received"] == _FLOWS
    )
    return healthy, sample


def flow_shadow(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Cross-rack flows hashed onto the shadowed switch are dropped by the rule."""
    sample = _sample(problem, params)
    if sample is None:
        return False, {"error": "no_cross_rack_probe_path"}
    before, after = sample["drop_packets_before"], sample["drop_packets_after"]
    counter_rise = before is not None and after is not None and after > before
    control_ok = sample["control"]["received"] == _FLOWS
    verified = counter_rise and sample["path"]["loss_percent"] > 0 and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={**sample, "control_ok": control_ok},
    )
