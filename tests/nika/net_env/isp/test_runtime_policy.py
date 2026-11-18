"""Runtime route-policy and path checks reject incomplete observations."""

import json
import pytest
from nika.net_env.contract import ValidationIntent
from nika.net_env.isp.kathara.lab import Isp

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("reached", [False, True])
def test_runtime_path_requires_destination_after_waypoint(reached) -> None:
    from nika.net_env.isp.kathara.verify import verify_isp_contract
    from nika.net_env.isp.verify_support import intent_command
    from nika.net_env.verify_collection import CommandResult, VerifyCollection

    env = Isp(topo="claranet", igp="ospf", bgp_mode="ibgp_rr")
    from nika.net_env.contract import NetworkEntity, PathConstraint, TrafficSelector

    source, middle, destination = env.plan.nodes[:3]
    intent = ValidationIntent(
        id="path.test",
        description="The destination follows the required waypoint.",
        property="waypoint",
        expected="path_compliant",
        source=NetworkEntity(
            kind="node", name=source.device_name, address=source.loopback
        ),
        destination=NetworkEntity(
            kind="node", name=destination.device_name, address=destination.loopback
        ),
        traffic=TrafficSelector(protocol="icmp"),
        path=PathConstraint(must_traverse=(middle.device_name,)),
    )
    contract = env.validation_contract
    waypoint = next(
        node
        for node in env.plan.nodes
        if node.device_name == intent.path.must_traverse[0]
    )
    trace = f"1  {waypoint.loopback}  1.0 ms\n"
    trace += f"2  {intent.destination.address}  2.0 ms\n" if reached else "2  * * *\n"
    request = intent_command(intent)
    observations = VerifyCollection(
        {(request.node, request.command): CommandResult(trace, 0)},
        [node.device_name for node in env.plan.nodes],
        0,
    )
    report = verify_isp_contract(
        observations,
        contract=contract.model_copy(update={"intents": (intent,)}),
        plan=env.plan,
    )
    assert report.results[0].status == ("passed" if reached else "failed")


@pytest.mark.parametrize("valid", [False, True])
def test_export_denial_requires_a_valid_selected_input_route(valid) -> None:
    from nika.net_env.contract import RoutePolicyExpectation, ValidationIntent
    from nika.net_env.isp.kathara.verify import verify_isp_contract
    from nika.net_env.isp.verify_support import intent_command
    from nika.net_env.verify_collection import CommandResult, VerifyCollection

    env = Isp(topo="claranet", igp="ospf", bgp_mode="ibgp_rr")
    intent = ValidationIntent(
        id="export.test",
        description="Denial needs a usable input route.",
        property="route_policy",
        expected="policy_compliant",
        route_policy=RoutePolicyExpectation(
            node=env.plan.nodes[0].device_name,
            peer="provider",
            remote_address="10.0.0.1",
            prefix="198.51.100.0/24",
            permit=False,
        ),
    )
    request = intent_command(intent)
    observations = VerifyCollection(
        {
            (request.node, request.command): CommandResult(
                '{"advertisedRoutes": {}}', 0
            ),
            (
                request.node,
                "vtysh -c 'show bgp ipv4 unicast 198.51.100.0/24 json'",
            ): CommandResult(
                json.dumps(
                    {"paths": [{"valid": valid, "bestpath": {"overall": True}}]}
                ),
                0,
            ),
        },
        [request.node],
        0,
    )
    report = verify_isp_contract(
        observations,
        contract=env.validation_contract.model_copy(update={"intents": (intent,)}),
        plan=env.plan,
    )
    assert report.results[0].status == ("passed" if valid else "error")


@pytest.mark.parametrize("multipath", [False, True])
def test_exit_selection_distinguishes_overall_best_from_best_per_as(multipath):
    from nika.net_env.contract import BgpRouteExpectation
    from nika.net_env.isp.kathara.verify import verify_isp_contract
    from nika.net_env.isp.verify_support import intent_command
    from nika.net_env.verify_collection import CommandResult, VerifyCollection

    env = Isp(topo="claranet", igp="ospf", bgp_mode="ibgp_rr")
    intent = ValidationIntent(
        id="exit.test",
        description="Only selected paths must use the specified neighboring AS.",
        property="bgp_route",
        expected="selected",
        bgp_route=BgpRouteExpectation(
            node=env.plan.nodes[0].device_name,
            prefix="198.51.101.0/24",
            exit_asn=65002,
        ),
    )
    request = intent_command(intent)
    # FRR reports bestpathFromAs even on a lower-preference, unselected path.
    paths = [
        {"path": "65002", "bestpath": {"bestpathFromAs": 65002, "overall": True}},
        {
            "path": "65003 65002",
            "bestpath": {"bestpathFromAs": 65003},
            "multipath": multipath,
        },
    ]
    observations = VerifyCollection(
        {
            (request.node, request.command): CommandResult(
                json.dumps({"paths": paths}), 0
            )
        },
        [request.node],
        0,
    )
    report = verify_isp_contract(
        observations,
        contract=env.validation_contract.model_copy(update={"intents": (intent,)}),
        plan=env.plan,
    )
    assert report.results[0].status == ("failed" if multipath else "passed")
