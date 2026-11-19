"""Deploy the gateway pipeline through the shared P4Runtime transport."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from nika.net_env.p4_dc_fabric.fabric_manager.apply import (
    FABRIC_DIR,
    INTENT_PATH,
    JSON_PATH,
    P4INFO_PATH,
    _copy_in,
    _exec,
    _wait_for_grpc,
    compile_pipeline,
    run_manager,
)
from nika.net_env.p4_dc_gateway.control import build_gateway_intent
from nika.net_env.p4_dc_gateway.topology_model import GatewayFabricModel
from nika.runtime.base import LabRuntime
from nika.service.lab.checked_exec import checked_exec

# A full queue drains in about 107 ms; the ECN exercise exceeds this rate.
QUEUE_RATE_PPS = 600
QUEUE_DEPTH_PACKETS = 64

PIPELINE_SOURCE = Path(__file__).resolve().with_name("gateway.p4")


def reconcile_gateway(runtime: LabRuntime, model: GatewayFabricModel) -> dict:
    """Compile once, configure every switch, and persist the observed state."""
    _wait_for_grpc(runtime, model, 180)
    _exec(runtime, "fabric_mgr", f"mkdir -p {FABRIC_DIR}", timeout=10)
    p4info, device_json = compile_pipeline(
        runtime, model.leaves[0], PIPELINE_SOURCE, "gateway"
    )
    _copy_in(runtime, "fabric_mgr", P4INFO_PATH, p4info)
    _copy_in(runtime, "fabric_mgr", JSON_PATH, device_json)
    intent = build_gateway_intent(model)
    collector_mac = _exec(
        runtime, "collector", "cat /sys/class/net/eth0/address", timeout=10
    ).strip()
    for switch in intent["switches"].values():
        switch["int"]["node_config"] = {
            "switch_id": switch["device_id"],
            "report_src": switch["telemetry_ip"],
            "collector_mac": collector_mac,
        }
    _copy_in(runtime, "fabric_mgr", INTENT_PATH, json.dumps(intent, indent=2).encode())
    applied = run_manager(runtime, "apply", timeout=240)
    if not applied.get("ok"):
        raise RuntimeError(f"P4Runtime gateway apply failed: {applied}")
    for switch in model.fabric_switches():
        telemetry_port = len(model.ports[switch]) + 1
        # Each queued data packet can produce an ingress and an egress report.
        # Reserve that burst on the telemetry TAP without enlarging data queues.
        report_queue_len = 2 * QUEUE_DEPTH_PACKETS * len(model.ports[switch])
        commands = [f"mirroring_add 500 {telemetry_port}"]
        for port in model.ports[switch]:
            commands += [
                f"set_queue_depth {QUEUE_DEPTH_PACKETS} {port.bmv2_port}",
                f"set_queue_rate {QUEUE_RATE_PPS} {port.bmv2_port}",
            ]
        output = checked_exec(
            runtime.exec,
            switch,
            f"ip link set dev eth{telemetry_port} txqueuelen {report_queue_len} && "
            f"printf %s {shlex.quote(chr(10).join(commands) + chr(10))} | simple_switch_CLI --thrift-port 9090",
            timeout=30,
        )
        if "Error" in output or "Invalid" in output:
            raise RuntimeError(
                f"BMv2 INT queue/report setup failed on {switch}: {output}"
            )
    observed = run_manager(runtime, "read", timeout=180)
    return {"intent": intent, "apply": applied, "observed": observed}
