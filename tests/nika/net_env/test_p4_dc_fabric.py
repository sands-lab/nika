"""Unit tests for p4_dc_fabric topology model and forwarding intent."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nika.net_env.p4_dc_fabric.fabric_manager.apply import (
    _COMPILE_OK,
    compile_pipeline_cmd,
    compile_pipeline_on_switch,
)
from nika.net_env.p4_dc_fabric.fabric_manager.intent import (
    build_forwarding_intent,
)
from nika.net_env.p4_dc_fabric.topology_model import (
    SIZE_TABLE,
    SWITCH_IMAGE,
    VIRTUAL_ROUTER_MAC,
    build_clos_fabric_model,
)
from nika.net_env.net_env_pool import list_all_net_envs
from nika.problems.forwarding_encapsulation_policy.p4_runtime import _BLACKHOLE_P4
from nika.mcp.registry import select_diagnosis_servers
from tests.support.prerequisites import docker_image_available


def test_size_table_matches_plan() -> None:
    assert SIZE_TABLE["s"] == (2, 4, 2)
    assert SIZE_TABLE["m"] == (4, 8, 4)
    assert SIZE_TABLE["l"] == (8, 16, 4)


def test_model_scales_without_hardcoding() -> None:
    for size in ("s", "m", "l"):
        model = build_clos_fabric_model(size)
        spines, leaves, ep = SIZE_TABLE[size]
        assert model.spine_count == spines
        assert model.leaf_count == leaves
        assert len(model.endpoints) == leaves * ep
        assert len(model.web_endpoints()) == leaves
        assert model.ecmp_fanout == spines
        assert model.expected_leaf_spine_link_count() == spines * leaves
        assert len(model.expected_device_ids()) == spines + leaves
        leaf = model.leaves[0]
        assert all(
            p.bmv2_port == int(p.name.replace("eth", "")) + 1 for p in model.ports[leaf]
        )
        assert model.switch_info[leaf].oob_ip.startswith("172.31.0.")


def test_intent_ecmp_groups_scale() -> None:
    for size in ("s", "m", "l"):
        model = build_clos_fabric_model(size)
        intent = build_forwarding_intent(model)
        leaf = intent["switches"]["leaf_1"]
        remote = [g for g in leaf["groups"] if g["kind"] == "ecmp"]
        assert len(remote) == model.leaf_count - 1
        for group in remote:
            assert group["member_ids"]
            assert len(group["member_ids"]) == model.spine_count
        prefixes = {e["prefix"] for e in leaf["ipv4_lpm"]}
        assert f"{model.endpoints_on_leaf(1)[0].ip}/32" in prefixes
        assert "10.0.2.0/24" in prefixes
        spine = intent["switches"]["spine_1"]
        assert len(spine["ipv4_lpm"]) == model.leaf_count


def test_intent_local_uses_virtual_router_mac() -> None:
    intent = build_forwarding_intent(build_clos_fabric_model("s"))
    local = [m for m in intent["switches"]["leaf_1"]["members"] if m["role"] == "host"]
    assert local
    assert all(m["src_mac"] == VIRTUAL_ROUTER_MAC for m in local)


def test_scenario_registered() -> None:
    specs = list_all_net_envs()
    assert "p4_dc_fabric" in specs
    spec = specs["p4_dc_fabric"]
    assert spec.topo_size == ["s", "m", "l"]
    assert "p4_runtime" in spec.tags
    assert "http" in spec.tags
    model = build_clos_fabric_model("s")
    assert model.web_urls
    assert "10.0.1." in model.web_urls[0]


def test_p4rt_exec_is_live_only() -> None:
    from unittest.mock import patch

    from nika.service.kathara.bmv2_api import KatharaBMv2API

    api = KatharaBMv2API.__new__(KatharaBMv2API)

    def _exec(host: str, command: str, timeout: float = 15) -> str:
        if "read --switch" in command:
            return '{"ok": true, "switches": {"leaf_1": {"pipeline": {"ok": true}, "ipv4_lpm": []}}}'
        return "1: lo: <LOOPBACK>"

    with patch.object(KatharaBMv2API, "exec_cmd", side_effect=_exec):
        state = json.loads(api.p4rt_exec("read --switch leaf_1"))

    assert "switches" in state
    assert state["switches"]["leaf_1"]["pipeline"] == {"ok": True}
    blob = json.dumps(state)
    assert "intended" not in blob
    assert "expected" not in blob
    assert "p4_table_entry_missing" not in blob
    assert "fault_type" not in blob
    servers = select_diagnosis_servers("p4_dc_fabric", backend="kathara")
    assert "kathara_bmv2_mcp_server" in servers
    assert "kathara_sdn_mcp_server" not in servers


def test_compile_pipeline_on_switch_requires_ok_marker(monkeypatch) -> None:
    from nika.net_env.p4_dc_fabric.fabric_manager import apply as apply_mod

    monkeypatch.setattr(
        apply_mod,
        "_exec",
        lambda runtime, host, cmd, timeout=60.0: (
            "cp: '/tmp/blackhole.p4' and '/tmp/blackhole.p4' are the same file"
        ),
    )
    with pytest.raises(RuntimeError, match="p4c failed"):
        compile_pipeline_on_switch(
            object(),
            "leaf_1",
            "/tmp/blackhole.p4",
            "blackhole.p4info.txt",
            "blackhole.json",
        )


def test_compile_pipeline_cmd_skips_same_path_copy() -> None:
    cmd = compile_pipeline_cmd(
        "/tmp/blackhole.p4", "blackhole.p4info.txt", "blackhole.json"
    )
    assert "cp /tmp/blackhole.p4 /tmp/blackhole.p4" not in cmd
    assert cmd.startswith("(true &&")
    assert _COMPILE_OK in cmd


def test_compile_pipeline_cmd_copies_relative_source() -> None:
    cmd = compile_pipeline_cmd("fabric.p4", "fabric.p4info.txt", "fabric.json")
    assert "cp fabric.p4 /tmp/fabric.p4" in cmd
    assert _COMPILE_OK in cmd


@pytest.mark.skipif(
    not docker_image_available(SWITCH_IMAGE),
    reason=f"{SWITCH_IMAGE} image not available",
)
def test_blackhole_p4_compiles_with_inject_command() -> None:
    cmd = compile_pipeline_cmd(
        "/tmp/blackhole.p4", "blackhole.p4info.txt", "blackhole.json"
    )
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{Path(_BLACKHOLE_P4).resolve()}:/tmp/blackhole.p4:ro",
            SWITCH_IMAGE,
            "bash",
            "-lc",
            f"{cmd} && test -s /tmp/blackhole.json && test -s /tmp/blackhole.p4info.txt",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _COMPILE_OK in result.stdout
