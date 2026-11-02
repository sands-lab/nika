"""Prefix-length filters must retain their import binding and match semantics."""

import pytest

from nika.problems.routing_control_plane.bgp_prefix import import_prefix_ranges
from nika.net_env.utils.routeros.parse import routeros_filter_rules

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("quoted", [False, True])
def test_routeros_filter_keeps_unquoted_expression(quoted):
    rule = "if (dst in 10.0.0.0/8 && dst-len <= 24) { set bgp-local-pref 200; accept; }"
    value = f'"{rule}"' if quoted else rule
    rows = routeros_filter_rules(f"2 chain=POLICY-IN-wg_hq rule={value}\r\n")
    assert rows[0]["chain"] == "POLICY-IN-wg_hq"
    assert rows[0]["rule"] == rule


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("10.0.0.0/8 le 24", {"BUSINESS": ("10.0.0.0/8", 24)}),
        ("10.0.0.0/8", {"BUSINESS": ("10.0.0.0/8", 8)}),
        ("10.1.0.0/24 le 24", {"BUSINESS": ("10.1.0.0/24", 24)}),
        ("10.0.0.0/8 ge 16 le 24", {}),
    ],
)
def test_bound_import_filter_lengths(entry, expected):
    config = (
        f"ip prefix-list BUSINESS seq 5 permit {entry}\n!\n"
        "route-map IMPORT permit 10\n match ip address prefix-list BUSINESS\n!\n"
        "router bgp 65000\n neighbor 192.0.2.1 route-map IMPORT in\n"
    )
    assert import_prefix_ranges(config) == expected
    assert import_prefix_ranges(config.replace("IMPORT in", "IMPORT out")) == {}
    assert (
        import_prefix_ranges(
            config + "ip prefix-list BUSINESS seq 10 permit 172.16.0.0/12 le 24\n"
        )
        == {}
    )
