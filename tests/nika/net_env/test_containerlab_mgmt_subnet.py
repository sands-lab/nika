from nika.net_env.utils.containerlab.mgmt_subnet import (
    mgmt_ipv4_address,
    mgmt_ipv4_subnet,
)


def test_mgmt_subnet_is_unique_per_lab() -> None:
    a = mgmt_ipv4_subnet("isp_abilene__lab1")
    b = mgmt_ipv4_subnet("isp_abilene__lab2")
    assert a != b
    assert a.endswith(".0/24")
    assert mgmt_ipv4_address("min3clos__x", 2).startswith("172.100.")
