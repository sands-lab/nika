"""Business workloads of the campus, Clos and P4 scenarios stay runnable."""

from __future__ import annotations

import pytest

from nika.config import pkg_path
from nika.net_env.net_env_pool import get_net_env_instance
from nika.traffic.workload import FAN_IN_BURSTS

pytestmark = pytest.mark.unit

SCENARIOS = ("campus_lan", "dc_clos", "sdn_l3_clos", "p4_dc_fabric", "p4_dc_gateway")


@pytest.mark.parametrize("size", ["s", "m", "l"])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_workload_runs_installed_programs_on_lab_nodes(
    scenario: str, size: str
) -> None:
    env = get_net_env_instance(scenario, topo_size=size)
    workload = env.business_workload()
    assert workload is not None and workload.processes
    assert set(workload.nodes) <= set(env.lab.machines)
    for process in workload.processes:
        assert pkg_path("traffic", "apps", f"{process.app}.py").is_file()


@pytest.mark.parametrize("size", ["s", "m", "l"])
@pytest.mark.parametrize(
    ("scenario", "receiver"),
    [("p4_dc_fabric", "web_2"), ("p4_dc_gateway", "service_1_1")],
)
def test_fan_in_receiver_has_several_workers(
    scenario: str, receiver: str, size: str
) -> None:
    workload = get_net_env_instance(scenario, topo_size=size).business_workload()
    assert workload.targets(FAN_IN_BURSTS) == [(receiver,)]
    front = next(p for p in workload.processes if "aggregator" in p.args)
    assert front.node == receiver
    assert front.args.count("--worker") >= 2


def test_campus_lab_pcs_refresh_the_catalog_with_gzip() -> None:
    workload = get_net_env_instance("campus_lan", topo_size="l").business_workload()
    syncing = {p.node for p in workload.processes if "--gzip" in p.args}
    assert syncing and all(node.startswith("pc_2_") for node in syncing)
    assert len(syncing) == 64
