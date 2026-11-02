from __future__ import annotations

import pytest

from nika.net_env.contract import AdjacencyExpectation, ValidationIntent
from nika.validation.batfish.client import _normalize_interfaces
from nika.validation.batfish.compiler import compile_intent
from nika.validation.batfish.verifier import _evaluate_adjacency

pytestmark = pytest.mark.unit


def _isis_intent() -> ValidationIntent:
    return ValidationIntent(
        id="adj.isis.r1.r2.eth0",
        description="Expected IS-IS link.",
        property="adjacency",
        expected="established",
        adjacency=AdjacencyExpectation(
            protocol="isis",
            isis_level=2,
            local_node="r1",
            remote_node="r2",
            local_address="10.0.0.0",
            remote_address="10.0.0.1",
            local_interface="eth0",
            remote_interface="eth0",
            local_router_id="1.1.1.1",
            remote_router_id="2.2.2.2",
        ),
    )


def test_compiles_isis_adjacency_as_topology_edge_query():
    question = compile_intent(_isis_intent())[0]

    assert question.kind == "isis_adjacency"
    assert question.parameters["local_interface"] == "eth0"
    assert question.parameters["remote_interface"] == "eth0"


@pytest.mark.parametrize("native", ["GigabitEthernet0/0/0/0", "ether2"])
def test_batfish_edge_aliases_normalize_to_original_interfaces(native):
    rows = [
        {
            "Interface": f"r1[{native}]",
            "Remote_Interface": f"r2[{native}]",
        }
    ]

    normalized = _normalize_interfaces(
        rows,
        {
            "r1": {native: "eth0"},
            "r2": {native: "eth0"},
        },
    )

    assert normalized == [{"Interface": "r1[eth0]", "Remote_Interface": "r2[eth0]"}]


def test_isis_evaluator_matches_parallel_link_by_both_interfaces():
    expectation = _isis_intent().adjacency
    assert expectation is not None
    edges = [
        {"Interface": "r1[eth1]", "Remote_Interface": "r2[eth1]"},
        {"Interface": "r1[eth0]", "Remote_Interface": "r2[eth1]"},
        {"Interface": "r1[eth0]", "Remote_Interface": "r2[eth0]"},
    ]

    passed, _, violations, matched = _evaluate_adjacency(expectation, edges)

    assert passed
    assert matched == [edges[-1]]
    assert violations == []


def test_isis_evaluator_rejects_wrong_parallel_counterpart():
    expectation = _isis_intent().adjacency
    assert expectation is not None
    edges = [
        {"Interface": "r1[eth0]", "Remote_Interface": "r2[eth1]"},
        {"Interface": "r1[eth1]", "Remote_Interface": "r2[eth0]"},
    ]

    passed, _, violations, matched = _evaluate_adjacency(expectation, edges)

    assert not passed
    assert matched == []
    assert violations == edges
