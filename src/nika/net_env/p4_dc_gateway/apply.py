"""Deploy the gateway pipeline through the shared P4Runtime transport."""

from __future__ import annotations

import json

from nika.net_env.p4_dc_fabric.fabric_manager.apply import (
    FABRIC_DIR,
    INTENT_PATH,
    JSON_PATH,
    P4INFO_PATH,
    _copy_in,
    _copy_out,
    _exec,
    _wait_for_grpc,
    compile_pipeline_on_switch,
    run_manager,
)
from nika.net_env.p4_dc_gateway.control import build_gateway_intent
from nika.net_env.p4_dc_gateway.topology_model import GatewayFabricModel
from nika.runtime.base import LabRuntime


def reconcile_gateway(runtime: LabRuntime, model: GatewayFabricModel) -> dict:
    """Compile once, configure every switch, and persist the observed state."""
    _wait_for_grpc(runtime, model, 180)
    _exec(runtime, "fabric_mgr", f"mkdir -p {FABRIC_DIR}", timeout=10)
    compiler = model.leaves[0]
    compile_pipeline_on_switch(
        runtime, compiler, "gateway.p4", "fabric.p4info.txt", "gateway.json"
    )
    for source, destination in (
        ("/tmp/gateway.json", JSON_PATH),
        ("/tmp/fabric.p4info.txt", P4INFO_PATH),
    ):
        _copy_in(
            runtime, "fabric_mgr", destination, _copy_out(runtime, compiler, source)
        )
    intent = build_gateway_intent(model)
    _copy_in(runtime, "fabric_mgr", INTENT_PATH, json.dumps(intent, indent=2).encode())
    applied = run_manager(runtime, "apply", timeout=240)
    if not applied.get("ok"):
        raise RuntimeError(f"P4Runtime gateway apply failed: {applied}")
    observed = run_manager(runtime, "read", timeout=180)
    _copy_in(
        runtime,
        "fabric_mgr",
        f"{FABRIC_DIR}/observed.json",
        json.dumps(observed, indent=2).encode(),
    )
    return {"intent": intent, "apply": applied, "observed": observed}
