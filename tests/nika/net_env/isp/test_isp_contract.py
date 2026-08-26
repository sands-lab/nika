from __future__ import annotations

from nika.net_env.isp.bgp import compile_bgp_plan
from nika.net_env.isp.contract import build_isp_validation_contract
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.net_env.isp.traffic.models import TrafficInterval, TrafficMatrixSeries
from nika.net_env.isp.traffic.stubs import attach_traffic_stubs
from nika.net_env.isp.kathara.verify import verify_isp_contract
from nika.topology.models import NetworkTopology, TopoLink, TopoNode


def _plan():
    topology = NetworkTopology(
        name="intent-test",
        source_format="sndlib-xml",
        meta={},
        nodes=tuple(TopoNode(id=name) for name in ("D", "C", "B", "A")),
        links=(
            TopoLink(id="L1", source="A", target="B"),
            TopoLink(id="L2", source="B", target="D"),
            TopoLink(id="L3", source="B", target="C"),
        ),
        demands=(),
    )
    return compile_isp_plan(IspConfig(topology="polska", igp="ospf"), topology=topology)


def _traffic(plan):
    series = TrafficMatrixSeries(
        topology=plan.topology_name,
        source="demands",
        intervals=(TrafficInterval(index=0, duration_sec=5, flows=()),),
    )
    return attach_traffic_stubs(
        plan, series, pop_node_ids=tuple(node.node_id for node in plan.nodes)
    )


def test_ospf_contract_covers_v1_properties_and_is_deterministic() -> None:
    original = _plan()
    traffic = _traffic(original)
    first = build_isp_validation_contract(traffic.plan, traffic=traffic)
    second = build_isp_validation_contract(traffic.plan, traffic=traffic)
    assert first.to_json() == second.to_json()
    serialized = first.to_json().lower()
    assert all(
        token not in serialized
        for token in ("batfish", "ping", "curl", "vtysh", "shell", "command")
    )
    assert {intent.property for intent in first.intents} == {
        "reachability",
        "isolation",
        "waypoint",
        "adjacency",
    }
    assert sum(intent.property == "adjacency" for intent in first.intents) == 3
    waypoint = next(intent for intent in first.intents if intent.property == "waypoint")
    assert waypoint.level == "optional"
    assert waypoint.path is not None
    assert waypoint.path.must_avoid == ("c",)
    isolation = next(
        intent for intent in first.intents if intent.property == "isolation"
    )
    assert isolation.destination is not None
    assert isolation.destination.address == "192.0.2.0/24"
    assert first.design_source["denied_external_prefixes"] == ["192.0.2.0/24"]


def test_bgp_contract_uses_bgp_plan_sessions_and_prefix_expectations() -> None:
    original = _plan()
    traffic = _traffic(original)
    bgp = compile_bgp_plan(traffic.plan, "ibgp_rr")
    assert bgp is not None
    contract = build_isp_validation_contract(
        traffic.plan, traffic=traffic, bgp_plan=bgp
    )
    bgp_adjacencies = [
        intent
        for intent in contract.intents
        if intent.property == "adjacency"
        and intent.adjacency is not None
        and intent.adjacency.protocol == "bgp"
    ]
    assert len(bgp_adjacencies) == len(bgp.sessions)
    bgp_reachability = [
        intent for intent in contract.intents if intent.id.startswith("reach.bgp.")
    ]
    assert len(bgp_reachability) == len(bgp.expect_reachable)
    assert all(intent.destination.kind == "prefix" for intent in bgp_reachability)


class _ContractRuntime:
    def __init__(self, outputs: dict[tuple[str, str], str]):
        self.outputs = outputs

    def exec(self, host: str, command: str, timeout: float = 10.0) -> str:
        return self.outputs.get((host, command), "")


def test_runtime_verifier_reports_per_intent_evidence() -> None:
    original = _plan()
    traffic = _traffic(original)
    contract = build_isp_validation_contract(traffic.plan, traffic=traffic)
    outputs: dict[tuple[str, str], str] = {}
    for intent in contract.intents:
        if intent.property == "adjacency":
            assert intent.adjacency is not None
            adjacency = intent.adjacency
            key = (adjacency.local_node, "vtysh -c 'show ip ospf neighbor'")
            outputs[key] = outputs.get(key, "") + (
                f"{adjacency.remote_router_id} 1 Full/DR 00:00:30 10.0.0.1 eth0\n"
            )
        elif intent.property == "reachability":
            assert intent.source is not None and intent.destination is not None
            outputs[
                (
                    intent.source.name,
                    f"ping -c 1 -W 2 {intent.destination.address}",
                )
            ] = "1 packets received"
    report = verify_isp_contract(
        _ContractRuntime(outputs), contract=contract, plan=traffic.plan
    )
    assert report.status == "passed"
    assert len(report.results) == len(contract.intents)
    required = {intent.id for intent in contract.intents if intent.level == "required"}
    assert all(
        result.status == "passed"
        for result in report.results
        if result.intent in required
    )
    assert all(result.evidence for result in report.results)
    adjacency_result = next(
        result
        for result in report.results
        if next(
            intent for intent in contract.intents if intent.id == result.intent
        ).property
        == "adjacency"
    )
    assert adjacency_result.evidence["command"] == ("vtysh -c 'show ip ospf neighbor'")
    assert "Full/DR" in adjacency_result.evidence["output"]
    reachability_result = next(
        result
        for result in report.results
        if next(
            intent for intent in contract.intents if intent.id == result.intent
        ).property
        == "reachability"
    )
    assert reachability_result.evidence["command"].startswith("ping -c 1")
    assert reachability_result.evidence["output"] == "1 packets received"
