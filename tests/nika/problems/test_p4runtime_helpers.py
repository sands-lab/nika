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


def test_oracle_compares_observed_forwarding_rows_without_mismatch_labels():
    import json
    from copy import deepcopy

    from nika.problems.forwarding_encapsulation_policy.p4_runtime import mismatch

    state = {"groups": [{"group_id": 1, "member_ids": [1, 2]}]}
    check = mismatch("leaf_1", "groups", {"switches": {"leaf_1": state}})
    assert not check(json.dumps({"switches": {"leaf_1": state}}))
    changed = deepcopy(state)
    changed["groups"][0]["member_ids"] = [1]
    assert check(json.dumps({"switches": {"leaf_1": changed}}))
    assert not check("tool_error: read failed")


@pytest.mark.parametrize(
    "failure",
    [None, "source", "compile", "read-json", "copy-json", "read-info", "copy-info"],
)
def test_pipeline_staging_cleanup_on_partial_compile_or_copy(
    monkeypatch, tmp_path, failure
):
    import shlex

    from nika.net_env.p4_dc_fabric.fabric_manager import apply
    from nika.problems.forwarding_encapsulation_policy.p4runtime_helpers import (
        load_blackhole_pipeline,
        remove_staged_pipeline,
    )

    files = {}
    source = tmp_path / "program.p4"
    source.write_text("program")

    def fail(stage):
        if failure == stage:
            raise RuntimeError(stage)

    def copy_in(runtime, node, path, content):
        files[node, path] = content
        fail(
            "source"
            if node == "leaf_1"
            else "copy-json"
            if path.endswith(".json")
            else "copy-info"
        )

    def compile_pipeline(runtime, node, path, info_name, json_name):
        for name in (info_name, json_name, "pipeline_candidate.p4i"):
            files[node, f"/tmp/{name}"] = b"compiled"
        fail("compile")

    def copy_out(runtime, node, path):
        fail("read-json" if path.endswith(".json") else "read-info")
        return files[node, path]

    class Runtime:
        def exec(self, node, command):
            for path in shlex.split(command)[2:]:
                files.pop((node, path), None)

    runtime = Runtime()
    monkeypatch.setattr(apply, "_copy_in", copy_in)
    monkeypatch.setattr(apply, "_copy_out", copy_out)
    monkeypatch.setattr(apply, "compile_pipeline_on_switch", compile_pipeline)
    if failure:
        with pytest.raises(RuntimeError, match=failure):
            load_blackhole_pipeline(runtime, "leaf_1", source)
    else:
        info, pipeline = load_blackhole_pipeline(runtime, "leaf_1", source)
        assert set(files) == {("fabric_mgr", info), ("fabric_mgr", pipeline)}
        remove_staged_pipeline(runtime, info, pipeline)
    assert files == {}
