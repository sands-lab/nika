"""ISP business traffic and the scenario facts release cases rely on."""

from __future__ import annotations

import pytest

from nika.config import pkg_path
from nika.traffic.workload import BULK_TRANSIT
from nika.workflows.benchmark.inject_resolve import _get_net_env_for_benchmark

pytestmark = pytest.mark.unit


def _env(scenario: str, size: str = "", **options: str):
    return _get_net_env_for_benchmark(scenario, size, isp_options=options or None)


@pytest.mark.parametrize(
    ("scenario", "size", "options"),
    [
        ("isp_abilene", "s", {"igp": "isis", "bgp_mode": "none"}),
        ("isp_geant", "m", {"igp": "ospf", "bgp_mode": "ebgp"}),
        ("isp_abilene_ebgp_rpki", "", {}),
    ],
)
def test_workload_stays_inside_designed_reachability(scenario, size, options) -> None:
    env = _env(scenario, size, **options)
    workload = env.business_workload()
    nodes = {n.device_name for n in env.plan.nodes} | {
        h.host_name for h in env.traffic.hosts
    }
    site = env.bgp_plan.customer_site if env.bgp_plan is not None else None
    if site is not None:
        nodes.add(site.host_name)
    assert set(workload.nodes) <= nodes
    for process in workload.processes:
        assert pkg_path("traffic", "apps", f"{process.app}.py").is_file()
    intent = env.probe_intent()
    isolated = intent.isolation_matcher() if intent else (lambda _a, _b: False)
    address_of = {h.address: h.host_name for h in env.traffic.hosts}
    servers = set()
    for process in workload.processes:
        if process.app != "file_sync":
            continue
        for url in process.args:
            if url.startswith("http://"):
                server = address_of[url.split("/")[2].split(":")[0]]
                servers.add(server)
                assert not isolated(process.node, server)
    # A few transfers run, each served by a stub's data set.
    assert servers
    assert servers <= {h.host_name for h in env.traffic.hosts}
    assert len(servers) < len(env.traffic.hosts)


def test_link_capacity_release_target_is_loaded() -> None:
    env = _env("isp_di-yuan", "s", igp="isis", bgp_mode="none")
    assert env.business_workload().covers(BULK_TRANSIT, ("n_3", "eth4"))


def test_abilene_backhaul_tunnel_has_reduced_mtu_on_both_ends() -> None:
    env = _env("isp_abilene", "s", igp="isis", bgp_mode="none")
    mtus = {
        (node.device_name, iface.peer_device): iface.mtu
        for node in env.plan.nodes
        for iface in node.interfaces
        if iface.mtu
    }
    assert mtus == {("atlam5", "atlang"): 1476, ("atlang", "atlam5"): 1476}


def test_rpki_content_prefixes_have_a_legitimate_origin() -> None:
    bgp = _env("isp_abilene_ebgp_rpki").bgp_plan
    inventory = bgp.inventory
    leak = set(inventory["leak_prefixes"])
    announced = {p.device for p in bgp.originated if p.prefix in leak}
    assert announced == {inventory["legitimate_origin_device"]}
    origin = next(n for n in bgp.nodes if n.device_name in announced)
    assert origin.asn == inventory["legitimate_origin_asn"]
    leaker = next(n for n in bgp.nodes if n.device_name == inventory["leaker_device"])
    assert leak <= {p.prefix for p in leaker.originated}
    assert leak <= set(leaker.export_deny_prefixes)
    assert any(
        s.remote_device == inventory["leaker_device"]
        for s in bgp.sessions
        if s.local_device == inventory["non_rov_observer"]
    )


def test_isp_hijack_targets_divert_a_service_client() -> None:
    env = _env("isp_janos-us", "m", igp="isis", bgp_mode="ibgp_rr")
    targets = {(t.hijacker, t.prefix): t for t in env.bgp_hijack_targets()}
    target = targets[("albany", "203.0.115.0/24")]
    clients = {
        p.node
        for p in env.business_workload().processes
        if p.app == "web_client" and p.args[0] == target.probe_ip
    }
    assert target.observer_host in clients
