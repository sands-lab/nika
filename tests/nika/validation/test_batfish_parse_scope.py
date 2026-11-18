"""Batfish analysis rejects ignored routing statements and normalizes syntax."""

import pytest

from nika.validation.batfish.client import unaccepted_parse_warnings
from nika.validation.batfish.snapshot import normalize_frr_for_batfish
from nika.validation.batfish.vendor_bgp import iosxr_batfish_config

pytestmark = pytest.mark.unit


def test_only_routing_neutral_parse_warnings_are_accepted():
    warnings = [
        {"Text": "no shutdown", "Comment": "This syntax is unrecognized"},
        {"Text": "domain ipv4 host r2 10.0.0.1", "Comment": "x"},
        {
            "Text": " neighbor 10.0.0.1\n",
            "Comment": "Duplicate IP peer group in neighbor config.",
        },
        {
            "Text": "neighbor 10.0.0.1 maximum-prefix 10",
            "Comment": "This syntax is unrecognized",
        },
    ]
    assert unaccepted_parse_warnings(warnings) == [warnings[-1]]


def test_iosxr_one_line_static_routes_and_circuit_type_are_normalized():
    config = (
        "router static\n"
        "  address-family ipv4 unicast 198.18.1.10/32 GigabitEthernet0/0/0/2 100.64.0.17\n"
        "!\nrouter isis CORE\n interface GigabitEthernet0/0/0/0\n  circuit-type level-2\n"
    )
    normalized = iosxr_batfish_config(config)
    assert (
        "  address-family ipv4 unicast\n"
        "   198.18.1.10/32 GigabitEthernet0/0/0/2 100.64.0.17\n"
    ) in normalized
    assert "  circuit-type level-2-only\n" in normalized


def test_frr_bare_ipv4_address_family_is_normalized():
    config = (
        "router bgp 65001\n address-family ipv4\n  neighbor 10.0.0.2 route-map IN in\n"
    )
    assert " address-family ipv4 unicast\n" in normalize_frr_for_batfish(config)
