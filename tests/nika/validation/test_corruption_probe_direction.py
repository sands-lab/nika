"""The corruption probe sends data out of the corrupted port, through tunnels too."""

import pytest

from nika.validation.symptom.corruption_probes import _egress_device

pytestmark = pytest.mark.unit


class _Runtime:
    def __init__(self, outputs: dict[str, str]):
        self.outputs = outputs

    def exec(self, host, command, timeout=10.0):
        return self.outputs.get(command, "")


def test_route_through_wireguard_leaves_by_the_underlay():
    runtime = _Runtime(
        {
            "ip -4 route get 10.0.20.2": "10.0.20.2 via 172.30.0.9 dev wg_hq src 172.30.0.10",
            "ip -d link show dev wg_hq": "5: wg_hq: <POINTOPOINT>\n    wireguard addrgenmode none",
            "wg show wg_hq endpoints": "09xC+mCmgSy=\t100.64.0.1:51822",
            "ip -4 route get 100.64.0.1": "100.64.0.1 via 100.64.0.18 dev eth3",
            "ip -d link show dev eth3": "7: eth3: <UP>\n    veth",
        }
    )
    assert _egress_device(runtime, "br1_edge", "10.0.20.2") == "eth3"


def test_plain_route_keeps_its_interface():
    runtime = _Runtime(
        {
            "ip -4 route get 10.0.2.11": "10.0.2.11 dev eth1 src 10.0.2.1",
            "ip -d link show dev eth1": "3: eth1: <UP>\n    veth",
        }
    )
    assert _egress_device(runtime, "leaf", "10.0.2.11") == "eth1"
