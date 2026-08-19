from __future__ import annotations

import pytest

from nika.net_env.contract import (
    NetworkEntity,
    PathConstraint,
    TrafficSelector,
    ValidationIntent,
)
from nika.validation.batfish.compiler import UnsupportedIntentError, compile_intent


def _flow_intent(property_name: str = "reachability") -> ValidationIntent:
    kwargs = {}
    if property_name == "waypoint":
        kwargs["path"] = PathConstraint(must_traverse=("r2",), must_avoid=("r4",))
    return ValidationIntent(
        id=f"test.{property_name}",
        description="Compiler test intent.",
        property=property_name,
        expected={
            "reachability": "reachable",
            "isolation": "unreachable",
            "waypoint": "path_compliant",
        }[property_name],
        source=NetworkEntity(kind="endpoint", name="client", address="10.0.0.2"),
        destination=NetworkEntity(
            kind="service", name="dns", address="10.0.1.53", node="server"
        ),
        traffic=TrafficSelector(protocol="udp", destination_port=53),
        **kwargs,
    )


def test_compiles_reachability_and_isolation_as_counterexample_queries() -> None:
    reach = compile_intent(_flow_intent())[0]
    isolation = compile_intent(_flow_intent("isolation"))[0]
    assert reach.parameters["actions"] == "failure"
    assert isolation.parameters["actions"] == "success"
    assert reach.parameters["headers"] == {
        "srcIps": "10.0.0.2",
        "dstIps": "10.0.1.53",
        "ipProtocols": "UDP",
        "dstPorts": "53",
    }
    assert reach.parameters["path_constraints"] == {
        "startLocation": "client",
        "endLocation": None,
    }


def test_compiles_waypoint_as_reachability_and_path_violation_queries() -> None:
    questions = compile_intent(_flow_intent("waypoint"))
    assert len(questions) == 3
    assert questions[1].parameters["path_constraints"]["transitLocations"] == "r4"
    assert questions[2].parameters["path_constraints"]["forbiddenLocations"] == "r2"


def test_prefix_without_source_location_is_explicitly_unsupported() -> None:
    intent = _flow_intent().model_copy(
        update={
            "source": NetworkEntity(
                kind="prefix", name="source-prefix", address="10.0.0.0/24"
            )
        }
    )
    with pytest.raises(UnsupportedIntentError, match="has no Batfish location"):
        compile_intent(intent)


def test_prefix_destination_excludes_ipv4_network_and_broadcast_addresses() -> None:
    intent = _flow_intent().model_copy(
        update={
            "destination": NetworkEntity(
                kind="prefix", name="service-prefix", address="192.0.2.0/24"
            )
        }
    )
    question = compile_intent(intent)[0]
    assert question.parameters["headers"]["dstIps"] == "192.0.2.1-192.0.2.254"
