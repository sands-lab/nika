from ipaddress import IPv4Address

import pytest
from nika.net_env.utils.containerlab.mgmt_subnet import (
    mgmt_ipv4_address,
    mgmt_ipv4_addresses,
    mgmt_ipv4_network,
    mgmt_ipv4_subnet,
)

pytestmark = pytest.mark.unit


def test_mgmt_subnet_is_unique_per_lab() -> None:
    a = mgmt_ipv4_subnet("isp_abilene__lab1")
    b = mgmt_ipv4_subnet("isp_abilene__lab2")
    assert a != b
    assert a.endswith(".0/24")
    assert mgmt_ipv4_address("min3clos__x", 2).startswith("172.100.")


def test_large_labs_get_a_separate_mgmt_22() -> None:
    small = mgmt_ipv4_addresses("isp_abilene__lab1", 253)
    assert small[0] == mgmt_ipv4_address("isp_abilene__lab1", 2)
    assert small[-1] == mgmt_ipv4_address("isp_abilene__lab1", 254)

    network = mgmt_ipv4_network("isp_cogentco__lab1", 394)
    addresses = mgmt_ipv4_addresses("isp_cogentco__lab1", 394)
    assert network.prefixlen == 22 and str(network).startswith("172.101.")
    assert len(set(addresses)) == 394
    assert all(IPv4Address(a) in network for a in addresses)
    assert str(network[1]) not in addresses
    with pytest.raises(ValueError):
        mgmt_ipv4_network("isp_huge", 1022)
