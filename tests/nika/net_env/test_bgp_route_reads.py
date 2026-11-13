"""Native BGP parsers consume outputs captured from NIKA vendor labs."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from nika.net_env.vendor_validation import (
    _parse_iosxr_bgp_summary,
    iosxr_bgp_prefix_paths,
    iosxr_bgp_table_prefixes,
    routeros_bgp_table,
)

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).with_name("fixtures") / "bgp_routes"


def test_captured_routeros_ebgp_table():
    table = routeros_bgp_table((FIXTURES / "routeros-ebgp-detail.txt").read_text())
    assert set(table) == {"172.31.255.28/30", "193.10.11.0/24", "200.1.1.0/24"}
    assert table["200.1.1.0/24"] == [
        {
            "aspath": "2",
            "best": True,
            "nexthops": ["193.10.11.2"],
            "originator": None,
            "peer_router_id": None,
        }
    ]


def test_captured_xr_table_and_local_asn():
    assert iosxr_bgp_table_prefixes((FIXTURES / "iosxr-table.txt").read_text()) == [
        "195.11.14.0/24",
        "200.1.1.0/24",
    ]
    asn, peers = _parse_iosxr_bgp_summary((FIXTURES / "iosxr-summary.txt").read_text())
    assert asn == 1
    assert peers["193.10.11.2"].established


def test_captured_xr_absence_and_command_failure_are_distinct():
    assert (
        iosxr_bgp_prefix_paths(
            (FIXTURES / "iosxr-absent.txt").read_text(), "192.168.3.0/24"
        )
        == []
    )
    with pytest.raises(ValueError):
        iosxr_bgp_prefix_paths(
            (FIXTURES / "iosxr-invalid-command.txt").read_text(), "192.168.3.0/24"
        )


@pytest.mark.parametrize("parser", [routeros_bgp_table, iosxr_bgp_table_prefixes])
@pytest.mark.parametrize(
    "output",
    [
        "",
        "[TIMEOUT]",
        "Flags: A - active\n[TIMEOUT]",
        "unrecognized output",
        "failure: command failed",
    ],
)
def test_unknown_table_output_is_not_an_empty_table(parser, output):
    with pytest.raises(ValueError):
        parser(output)


def test_captured_xr_ibgp_path_ignores_update_group_numbers():
    paths = iosxr_bgp_prefix_paths(
        (FIXTURES / "iosxr-ibgp-path.txt").read_text(), "198.18.0.0/24"
    )
    assert paths == [
        {
            "aspath": "",
            "best": True,
            "nexthops": ["10.255.0.1"],
            "originator": None,
            "peer_router_id": "10.255.0.1",
        }
    ]


def test_captured_xr_reflected_paths_keep_unselected_path_and_originator():
    paths = iosxr_bgp_prefix_paths(
        (FIXTURES / "iosxr-reflected-paths.txt").read_text(), "203.0.113.0/24"
    )
    assert len(paths) == 2
    assert [path["best"] for path in paths] == [True, False]
    assert all(path["aspath"] == "" for path in paths)
    assert all(path["nexthops"] == ["10.255.0.8"] for path in paths)
    assert all(path["originator"] == "10.255.0.8" for path in paths)
    assert all(path["peer_router_id"] is None for path in paths)


def test_captured_routeros_ibgp_omits_empty_as_path():
    table = routeros_bgp_table((FIXTURES / "routeros-ibgp-path.txt").read_text())
    assert table == {
        "198.18.0.0/24": [
            {
                "aspath": "",
                "best": True,
                "nexthops": ["10.255.0.1"],
                "originator": None,
                "peer_router_id": None,
            }
        ]
    }


def test_captured_routeros_absent_route_is_readable():
    assert routeros_bgp_table((FIXTURES / "routeros-absent.txt").read_text()) == {}


@pytest.mark.parametrize(
    ("field", "message"),
    [("routing-table", "routing table"), ("bgp.session", "path attributes")],
)
def test_truncated_routeros_ibgp_route_is_unreadable(field, message):
    output = (FIXTURES / "routeros-ibgp-path.txt").read_text().split(field, 1)[0]
    with pytest.raises(ValueError, match=message):
        routeros_bgp_table(output)


def test_captured_routeros_reflected_paths_keep_inactive_candidate():
    paths = routeros_bgp_table((FIXTURES / "routeros-reflected-paths.txt").read_text())[
        "203.0.113.0/24"
    ]
    assert len(paths) == 2
    assert [path["best"] for path in paths] == [False, True]
    assert [path["originator"] for path in paths] == ["10.255.0.2", "10.255.0.1"]
    assert all(path["aspath"] == "" for path in paths)
    assert all(path["nexthops"] == ["10.255.0.8"] for path in paths)


@pytest.mark.parametrize("profile", ["iosxr", "routeros"])
def test_route_presence_rejects_unrecognized_output(profile):
    from nika.net_env.native_routers import native_platform

    ops = native_platform(profile).fault_ops(
        SimpleNamespace(exec=lambda *a, **k: "unrecognized output"), "r1"
    )
    with pytest.raises(ValueError):
        ops.has_bgp_route("198.18.0.0/24")


@pytest.mark.parametrize("profile", ["iosxr", "routeros"])
def test_route_presence_reads_captured_available_paths(profile):
    from nika.net_env.native_routers import native_platform

    output = (FIXTURES / f"{profile}-ibgp-path.txt").read_text()
    ops = native_platform(profile).fault_ops(
        SimpleNamespace(exec=lambda *a, **k: output), "r1"
    )
    assert ops.has_bgp_route("198.18.0.0/24")


@pytest.mark.parametrize(
    "rib",
    [
        {},
        {"afi-safi": []},
        {"afi-safi": [{"afi-safi-name": "ipv6-unicast"}]},
        {"afi-safi": [{"afi-safi-name": "ipv4-unicast"}]},
        {"afi-safi": [{"afi-safi-name": "ipv4-unicast", "ipv4-unicast": {}}]},
        {
            "afi-safi": [
                {"afi-safi-name": "ipv4-unicast", "ipv4-unicast": {"local-rib": {}}}
            ]
        },
    ],
)
def test_srl_empty_ribs_are_readable_in_both_consumers(rib):
    import json

    from nika.net_env.srl_validation import selected_as_paths
    from nika.service.containerlab.srl_api import srl_bgp_prefix_paths

    data = {"network-instance": [{"name": "default", "bgp-rib": rib}]}
    assert srl_bgp_prefix_paths(json.dumps(data), "192.0.2.0/24") == []
    assert selected_as_paths(data, "192.0.2.0/24") == []


@pytest.mark.parametrize(
    "output", ["", "[TIMEOUT]", "{}", '{"network-instance": [{"name": "default"}]}']
)
def test_srl_unreadable_rib_is_not_an_empty_rib(output):
    from nika.service.containerlab.srl_api import srl_bgp_prefix_paths

    with pytest.raises((ValueError, KeyError)):
        srl_bgp_prefix_paths(output, "192.0.2.0/24")
