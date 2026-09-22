"""Unit tests for IOS-XR BGP API helpers (no Docker)."""

from __future__ import annotations

from nika.service.lab.iosxr_api import (
    IOSXRAPIMixin,
    _rewrite_router_bgp_asn,
)


class _FakeExec(IOSXRAPIMixin):
    def __init__(self, outputs: dict[str, str]) -> None:
        self.outputs = outputs
        self.commands: list[str] = []

    def exec_cmd(self, host_name: str, command: str, timeout: float = 10) -> str:
        self.commands.append(command)
        for key, value in self.outputs.items():
            if key in command:
                return value
        return ""


def test_rewrite_router_bgp_asn() -> None:
    conf = "\n".join(
        [
            "router bgp 1",
            " bgp router-id 193.10.11.1",
            " address-family ipv4 unicast",
            "  network 195.11.14.0/24",
            " !",
            "!",
            "end",
        ]
    )
    lines = _rewrite_router_bgp_asn(conf, 1, 601)
    assert lines[0] == "router bgp 601"
    assert any("network 195.11.14.0/24" in line for line in lines)


def test_iosxr_get_bgp_asn_from_summary() -> None:
    api = _FakeExec(
        {
            "show bgp summary": "BGP router identifier 1.1.1.1, local AS number 42\n",
        }
    )
    assert api.iosxr_get_bgp_asn_number("r1") == 42


def test_iosxr_list_bgp_networks() -> None:
    api = _FakeExec(
        {
            "show running-config router bgp": (
                "router bgp 1\n"
                " address-family ipv4 unicast\n"
                "  network 195.11.14.0/24\n"
                "  network 10.0.0.0/8\n"
            ),
        }
    )
    assert api.iosxr_list_bgp_networks("r1") == ["195.11.14.0/24", "10.0.0.0/8"]
