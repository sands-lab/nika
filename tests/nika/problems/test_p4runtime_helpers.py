"""Unit tests for shared P4Runtime forwarding helpers."""

from __future__ import annotations

from nika.net_env.p4_dc_fabric.fabric_manager.intent import build_forwarding_intent
from nika.net_env.p4_dc_fabric.topology_model import build_clos_fabric_model
from nika.problems.forwarding_encapsulation_policy.p4runtime_helpers import (
    fabric_misconfig_group_id,
    fabric_table_entry_prefix,
    lpm_prefix_for_dst,
    probe_victim_ip,
    wrong_local_group_id,
)


def test_probe_victim_ip_matches_default_probe_path() -> None:
    model = build_clos_fabric_model("s")
    intent = build_forwarding_intent(model)
    observer = model.client_endpoints()[0]
    victim = next(w for w in model.web_endpoints() if w.leaf_id != observer.leaf_id)
    assert probe_victim_ip(intent) == victim.ip


def test_fabric_table_entry_prefix_targets_victim_rack_on_ingress_leaf() -> None:
    model = build_clos_fabric_model("s")
    intent = build_forwarding_intent(model)
    victim_ip = probe_victim_ip(intent)
    prefix = fabric_table_entry_prefix(intent, "leaf_1", "p4_dc_fabric")
    assert prefix == lpm_prefix_for_dst(intent, "leaf_1", victim_ip)
    assert prefix == "10.0.2.0/24"


def test_wrong_local_group_id_prefers_local_host_group() -> None:
    intent = build_forwarding_intent(build_clos_fabric_model("s"))
    prefix = fabric_table_entry_prefix(intent, "leaf_1", "p4_dc_fabric")
    assert prefix is not None
    wrong = wrong_local_group_id(intent, "leaf_1", prefix)
    assert wrong == 1

    misconfig = fabric_misconfig_group_id(intent, "leaf_1", prefix, "p4_dc_fabric")
    assert misconfig == wrong
