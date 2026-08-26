"""Unit and integration tests for sdn_l3_clos topology model and forwarding."""

from __future__ import annotations

import time

import pytest

from nika.net_env.sdn_l3_clos.fabric_manager import apply as fabric_apply
from nika.net_env.sdn_l3_clos.fabric_manager.apply import (
    apply_forwarding,
    prune_groups_for_down_link,
)
from nika.net_env.sdn_l3_clos.fabric_manager.forwarding_rules import (
    build_forwarding_rules,
)
from nika.net_env.sdn_l3_clos.topology_model import (
    SIZE_TABLE,
    build_clos_fabric_model,
    device_id,
    dpid_for_leaf,
    dpid_for_spine,
)
from nika.net_env.verify import http_ok, ping_ok
from nika.runtime.factory import runtime_for_session
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available


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


def test_forwarding_rules_ecmp_groups() -> None:
    model = build_clos_fabric_model("s")
    rules = build_forwarding_rules(model)
    assert rules["ecmp_fanout"] == 2
    leaf_groups = [g for g in rules["groups"] if g["switch"] == "leaf_1"]
    assert len(leaf_groups) == model.leaf_count - 1
    for group in leaf_groups:
        assert group["type"] == "select"
        assert len(group["buckets"]) == 2
        assert group["device_id"] == device_id(dpid_for_leaf(1))


def test_forwarding_rules_spine_prefixes() -> None:
    model = build_clos_fabric_model("s")
    rules = build_forwarding_rules(model)
    spine_flows = [f for f in rules["flows"] if f["switch"] == "spine_1"]
    assert len(spine_flows) == model.leaf_count
    assert all(f["device_id"] == device_id(dpid_for_spine(1)) for f in spine_flows)


def test_onos_batch_splits_large_command_payload() -> None:
    class Runtime:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def exec(self, _node: str, command: str, **_kwargs) -> str:
            self.commands.append(command)
            return '[{"status": 200}]'

    runtime = Runtime()
    ops = [("POST", "/onos/v1/groups/of:1", {"payload": "x" * 1000})] * 100
    result = fabric_apply._onos_batch(runtime, ops)

    assert len(runtime.commands) > 1
    assert all(len(command) < 70_000 for command in runtime.commands)
    assert len(__import__("json").loads(result)) == len(runtime.commands)


def test_prune_groups_removes_only_failed_link_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = build_clos_fabric_model("s")
    batches: list[list[tuple[str, str, dict | None]]] = []
    waits: list[set[tuple[str, str, str]]] = []

    monkeypatch.setattr(
        fabric_apply,
        "_ofport_map",
        lambda _runtime, switch, _model: {
            port.name: str(index)
            for index, port in enumerate(model.ports[switch], start=1)
        },
    )
    monkeypatch.setattr(
        fabric_apply,
        "_bucket_ids_for_output_port",
        lambda _runtime, _device_id, _cookie, output_port: [f"bucket-{output_port}"],
    )

    def record_batch(_runtime, ops):
        batches.append(ops)
        return "[" + ",".join('{"status": 200}' for _ in ops) + "]"

    def record_wait(_runtime, expected, *, timeout_sec=30.0):
        waits.append(expected)
        return True

    monkeypatch.setattr(fabric_apply, "_onos_batch", record_batch)
    monkeypatch.setattr(fabric_apply, "_wait_for_group_bucket_removal", record_wait)

    prune_groups_for_down_link(
        object(),
        model,
        leaf="leaf_1",
        spine="spine_1",  # type: ignore[arg-type]
    )

    assert [[method for method, _path, _body in batch] for batch in batches] == [
        ["DELETE"] * 6
    ]
    assert all("/buckets/bucket-" in path for _method, path, _body in batches[0])
    assert len(waits) == 1
    assert len(waits[0]) == 6


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class SDNL3ClosTopologyChangeTest(IntegrationTestCase):
    """Topology-change recovery (not a benchmark failure)."""

    def test_leaf_spine_link_down_recovers(self) -> None:
        session_id = self._start_env("sdn_l3_clos", ["-s", "s"])
        try:
            row = self._assert_session_ready(session_id, "sdn_l3_clos")
            runtime = runtime_for_session(row)
            model = build_clos_fabric_model("s")
            src = model.client_endpoints()[0]
            dst = next(w for w in model.web_endpoints() if w.leaf_id != src.leaf_id)

            assert ping_ok(runtime, src.name, dst.ip)
            assert http_ok(runtime, src.name, f"http://{dst.ip}/")

            leaf, spine = "leaf_1", "spine_1"
            leaf_port = model.port_to_peer(leaf, spine)
            assert leaf_port is not None

            runtime.set_interface_state(leaf, leaf_port.name, "down")
            prune_groups_for_down_link(runtime, model, leaf=leaf, spine=spine)
            time.sleep(5)
            assert ping_ok(runtime, src.name, dst.ip)

            runtime.set_interface_state(leaf, leaf_port.name, "up")
            apply_forwarding(runtime, model)
            time.sleep(5)
            assert ping_ok(runtime, src.name, dst.ip)
            assert http_ok(runtime, src.name, f"http://{dst.ip}/")
        finally:
            self._close_session(session_id)
