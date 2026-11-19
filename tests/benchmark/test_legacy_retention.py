"""0.2.0 retention scoring and lexicographic priority."""

import pytest

from nika.workflows.benchmark import legacy_retention as legacy
from nika.workflows.benchmark import split_generator as generator
from nika.workflows.benchmark.network_identity import network_identity

pytest_plugins = ("test_split_generator",)
pytestmark = pytest.mark.unit


def test_fault_mapping():
    assert legacy.canonical_fault("frr_service_down") == "routing_service_down"
    assert legacy.canonical_fault("link_detach") == "link_down"
    assert legacy.canonical_fault("icmp_acl_block") == "host_acl_block"
    assert legacy.canonical_fault("bgp_max_prefix_exceeded") is None
    assert legacy.canonical_fault("link_flap") == "link_flap"


def test_score_and_merged_variant():
    old = {
        "scenario": "dc_clos",
        "topo_size": "s",
        "problem": "icmp_acl_block",
        "inject": {"host_name": "r1"},
        "split": "dev",
    }
    index = {(old["scenario"], network_identity(old), "host_acl_block"): [old]}
    row = {
        **old,
        "problem": "host_acl_block",
        "inject": {"host_name": "r1", "protocol": "icmp", "extra": 1},
    }
    assert legacy.match(row, "dev", index) == (4, "exact", True)
    assert legacy.match(row, "test", index) == (3, "exact", False)
    row["inject"]["protocol"] = "http"
    assert legacy.match(row, "dev", index) == (2, "network_fault", True)
    row["topo_size"] = "m"
    assert legacy.match(row, "dev", index) == (0, None, False)


def test_priority_switch_and_candidate_retention(small_inventory, monkeypatch):
    old = {
        "scenario": "campus_lan",
        "topo_size": "s",
        "problem": "link_down",
        "inject": {"host_name": "r2", "intf_name": "eth0"},
        "split": "test",
    }
    monkeypatch.setattr(
        legacy,
        "legacy_index",
        lambda: {(old["scenario"], network_identity(old), old["problem"]): [old]},
    )
    enabled, on = generator.generate_splits(small_inventory)
    _, off = generator.generate_splits(small_inventory, legacy_retention=False)
    stages_on = {s["stage"]: s["objective"] for s in on["solver"]["stages"]}
    stages_off = {s["stage"]: s["objective"] for s in off["solver"]["stages"]}
    assert list(stages_on)[-2:] == ["legacy_retention", "heavy_cost"]
    assert "legacy_retention" not in stages_off
    for name in (
        "tier_a_capacity",
        "main_tvd_excess",
        "vendor_capacity",
        "third_size_domains",
    ):
        assert stages_on[name] == pytest.approx(stages_off[name], abs=1e-6)
    selected = enabled["dev"] + enabled["test"]
    assert any(
        r["scenario"] == "campus_lan"
        and r["topo_scale"] == "s"
        and r["problem"] == "link_down"
        and r["inject"]["host_name"] == "r2"
        for r in selected
    )
    assert (
        sum(
            on["legacy_retention"][s]["exact"][side]
            for s in ("dev", "test")
            for side in ("same_split", "other_split")
        )
        >= 1
    )
