"""Fabric manager package for p4_dc_fabric."""

from nika.net_env.p4_dc_fabric.fabric_manager.apply import (
    compile_pipeline_on_switch,
    load_intent,
    reconcile_fabric,
    run_manager,
)
from nika.net_env.p4_dc_fabric.fabric_manager.intent import (
    build_forwarding_intent,
    remote_rack_prefix,
)

__all__ = [
    "build_forwarding_intent",
    "compile_pipeline_on_switch",
    "load_intent",
    "reconcile_fabric",
    "remote_rack_prefix",
    "run_manager",
]
