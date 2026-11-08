"""Fixed-VRP ROV checks use Batfish policy evaluation and original FRR sketches."""

import pytest

from nika.net_env.contract import BgpRouteExpectation, ValidationContract
from nika.net_env.isp.contract import bgp_adjacency_intents
from nika.problems.rca.inventory import load_offline_net_env
from nika.validation.batfish.kathara import build_kathara_snapshot
from nika.validation.batfish.verifier import BatfishVerifier
from tests.support.prerequisites import docker_available


@pytest.mark.unit
def test_absent_route_cannot_constrain_its_exit_as():
    with pytest.raises(ValueError, match="absent BGP route"):
        BgpRouteExpectation(
            node="r1", prefix="203.0.113.0/24", present=False, exit_asn=65001
        )


@pytest.mark.unit
def test_route_expectations_distinguish_best_and_accepted_origins():
    local, leaked = [], ["65002"]
    # The unauthorized origin must stay out of every accepted path.
    dormant = BgpRouteExpectation(
        node="r1",
        prefix="203.0.113.0/24",
        origin_asn=65002,
        present=False,
        selected=False,
    )
    assert dormant.satisfied_by([local])
    assert not dormant.satisfied_by([local, leaked])
    # A leak is accepted once any path from the origin is present.
    accepted = dormant.model_copy(update={"present": True})
    assert accepted.satisfied_by([local, leaked])
    # A best-path expectation constrains every best path.
    best = accepted.model_copy(update={"selected": True})
    assert not best.satisfied_by([local, leaked])
    assert best.satisfied_by([leaked])


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.skipif(not docker_available(), reason="Docker unavailable")
@pytest.mark.parametrize("scenario", ["isp_abilene_ebgp_rpki", "isp_geant_ebgp_rpki"])
def test_fixed_vrp_projection_with_batfish_route_policy_evaluation(scenario, tmp_path):
    import docker
    from docker.errors import NotFound
    from pybatfish.client.session import Session
    from pybatfish.datamodel.route import BgpRoute
    from nika.validation.batfish.service import (
        BATFISH_CONTAINER_NAME,
        ensure_batfish_service,
    )

    env = load_offline_net_env(scenario)
    contract = ValidationContract(
        contract_id=scenario + ".vrp-regression",
        scenario=scenario,
        design_source={},
        intents=tuple(
            sorted(
                bgp_adjacency_intents(env.bgp_plan, verify_rr_roles=True),
                key=lambda i: i.id,
            )
        ),
    )
    client = docker.from_env()
    try:
        client.containers.get(BATFISH_CONTAINER_NAME)
        owned = False
    except NotFound:
        owned = True
    try:
        ensure_batfish_service()
        snapshot = build_kathara_snapshot(env, contract, tmp_path)
        report = BatfishVerifier().verify(contract, snapshot)
        report.write(tmp_path / "report.json")
        assert report.status == "passed"
        session = Session(host="127.0.0.1")
        identity = contract.contract_id.replace(".", "_")
        session.set_network("nika_" + identity)
        observer = env.bgp_plan.inventory["rov_observer"]
        vectors = [
            ("203.0.113.0/24", [65001], "PERMIT"),
            ("203.0.113.0/24", [65002], "DENY"),
            ("203.0.113.0/25", [65001], "DENY"),
            ("203.0.113.0/24", [165001], "DENY"),
            ("203.0.113.0/24", [65002, 65001], "PERMIT"),
            ("203.0.113.0/24", [65001, 65002], "DENY"),
            ("198.51.102.0/24", [65002], "PERMIT"),
            ("203.0.113.0/24", [], "DENY"),
        ]
        for prefix, path, action in vectors:
            route = BgpRoute(
                network=prefix,
                originatorIp="0.0.0.0",
                originType="igp",
                protocol="bgp",
                sourceProtocol="bgp",
                nextHopIp="0.0.0.1",
                asPath=[[a] for a in path],
            )
            rows = (
                session.q.testRoutePolicies(
                    nodes=observer,
                    policies="BGP-IN",
                    direction="in",
                    inputRoutes=[route],
                )
                .answer(snapshot="snapshot_" + identity)
                .frame()
            )
            assert rows.Action.tolist() == [action], (prefix, path, rows)
    finally:
        if owned:
            try:
                client.containers.get(BATFISH_CONTAINER_NAME).remove(force=True)
            except NotFound:
                pass
        client.close()
