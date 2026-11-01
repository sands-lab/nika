import random

import pytest

from nika.traffic.apps.object_sync import MIXES, ObjectSizes
from typing import get_args

from nika.traffic.profiles import (
    Endpoint,
    Pattern,
    TrafficProfile,
    _parse_addresses,
    parse_traffic_option,
    plan_background,
    traffic_matrix,
)

pytestmark = pytest.mark.unit

HOSTS = [f"h{i}" for i in range(6)]
ENDPOINTS = [Endpoint(h, f"10.0.0.{i + 1}") for i, h in enumerate(HOSTS)]


def _isolated(a: str, b: str) -> bool:
    # h0 and h1 are a designed island.
    return ({a, b} & {"h0", "h1"}) and not {a, b} <= {"h0", "h1"}


@pytest.mark.parametrize("pattern", [p for p in get_args(Pattern) if p != "demand"])
def test_matrix_sums_to_one_and_respects_isolation(pattern: str) -> None:
    matrix = traffic_matrix(TrafficProfile(pattern=pattern), HOSTS, isolated=_isolated)
    assert sum(matrix.values()) == pytest.approx(1.0)
    assert all(src != dst and not _isolated(src, dst) for src, dst in matrix)


def test_matrix_is_reproducible_and_seeded() -> None:
    a = traffic_matrix(TrafficProfile(pattern="gravity", seed=3), HOSTS)
    assert a == traffic_matrix(TrafficProfile(pattern="gravity", seed=3), HOSTS)
    assert a != traffic_matrix(TrafficProfile(pattern="gravity", seed=4), HOSTS)


def test_permutation_sends_to_one_receiver_each() -> None:
    matrix = traffic_matrix(TrafficProfile(pattern="permutation"), HOSTS)
    sources = [src for src, _ in matrix]
    receivers = [dst for _, dst in matrix]
    assert len(sources) == len(set(sources)) == len(HOSTS)
    assert len(set(receivers)) == len(receivers)


def test_hotspot_sends_its_share_to_the_hot_hosts() -> None:
    profile = TrafficProfile(pattern="hotspot", hotspots=1, hot_fraction=0.7)
    matrix = traffic_matrix(profile, HOSTS)
    by_dst: dict[str, float] = {}
    for (_, dst), weight in matrix.items():
        by_dst[dst] = by_dst.get(dst, 0.0) + weight
    assert max(by_dst.values()) == pytest.approx(0.7)


def test_demand_uses_the_scenario_matrix_and_needs_one() -> None:
    demands = {("h0", "h1"): 3.0, ("h1", "h0"): 1.0, ("h0", "h0"): 9.0}
    matrix = traffic_matrix(TrafficProfile(pattern="demand"), HOSTS, demands=demands)
    assert matrix == {("h0", "h1"): 0.75, ("h1", "h0"): 0.25}
    with pytest.raises(ValueError, match="declares none"):
        traffic_matrix(TrafficProfile(pattern="demand"), HOSTS)


def test_plan_splits_load_by_matrix_rows() -> None:
    profile = TrafficProfile(pattern="uniform", load_mbps=6)
    workload = plan_background(profile, ENDPOINTS)
    senders = [p for p in workload.processes if p.command == "backup-agent"]
    sinks = [p for p in workload.processes if p.command == "object-store"]
    assert len(senders) == len(sinks) == len(HOSTS)
    loads = [float(p.args[p.args.index("--rate-kbps") + 1]) for p in senders]
    assert sum(loads) == pytest.approx(6000, rel=1e-3)
    assert senders[0].args.count("--store") == len(HOSTS) - 1


def test_incast_adds_a_synchronized_fan_in() -> None:
    profile = TrafficProfile(pattern="incast", fan_in=4)
    workload = plan_background(profile, ENDPOINTS)
    shards = [
        p for p in workload.processes if p.app == "fanout" and p.args[0] == "worker"
    ]
    frontends = [p for p in workload.processes if p.args[:1] == ("aggregator",)]
    assert len(shards) == 4 and len(frontends) == 1
    assert frontends[0].node not in {p.node for p in shards}


def test_profile_case_value_and_cli_form_agree() -> None:
    parsed = parse_traffic_option("gravity,load_mbps=3,flow_sizes=hadoop")
    assert parsed.case_value() == {
        "pattern": "gravity",
        "flow_sizes": "hadoop",
        "load_mbps": 3.0,
    }
    assert TrafficProfile.model_validate("uniform").case_value() == {
        "pattern": "uniform"
    }
    with pytest.raises(ValueError):
        TrafficProfile.model_validate({"pattern": "uniform", "bogus": 1})


def test_address_skips_management_and_telemetry_interfaces() -> None:
    output = (
        "1: eth0    inet 172.20.20.5/24 brd 172.20.20.255 scope global eth0\n"
        "2: eth2    inet 172.28.0.9/16 scope global eth2\n"
        "3: eth1    inet 10.1.0.2/24 scope global eth1\n"
        "4: cni0    inet 10.42.0.1/24 scope global cni0\n"
    )
    assert _parse_addresses(output, skip_eth0=True) == "10.1.0.2"
    assert _parse_addresses(output, skip_eth0=False) == "172.20.20.5"


@pytest.mark.parametrize("name", sorted(MIXES))
def test_clipped_flow_size_mean_matches_samples(name: str) -> None:
    sizes = ObjectSizes(name, 2048 * 1024)
    rng = random.Random(0)
    draws = [sizes.draw(rng) for _ in range(40000)]
    assert max(draws) <= 2048 * 1024
    assert sum(draws) / len(draws) == pytest.approx(sizes.mean(), rel=0.05)


def test_k8s_traffic_pods_pin_programs_to_their_node() -> None:
    from nika.net_env.utils.k8s_traffic_pods import _manifest
    from nika.traffic.workload import WorkloadProcess

    process = WorkloadProcess("worker_1", "object_sync", "object-store", ("store",))
    namespace, configmap, pod = _manifest([process], "python:3")["items"]
    assert "object_sync.py" in configmap["data"]
    spec = pod["spec"]
    assert spec["nodeName"] == "worker_1" and spec["hostNetwork"]
    assert spec["containers"][0]["imagePullPolicy"] == "Never"


def test_profile_fits_the_routers_send_cap() -> None:
    from nika.traffic.profiles import fit_to_rate_limit

    assert fit_to_rate_limit(TrafficProfile(pattern="gravity"), None).load_mbps == 4
    capped = fit_to_rate_limit(TrafficProfile(pattern="gravity"), 1000)
    assert capped.load_mbps == pytest.approx(0.15)
    incast = fit_to_rate_limit(TrafficProfile(pattern="incast"), 1000)
    answers_kbps = incast.fan_in * incast.incast_kb * 8.192 / incast.incast_period_sec
    assert incast.load_mbps * 1000 + answers_kbps <= 150


def test_pair_replication_fits_the_routers_send_cap() -> None:
    from nika.net_env.utils.pair_workload import build_pair_workload
    from nika.traffic.workload import BULK_TRANSIT

    hops = (("a", "eth0"),)
    free = build_pair_workload(("a", "10.0.0.1"), ("b", "10.0.0.2"), hops)
    capped = build_pair_workload(("a", "10.0.0.1"), ("b", "10.0.0.2"), hops, 1000)
    rates = [a for p in capped.processes for a in p.args if "rate=" in a]
    assert all(int(r.split("rate=")[1].rstrip("k")) <= 600 for r in rates)
    assert free.targets(BULK_TRANSIT) and not capped.targets(BULK_TRANSIT)
