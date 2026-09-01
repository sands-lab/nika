from __future__ import annotations

from nika.net_env.contract import (
    AdjacencyExpectation,
    NetworkEntity,
    TrafficSelector,
    ValidationContract,
    ValidationIntent,
    ValidationSanityResult,
)
from nika.validation.base import ValidationSnapshot
from nika.validation.batfish.verifier import BatfishVerifier


class _FakeClient:
    def __init__(self, answers=None, sanity=()):
        self.answers = answers or {}
        self.sanity = sanity

    def initialize(self, snapshot):
        return {"snapshot": snapshot.snapshot_id, "components": {"Batfish": "test"}}

    def execute(self, question):
        return self.answers.get(question.kind, [])

    def sanity_checks(self):
        return self.sanity


class _FailedClient(_FakeClient):
    def initialize(self, snapshot):
        raise ConnectionError("Batfish unavailable")


def _snapshot(tmp_path) -> ValidationSnapshot:
    return ValidationSnapshot(snapshot_id="snapshot", path=tmp_path, metadata={})


def _contract(*intents: ValidationIntent) -> ValidationContract:
    return ValidationContract(
        contract_id="test.batfish",
        scenario="test",
        design_source={"source": "unit design"},
        intents=tuple(sorted(intents, key=lambda item: item.id)),
    )


def _flow(property_name: str) -> ValidationIntent:
    return ValidationIntent(
        id=f"flow.{property_name}",
        description="Flow expectation.",
        property=property_name,
        expected={"reachability": "reachable", "isolation": "unreachable"}[
            property_name
        ],
        source=NetworkEntity(kind="endpoint", name="client", address="10.0.0.2"),
        destination=NetworkEntity(kind="endpoint", name="server", address="10.0.1.2"),
        traffic=TrafficSelector(protocol="icmp"),
    )


def test_flow_mutations_return_concrete_counterexamples(tmp_path) -> None:
    violation = {"Flow": "icmp 10.0.0.2 -> 10.0.1.2", "Traces": "LOOP"}
    report = BatfishVerifier(
        client=_FakeClient(answers={"reachability": [violation]})
    ).verify(_contract(_flow("isolation")), _snapshot(tmp_path))
    result = report.results[0]
    assert result.status == "failed"
    assert result.evidence["violations"][0]["rows"] == [violation]
    assert report.coverage is not None
    assert report.coverage.failed == 1


def test_bgp_remote_as_mismatch_is_reported_as_failed_session(tmp_path) -> None:
    intent = ValidationIntent(
        id="adj.bgp.r1.r2",
        description="Expected BGP peer.",
        property="adjacency",
        expected="established",
        adjacency=AdjacencyExpectation(
            protocol="bgp",
            local_node="r1",
            remote_node="r2",
            local_address="10.0.0.1",
            remote_address="10.0.0.2",
            local_asn=65001,
            remote_asn=65002,
            session_type="ebgp",
        ),
    )
    incompatible = {
        "analysis": "compatibility",
        "Node": "r1",
        "Remote_Node": "r2",
        "Local_IP": "10.0.0.1",
        "Remote_IP": "10.0.0.2",
        "Local_AS": 65001,
        "Remote_AS": 65100,
        "Configured_Status": "HALF_OPEN",
    }
    report = BatfishVerifier(
        client=_FakeClient(answers={"bgp_adjacency": [incompatible]})
    ).verify(_contract(intent), _snapshot(tmp_path))
    assert report.results[0].status == "failed"
    assert report.results[0].evidence["violations"] == [incompatible]
    assert report.coverage is not None
    assert report.coverage.by_adjacency_protocol["bgp"].failed == 1


def test_ospf_area_mismatch_is_reported_with_session_evidence(tmp_path) -> None:
    intent = ValidationIntent(
        id="adj.ospf.r1.r2",
        description="Expected OSPF peer.",
        property="adjacency",
        expected="established",
        adjacency=AdjacencyExpectation(
            protocol="ospf",
            local_node="r1",
            remote_node="r2",
            local_address="10.0.0.1",
            remote_address="10.0.0.2",
            local_router_id="1.1.1.1",
            remote_router_id="2.2.2.2",
            ospf_area="0.0.0.0",
        ),
    )
    mismatch = {
        "Interface": "r1[eth0]",
        "Remote_Interface": "r2[eth0]",
        "IP": "10.0.0.1",
        "Remote_IP": "10.0.0.2",
        "Area": 0,
        "Remote_Area": 1,
        "Session_Status": "AREA_MISMATCH",
    }
    report = BatfishVerifier(
        client=_FakeClient(answers={"ospf_adjacency": [mismatch]})
    ).verify(_contract(intent), _snapshot(tmp_path))
    assert report.results[0].status == "failed"
    assert report.results[0].evidence["violations"] == [mismatch]
    assert report.coverage is not None
    assert report.coverage.by_adjacency_protocol["ospf"].failed == 1


def test_unsupported_intent_and_sanity_error_are_counted_separately(tmp_path) -> None:
    unsupported = _flow("reachability").model_copy(
        update={
            "source": NetworkEntity(
                kind="prefix", name="source-prefix", address="10.0.0.0/24"
            )
        }
    )
    sanity = (
        ValidationSanityResult(
            check="forwarding_loops", status="failed", duration_ms=1
        ),
    )
    report = BatfishVerifier(client=_FakeClient(sanity=sanity)).verify(
        _contract(unsupported), _snapshot(tmp_path)
    )
    assert report.results[0].status == "unsupported"
    assert report.results[0].supported is False
    assert report.coverage is not None
    assert report.coverage.unsupported == 1
    assert report.status == "failed"


def test_provider_initialization_error_returns_a_complete_report(tmp_path) -> None:
    contract = _contract(_flow("reachability"), _flow("isolation"))
    report = BatfishVerifier(client=_FailedClient()).verify(
        contract, _snapshot(tmp_path)
    )
    assert report.status == "error"
    assert all(result.status == "error" for result in report.results)
    assert report.sanity[0].check == "provider_initialization"


def test_rpki_flow_is_explicitly_unsupported_but_adjacency_is_modeled(
    tmp_path,
) -> None:
    flow = _flow("reachability")
    adjacency = ValidationIntent(
        id="adj.bgp.r1.r2",
        description="Expected BGP peer.",
        property="adjacency",
        expected="established",
        adjacency=AdjacencyExpectation(
            protocol="bgp",
            local_node="r1",
            remote_node="r2",
            local_address="10.0.0.1",
            remote_address="10.0.0.2",
            local_asn=65001,
            remote_asn=65002,
            session_type="ebgp",
        ),
    )
    contract = ValidationContract(
        contract_id="test.rpki",
        scenario="test",
        design_source={"bgp_mode": "ebgp", "rpki": True},
        intents=tuple(sorted((flow, adjacency), key=lambda item: item.id)),
    )
    established = {
        "analysis": "establishment",
        "Node": "r1",
        "Remote_Node": "r2",
        "Local_IP": "10.0.0.1",
        "Remote_IP": "10.0.0.2",
        "Established_Status": "ESTABLISHED",
    }
    compatible = {
        "analysis": "compatibility",
        "Node": "r1",
        "Remote_Node": "r2",
        "Local_IP": "10.0.0.1",
        "Remote_IP": "10.0.0.2",
        "Configured_Status": "UNIQUE_MATCH",
        "Local_AS": 65001,
        "Remote_AS": 65002,
        "Session_Type": "EBGP_SINGLEHOP",
    }
    report = BatfishVerifier(
        client=_FakeClient(answers={"bgp_adjacency": [compatible, established]})
    ).verify(contract, _snapshot(tmp_path))
    by_id = {result.intent: result for result in report.results}
    assert by_id[flow.id].status == "unsupported"
    assert "RPKI" in (by_id[flow.id].reason or "")
    assert by_id[adjacency.id].status == "passed"
