from __future__ import annotations

from types import SimpleNamespace

from nika.net_env.contract import (
    AdjacencyExpectation,
    NetworkEntity,
    TrafficSelector,
    ValidationContract,
    ValidationIntent,
    ValidationReport,
    ValidationResult,
)
from nika.validation.effect import (
    FailureEffectContract,
    build_failure_effect_contract,
    compare_failure_effect,
)


def _contract() -> ValidationContract:
    return ValidationContract(
        contract_id="isp.test.ospf.none",
        scenario="isp",
        design_source={},
        intents=(
            ValidationIntent(
                id="adj.ospf.r1.r2",
                description="OSPF r1-r2",
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
                ),
            ),
        ),
    )


def _report(
    contract: ValidationContract, verifier: str, status: str
) -> ValidationReport:
    return ValidationReport.from_results(
        contract,
        verifier,
        (
            ValidationResult(
                intent=contract.intents[0].id,
                verifier=verifier,
                status=status,
                duration_ms=1,
                supported=status != "unsupported",
                evidence={"command": f"{verifier} probe", "output": status},
            ),
        ),
    )


def test_build_ospf_failure_effect_from_declared_problem_metadata() -> None:
    problem = SimpleNamespace(
        root_cause_name="ospf_area_misconfiguration",
        effect_protocol="ospf",
        _resolved_params=SimpleNamespace(host_name="r1"),
    )
    effect = build_failure_effect_contract(problem, _contract())
    assert effect.supported is True
    assert effect.expected_change[0].from_state == "established"
    assert effect.expected_change[0].to_state == "not_established"


def test_reachability_effect_only_targets_prefix_destinations() -> None:
    """A missing-advertisement failure on a node must not claim edge intents.

    ``reach.edge.pc_a.pc_b`` destinations are endpoints hosted behind the node
    (IGP stubs stay reachable), so only prefix destinations originated by the
    node belong in the declared change.
    """
    contract = ValidationContract(
        contract_id="isp.test.ebgp",
        scenario="isp",
        design_source={},
        intents=(
            ValidationIntent(
                id="reach.bgp.r3.bgp_198_51_100_0_24.icmp",
                description="Observer must reach the designed prefix.",
                property="reachability",
                expected="reachable",
                source=NetworkEntity(kind="node", name="r3", address="10.255.0.3"),
                destination=NetworkEntity(
                    kind="prefix",
                    name="bgp_198_51_100_0_24",
                    address="198.51.100.0/24",
                    node="r2",
                ),
                traffic=TrafficSelector(protocol="icmp"),
            ),
            ValidationIntent(
                id="reach.edge.pc_a.pc_b.icmp",
                description="Edge stub must reach the other edge stub.",
                property="reachability",
                expected="reachable",
                source=NetworkEntity(
                    kind="endpoint",
                    name="pc_a",
                    address="10.0.0.1",
                    node="r1",
                ),
                destination=NetworkEntity(
                    kind="endpoint",
                    name="pc_b",
                    address="10.0.0.5",
                    node="r2",
                ),
                traffic=TrafficSelector(protocol="icmp"),
            ),
        ),
    )
    problem = SimpleNamespace(
        root_cause_name="bgp_missing_route_advertisement",
        effect_property="reachability",
        _resolved_params=SimpleNamespace(host_name="r2"),
    )
    effect = build_failure_effect_contract(problem, contract)
    assert effect.supported is True
    assert [item.intent for item in effect.expected_change] == [
        "reach.bgp.r3.bgp_198_51_100_0_24.icmp"
    ]
    expectation = effect.expected_change[0]
    assert expectation.from_state == "reachable"
    assert expectation.to_state == "unreachable"


def test_compare_effect_requires_static_and_runtime_agreement() -> None:
    contract = _contract()
    effect = build_failure_effect_contract(
        SimpleNamespace(
            root_cause_name="ospf_area_misconfiguration",
            effect_protocol="ospf",
            _resolved_params=SimpleNamespace(host_name="r1"),
        ),
        contract,
    )
    report = compare_failure_effect(
        effect,
        contract,
        healthy_static=_report(contract, "batfish", "passed"),
        faulty_static=_report(contract, "batfish", "failed"),
        healthy_runtime=_report(contract, "isp-runtime-v1", "passed"),
        faulty_runtime=_report(contract, "isp-runtime-v1", "failed"),
    )
    assert report.status == "PASS"
    intent_evidence = report.evidence["intents"]["adj.ospf.r1.r2"]
    assert intent_evidence["expected"]["faulty"] == "not_established"
    assert intent_evidence["batfish"]["faulty"]["state"] == "not_established"
    assert intent_evidence["runtime"]["faulty"]["evidence"] == (
        {"command": "isp-runtime-v1 probe", "output": "failed"}
    )

    mismatch = compare_failure_effect(
        effect,
        contract,
        healthy_static=_report(contract, "batfish", "passed"),
        faulty_static=_report(contract, "batfish", "failed"),
        healthy_runtime=_report(contract, "isp-runtime-v1", "passed"),
        faulty_runtime=_report(contract, "isp-runtime-v1", "passed"),
    )
    assert mismatch.status == "STATIC_RUNTIME_MISMATCH"


def test_undeclared_failure_is_unsupported() -> None:
    contract = _contract()
    effect = FailureEffectContract(
        failure="link_down",
        supported=False,
        reason="failure has no Batfish-modeled effect declaration",
    )
    report = compare_failure_effect(
        effect,
        contract,
        healthy_static=None,
        faulty_static=None,
        healthy_runtime=None,
        faulty_runtime=None,
    )
    assert report.status == "UNSUPPORTED"
