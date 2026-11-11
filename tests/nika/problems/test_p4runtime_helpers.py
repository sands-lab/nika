"""Unit tests for shared P4Runtime forwarding helpers."""

from __future__ import annotations

import pytest

from nika.net_env.p4_dc_fabric.fabric_manager.intent import build_forwarding_intent
from nika.net_env.p4_dc_fabric.topology_model import build_clos_fabric_model
from nika.problems.forwarding_encapsulation_policy.p4runtime_helpers import (
    fabric_misconfig_group_id,
    fabric_table_entry_prefix,
    lpm_prefix_for_dst,
    probe_victim_ip,
    wrong_local_group_id,
)

pytestmark = pytest.mark.unit


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


@pytest.mark.parametrize(
    ("codes", "messages", "accepted"),
    [
        (["RESOURCE_EXHAUSTED"], [], True),
        (["UNKNOWN"], ["Error when adding match entry to target"], True),
        (["ALREADY_EXISTS"], [], False),
        (["INVALID_ARGUMENT"], [], False),
        (["UNAVAILABLE"], [], False),
        (["UNKNOWN"], ["transport error"], False),
        (["OK"], [], False),
    ],
)
def test_table_full_requires_a_device_resource_error(codes, messages, accepted):
    from nika.problems.forwarding_encapsulation_policy.p4runtime_helpers import (
        table_capacity_rejected,
    )

    result = {
        "inserted": False,
        "present_after": False,
        "codes": codes,
        "messages": messages,
    }
    assert table_capacity_rejected(result) is accepted
    assert not table_capacity_rejected({**result, "error": "read failed"})
    assert not table_capacity_rejected({**result, "present_after": True})
    assert not table_capacity_rejected({**result, "inserted": True})


@pytest.mark.parametrize("size", ["s", "m", "l"])
@pytest.mark.parametrize("switch", ["gateway_1", "spine_1"])
def test_tcam_oracle_builds_offline_and_requires_only_the_changed_match(size, switch):
    import ipaddress
    import json
    from nika.agents.oracle.plan import fault_plan
    from nika.net_env.p4_dc_gateway.topology_model import build_gateway_fabric_model
    from nika.net_env.p4_dc_gateway.control import build_gateway_intent
    from nika.problems.forwarding_encapsulation_policy.p4_gateway import (
        P4TcamEntryCorruptionParams,
        _tcam_row,
    )

    model = build_gateway_fabric_model(size)
    params = {
        "host_name": switch,
        "target_ip": model.services[0].ip,
        "control_source": model.clients[1].name,
    }
    plan = fault_plan(
        "p4_tcam_entry_corruption", "p4_dc_gateway", {"topo_size": size}, params
    )
    row = _tcam_row(build_gateway_intent(model), P4TcamEntryCorruptionParams(**params))
    net = ipaddress.IPv4Network(row["prefix"])
    normal = {
        "match": {"dstAddr": [int(net.network_address), int(net.netmask)]},
        "priority": net.prefixlen + 1,
        "group_id": row["group_id"],
    }
    changed = {
        **normal,
        "match": {
            "dstAddr": [
                int(net.network_address),
                int(ipaddress.IPv4Address(row["mask"])),
            ]
        },
    }
    check = plan.playbook.steps[-1].expect

    def output(entries):
        return json.dumps({"tables": {"ipv4_lpm": entries}})

    assert not check(output([normal]))
    assert check(output([changed]))
    assert not check(output([changed, normal]))
    assert not check(output([{**changed, "group_id": row["group_id"] + 999}]))
    assert not check("tool_error: unreadable table")


def test_tcam_mask_rejects_targets_that_cannot_be_isolated_from_peers():
    from nika.problems.support.p4_gateway import ternary_miss_row

    with pytest.raises(ValueError, match="No mask bit isolates"):
        ternary_miss_row("10.0.1.0/24", "10.0.1.3", ["10.0.1.1", "10.0.1.2"])
