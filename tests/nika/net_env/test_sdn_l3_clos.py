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
from nika.net_env.sdn_l3_clos.verify import verify_sdn_l3_clos_lab_startup
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


def test_is_transient_onos_failure_matches_connection_refused() -> None:
    assert fabric_apply._is_transient_onos_failure(
        {
            "path": "/onos/v1/groups/of:0000000000001001",
            "error": "<urlopen error [Errno 111] Connection refused>",
            "status": None,
        }
    )
    assert fabric_apply._is_transient_onos_failure({"status": 503, "error": "busy"})
    assert not fabric_apply._is_transient_onos_failure(
        {"status": 400, "error": "bad request"}
    )


def test_onos_batch_resilient_retries_connection_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []

    def fake_batch(_runtime, ops, **_kwargs):
        calls.append(len(ops))
        if len(calls) == 1:
            return __import__("json").dumps(
                [
                    {
                        "path": ops[0][1],
                        "error": "<urlopen error [Errno 111] Connection refused>",
                        "status": None,
                    }
                    for _ in ops
                ]
            )
        return __import__("json").dumps([{"path": op[1], "status": 200} for op in ops])

    monkeypatch.setattr(fabric_apply, "_onos_batch", fake_batch)
    monkeypatch.setattr(fabric_apply, "wait_for_onos", lambda *_a, **_k: True)
    monkeypatch.setattr(fabric_apply.time, "sleep", lambda *_a, **_k: None)

    ops = [
        ("POST", "/onos/v1/groups/of:0000000000001001", {"type": "SELECT"}),
        ("POST", "/onos/v1/groups/of:0000000000001002", {"type": "SELECT"}),
    ]
    result = fabric_apply._onos_batch_resilient(
        object(), ops, operation="group install"
    )
    assert calls == [2, 2]
    assert all(item["status"] == 200 for item in __import__("json").loads(result))


def test_onos_batch_resilient_treats_delete_404_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_batch(_runtime, ops, **_kwargs):
        return __import__("json").dumps(
            [{"path": op[1], "status": 404, "error": "not found"} for op in ops]
        )

    monkeypatch.setattr(fabric_apply, "_onos_batch", fake_batch)
    ops = [("DELETE", "/onos/v1/groups/of:1/cookie", None)]
    result = fabric_apply._onos_batch_resilient(object(), ops, operation="fabric clear")
    assert __import__("json").loads(result)[0]["status"] == 404


def test_onos_batch_resilient_does_not_retry_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def fake_batch(_runtime, ops, **_kwargs):
        calls["n"] += 1
        return "not-json"

    monkeypatch.setattr(fabric_apply, "_onos_batch", fake_batch)
    with pytest.raises(RuntimeError, match="invalid JSON"):
        fabric_apply._onos_batch_resilient(
            object(),
            [("POST", "/onos/v1/groups/of:1", {})],
            operation="group install",
        )
    assert calls["n"] == 1


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


def test_local_arp_flood_actions_translate_at_all_sizes() -> None:
    """Multi-host racks must keep local ARP flood rules installable."""
    for size in ("s", "m", "l"):
        model = build_clos_fabric_model(size)
        arp = [
            f for f in build_forwarding_rules(model)["flows"] if f["priority"] == 42000
        ]
        assert arp
        for flow in arp:
            ports = {
                p.name: str(i)
                for i, p in enumerate(model.ports[flow["switch"]], start=1)
            }
            body = fabric_apply._install_onos_flow_body(flow, ports)
            assert body is not None, (size, flow["match"], flow["actions"])


def test_apply_forwarding_fails_on_group_build_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_build(*_args):
        raise KeyError("missing port")

    model = build_clos_fabric_model("s")
    monkeypatch.setattr(
        fabric_apply,
        "build_forwarding_rules",
        lambda _model: {
            "groups": [{"switch": "leaf_1", "device_id": "of:1"}],
            "flows": [],
        },
    )
    monkeypatch.setattr(fabric_apply, "_set_dpid", lambda *_args: None)
    monkeypatch.setattr(fabric_apply, "_ofport_map", lambda *_args: {})
    monkeypatch.setattr(fabric_apply, "_clear_rest_flows_groups", lambda *_args: None)
    monkeypatch.setattr(
        fabric_apply,
        "_install_onos_group_body",
        fail_build,
    )

    with pytest.raises(RuntimeError, match="ONOS group build failed on leaf_1"):
        apply_forwarding(object(), model)  # type: ignore[arg-type]


def test_apply_forwarding_fails_on_flow_build_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_build(*_args):
        raise KeyError("missing port")

    model = build_clos_fabric_model("s")
    monkeypatch.setattr(
        fabric_apply,
        "build_forwarding_rules",
        lambda _model: {
            "groups": [],
            "flows": [{"switch": "leaf_1", "device_id": "of:1"}],
        },
    )
    monkeypatch.setattr(fabric_apply, "_set_dpid", lambda *_args: None)
    monkeypatch.setattr(fabric_apply, "_ofport_map", lambda *_args: {})
    monkeypatch.setattr(fabric_apply, "_clear_rest_flows_groups", lambda *_args: None)
    monkeypatch.setattr(fabric_apply, "onos_group_snapshot", lambda *_args: {})
    monkeypatch.setattr(
        fabric_apply,
        "_install_onos_flow_body",
        fail_build,
    )

    with pytest.raises(RuntimeError, match="ONOS flow build failed on leaf_1"):
        apply_forwarding(object(), model)  # type: ignore[arg-type]


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class SDNL3ClosTopologyChangeTest(IntegrationTestCase):
    """SDN Clos dataplane recovery and startup validation regressions."""

    def test_leaf_spine_link_down_recovers(self) -> None:
        session_id = self._start_env("sdn_l3_clos", ["-s", "s"])
        try:
            row = self._assert_session_ready(session_id, "sdn_l3_clos")
            runtime = runtime_for_session(row)
            model = build_clos_fabric_model("s")
            src = model.client_endpoints()[0]
            same = next(w for w in model.web_endpoints() if w.leaf_id == src.leaf_id)
            dst = next(w for w in model.web_endpoints() if w.leaf_id != src.leaf_id)

            assert ping_ok(runtime, src.name, same.ip)
            assert http_ok(runtime, src.name, f"http://{same.ip}/")
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
            assert ping_ok(runtime, src.name, same.ip)
            assert http_ok(runtime, src.name, f"http://{same.ip}/")
            assert ping_ok(runtime, src.name, dst.ip)
            assert http_ok(runtime, src.name, f"http://{dst.ip}/")
        finally:
            self._close_session(session_id)

    def test_startup_rejects_broken_same_rack_arp(self) -> None:
        session_id = self._start_env("sdn_l3_clos", ["-s", "s"])
        try:
            row = self._assert_session_ready(session_id, "sdn_l3_clos")
            runtime = runtime_for_session(row)
            model = build_clos_fabric_model("s")
            src = model.client_endpoints()[0]
            same = next(w for w in model.web_endpoints() if w.leaf_id == src.leaf_id)
            leaf = model.leaves[src.leaf_id - 1]
            source_port = model.port_to_peer(leaf, src.name)
            assert source_port is not None

            assert ping_ok(runtime, src.name, same.ip)
            assert http_ok(runtime, src.name, f"http://{same.ip}/")

            runtime.exec(
                leaf,
                "ovs-ofctl -O OpenFlow13 add-flow "
                f"{leaf} 'table=0,priority=65000,arp,"
                f"in_port={source_port.name},actions=drop'",
            )
            runtime.exec(src.name, f"ip neigh del {same.ip} dev eth0 || true")

            flows = runtime.exec(leaf, f"ovs-ofctl -O OpenFlow13 dump-flows {leaf}")
            assert "priority=65000" in flows

            result = verify_sdn_l3_clos_lab_startup(
                runtime,
                scenario_name="sdn_l3_clos",
                model=model,
            )
            assert not result["verified"]
            assert not result["checks"]["same_rack_ping"]
            assert result["checks"]["cross_rack_ping"]
        finally:
            self._close_session(session_id)
