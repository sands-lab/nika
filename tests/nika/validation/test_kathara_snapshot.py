"""Reject unsupported projections instead of silently claiming VRF coverage."""

from pathlib import Path
import pytest
from nika.net_env.enterprise_branch.lab import EnterpriseBranch
from nika.validation.batfish.kathara import build_kathara_snapshot
from nika.workflows.validation.scenario_spec import ScenarioExperiment

pytestmark = pytest.mark.contract


def test_vrf_requires_explicit_static_underlay_scope(tmp_path):
    spec = ScenarioExperiment.model_validate_json(
        (
            Path(__file__).resolve().parents[3]
            / "experiment/scenario_validation/enterprise.json"
        ).read_text()
    )
    env = EnterpriseBranch()
    with pytest.raises(ValueError, match="VRF interface attachment"):
        build_kathara_snapshot(env, spec.healthy, tmp_path / "unsupported")
    snapshot = build_kathara_snapshot(
        env, spec.healthy, tmp_path / "underlay", routing_model="linux-static"
    )
    config = (snapshot.path / "configs" / "hq_edge.cfg").read_text()
    assert "ip route 100.64.0.17/32 100.64.0.2" in config
    assert "router bgp" not in config
    assert "10.0.10.1/24" not in config
    assert snapshot.metadata["routing_model"] == "linux-static"

    with pytest.raises(ValueError, match="default-table router endpoints"):
        build_kathara_snapshot(
            env,
            spec.runtime_requirements,
            tmp_path / "invalid-overlay-claim",
            routing_model="linux-static",
        )


def test_runtime_counterexample_requires_a_real_expected_violation():
    from pydantic import ValidationError
    from nika.workflows.validation.scenario_spec import RoutingRuntimeMutation

    with pytest.raises(ValidationError):
        RoutingRuntimeMutation(
            name="vacuous",
            node="r1",
            commands=("show ip route",),
            recovery=("show ip route",),
            expected_failed_ids=(),
        )


def test_concrete_host_default_route_preserves_gateway():
    from nika.validation.batfish.kathara import startup_static_routes

    assert startup_static_routes(
        "ip route add default via 195.11.14.1 dev eth0\n"
        "ip route add 10.0.0.0/24 via 192.0.2.1\n"
        "ip route add default via 203.0.113.1 table 100\n"
    ) == [("0.0.0.0/0", "195.11.14.1"), ("10.0.0.0/24", "192.0.2.1")]


def test_snapshot_rebuild_removes_obsolete_devices(tmp_path):
    from nika.net_env.net_env_pool import get_net_env_instance

    spec = ScenarioExperiment.model_validate_json(
        (
            Path(__file__).resolve().parents[3]
            / "experiment/scenario_validation/clos.json"
        ).read_text()
    )
    env = get_net_env_instance(spec.scenario, topo_size=spec.topo_size)
    snapshot = build_kathara_snapshot(env, spec.healthy, tmp_path)
    obsolete = snapshot.path / "configs" / "obsolete-router.cfg"
    obsolete.write_text("hostname obsolete-router\n")
    old_hosts = snapshot.path / "hosts"
    old_hosts.mkdir(exist_ok=True)
    (old_hosts / "obsolete-host.json").write_text('{"hostname": "obsolete-host"}')
    build_kathara_snapshot(env, spec.healthy, tmp_path)
    assert not obsolete.exists()
    assert not (old_hosts / "obsolete-host.json").exists()
    assert (snapshot.path / "configs/leaf_router_0_0.cfg").exists()
