"""Deploy the gateway pipeline through the shared P4Runtime transport."""

from __future__ import annotations

import json
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
    _copy_in(runtime, "fabric_mgr", INTENT_PATH, json.dumps(intent, indent=2).encode())
    applied = run_manager(runtime, "apply", timeout=240)
    if not applied.get("ok"):
        raise RuntimeError(f"P4Runtime gateway apply failed: {applied}")
    observed = run_manager(runtime, "read", timeout=180)
    return {"intent": intent, "apply": applied, "observed": observed}
