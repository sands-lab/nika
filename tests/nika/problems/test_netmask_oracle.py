"""Local netmask overrides remain distinct from DHCP-provided bad masks."""

import pytest
from nika.problems.addressing_neighbor_naming.host_configuration import (
    HostIncorrectNetmask,
    HostIncorrectNetmaskParams,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "live,boot,mask,leased_ip,expected",
    [
        ("10.1.1.10/8", "10.1.1.10/24", None, None, True),
        ("10.1.1.10/8", None, "255.255.255.0", "10.1.1.10", True),
        ("10.1.1.10/8", None, "255.0.0.0", "10.1.1.10", False),
        ("10.1.1.10/8", "10.1.1.10/24", "255.0.0.0", "10.1.1.10", False),
        ("10.1.1.10/24", None, "255.255.255.0", "10.1.1.10", False),
        (None, None, "255.255.255.0", "10.1.1.10", False),
        ("10.1.1.10/8", None, "255.0.255.0", "10.1.1.10", False),
        ("10.1.1.11/8", None, "255.255.255.0", "10.1.1.10", False),
        ("10.1.1.10/8", None, "255.255.255.0", None, False),
    ],
)
def test_netmask_oracle_uses_the_intended_prefix(live, boot, mask, leased_ip, expected):
    problem = HostIncorrectNetmask("campus_lan", topo_size="s")
    check = (
        problem.oracle_playbook(
            HostIncorrectNetmaskParams(host_name="pc_1_1_1_1", netmask_prefix=8)
        )
        .steps[0]
        .expect
    )
    output = (f"inet {live}\n" if live else "") + "--- boot config\n"
    if boot:
        output += f"+ ip addr add {boot} dev eth0\n"
    if leased_ip:
        output += f"fixed-address {leased_ip};\n"
    if mask:
        output += f"option subnet-mask {mask};\n"
    assert check(output) is expected


@pytest.mark.parametrize("reverse", [False, True])
def test_netmask_oracle_ignores_telemetry_prefix(monkeypatch, reverse):
    from nika.problems.addressing_neighbor_naming import host_configuration

    monkeypatch.setattr(host_configuration, "segment_router", lambda *args: "router")
    monkeypatch.setattr(
        host_configuration,
        "router_cli",
        lambda *args, **kwargs: args[3],
    )
    problem = HostIncorrectNetmask("campus_lan", topo_size="s")
    check = problem.oracle_playbook(
        HostIncorrectNetmaskParams(host_name="pc_1_1_1_1", netmask_prefix=16)
    ).steps[-1]
    lines = ["2: eth0 inet 10.1.1.1/24", "3: eth1 inet 172.28.0.1/16"]
    assert check("\n".join(reversed(lines) if reverse else lines))
    assert not check("2: eth0 inet 10.1.1.1/16")
