"""The HTTP ACL, symptom probe, and Oracle use the declared service port."""

from types import SimpleNamespace

import pytest

from nika.net_env.net_env_pool import get_net_env_instance
from nika.problems.forwarding_encapsulation_policy.acl import (
    HostACLBlock,
    HostACLBlockParams,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "backend,profile", [("kathara", "frr"), ("containerlab", "nokia_srlinux")]
)
def test_http_acl_uses_isp_workload_service(backend, profile):
    problem = object.__new__(HostACLBlock)
    problem.net_env = get_net_env_instance(
        "isp_dfn-bwin",
        backend=backend,
        device_profile=profile,
        igp="isis",
        bgp_mode="none",
        rpki=False,
    )
    problem.scenario_name = "isp_dfn-bwin"
    problem.runtime = SimpleNamespace(get_data_plane_host_ip=lambda _: "10.254.0.2")
    params = HostACLBlockParams(host_name="pc_berlin", protocol="http")
    path = problem.probe_path(params)
    assert path.src_host == "pc_berlin"
    assert path.http_url == "http://10.254.0.6:8080/"
    assert problem._rule(params) == ("inet", "tcp dport 8080 drop")
    playbook = problem.oracle_playbook(params)
    assert "8080" in playbook.finding
    assert problem._rule(HostACLBlockParams(host_name="pc_berlin")) == (
        "ip",
        "ip protocol icmp drop",
    )
