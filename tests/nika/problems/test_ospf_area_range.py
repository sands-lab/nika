"""Exact range coverage and unambiguous ABR selection."""

import ipaddress

import pytest

from nika.net_env.campus_lan.lab import CampusLan
from nika.problems.routing_control_plane.ospf import ospf_area_branches

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "profile,size",
    [
        (None, "s"),
        (None, "m"),
        (None, "l"),
        ("iosxr", "s"),
        ("iosxr", "m"),
        ("iosxr", "l"),
        ("routeros", "s"),
    ],
)
def test_campus_exact_area_cover(profile, size):
    branches = ospf_area_branches(CampusLan(topo_size=size, device_profile=profile))
    assert set(branches) == {("router_core_1", "0.0.0.1"), ("router_core_2", "0.0.0.1")}
    count = {"s": 1, "m": 2, "l": 4}[size]
    for (host, _), branch in branches.items():
        networks = [ipaddress.ip_network(p) for p in branch["prefixes"]]
        assert sum(n.prefixlen == 24 for n in networks) == count
        assert sum(n.prefixlen == 31 for n in networks) == count
        assert len(branch["members"]) == count + 1
        assert host in branch["members"]
        assert not set(branch["members"]) & set(branch["observers"])
        covered = set().union(*(set(ipaddress.ip_network(p)) for p in branch["ranges"]))
        assert covered == set().union(*(set(n) for n in networks))
        other = branches[
            ("router_core_2" if host == "router_core_1" else "router_core_1", "0.0.0.1")
        ]
        assert not covered.intersection(
            set().union(*(set(ipaddress.ip_network(p)) for p in other["prefixes"]))
        )


def test_redundant_abr_rejected():
    env = CampusLan()
    machine = env.lab.machines["router_dist_1_1"]
    path = "etc/frr/frr.conf"
    config = machine.fs.readtext(path)
    machine.fs.writetext(
        path,
        config.replace("router ospf\n", "router ospf\n network 10.1.1.0/24 area 0\n"),
    )
    branches = ospf_area_branches(env)
    assert ("router_core_1", "0.0.0.1") not in branches
    assert ("router_core_2", "0.0.0.1") in branches


def test_frr_summary_lsdb_excludes_maxage_and_other_areas():
    import json

    from nika.validation.symptom.ospf_range_probes import frr_summary_lsa_ids

    output = json.dumps(
        {
            "summaryLinkStates": {
                "areas": {
                    "0.0.0.0": [
                        {"linkStateId": "10.1.1.0", "lsaAge": 116, "networkMask": 24},
                        {
                            "linkStateId": "172.16.0.6",
                            "lsaAge": 3600,
                            "networkMask": 31,
                        },
                    ],
                    "0.0.0.1": [
                        {"linkStateId": "10.200.0.0", "lsaAge": 100, "networkMask": 24}
                    ],
                }
            }
        }
    )
    assert frr_summary_lsa_ids(output, "0.0.0.0") == {"10.1.1.0"}
    with pytest.raises(ValueError):
        frr_summary_lsa_ids("[TIMEOUT]", "0.0.0.0")


@pytest.mark.parametrize("configured_area", ["1", "0.0.0.1"])
def test_iosxr_range_preserves_configured_area_spelling(configured_area):
    from nika.net_env.utils.iosxr.fault_ops import IosxrFaultOps

    # XR rejects the same area ID when an edit uses a different spelling.
    config = f"router ospf CORE\n area 0\n !\n area {configured_area}\n !\n!\n"
    provider = IosxrFaultOps(None, "abr")
    provider._show = lambda command: config
    applied = []
    provider._apply = lambda *lines: applied.extend(lines)
    provider.set_ospf_area_ranges("0.0.0.1", ["10.1.1.0/24"], True)
    assert [line.strip() for line in applied if line.startswith(" area ")] == [
        f"area {configured_area}"
    ]
