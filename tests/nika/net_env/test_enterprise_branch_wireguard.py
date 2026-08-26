"""Unit tests for enterprise_branch WireGuard helpers and peer-key targets."""

from __future__ import annotations

from nika.net_env.enterprise_branch.topology import (
    SCALE,
    build_topo_spec,
    primary_hq_peer_targets,
    remote_advertised_prefixes_for_spoke,
    single_path_hq_peer_targets,
)
from nika.net_env.enterprise_branch.wireguard import (
    load_key_pairs,
)
from nika.problems.forwarding_encapsulation_policy.wireguard import (
    WRONG_HUB_PEER_PUBLIC_KEY,
    allowed_ips_for_spoke_hub_peer,
)


def test_wrong_hub_peer_key_is_not_a_lab_edge_key() -> None:
    used = {pub for _, pub in load_key_pairs()[:10]}
    assert WRONG_HUB_PEER_PUBLIC_KEY not in used


def test_scale_diffs_roles_and_counts() -> None:
    specs = {size: build_topo_spec(size) for size in ("s", "m", "l")}
    for size, spec in specs.items():
        assert spec.providers == ("isp1", "isp2")
        assert set(spec.sites) >= {"hq", "dc2"}
        assert spec.sites["hq"].lans[0].role == "corp"
        hq_roles = {lan.role for lan in spec.sites["hq"].lans}
        dc2_roles = {lan.role for lan in spec.sites["dc2"].lans}
        expected_hub = set(SCALE[size].hub_roles)
        expected_branch = set(SCALE[size].branch_roles)
        assert hq_roles == expected_hub
        assert dc2_roles == expected_hub
        if size == "s":
            assert "iot" not in hq_roles
        else:
            assert "iot" in hq_roles
        assert spec.sites["hq"].wan_providers == ("isp1", "isp2")
        assert SCALE[size].branches == len(spec.branch_names())
        assert spec.hosts_per_lan == SCALE[size].hosts_per_lan
        for br in spec.branch_names():
            roles = {lan.role for lan in spec.sites[br].lans}
            assert roles == expected_branch
            assert spec.sites[br].wan_providers == ("isp1", "isp2")
            corp = next(lan for lan in spec.sites[br].lans if lan.role == "corp")
            assert len(corp.host_names) == SCALE[size].hosts_per_lan
            for lan in spec.sites[br].lans:
                assert lan.vrf == f"vrf_{lan.role}"
        # Same tunnel kinds per branch + hub interconnect.
        branch_tunnels = [
            t for t in spec.tunnels if t.local_site in spec.branch_names()
        ]
        assert len(branch_tunnels) == SCALE[size].branches * 3
        hub_ic = [
            t for t in spec.tunnels if t.local_site == "hq" and t.remote_site == "dc2"
        ]
        assert len(hub_ic) == 2


def test_topo_build_is_deterministic() -> None:
    for size in ("s", "m", "l"):
        a = build_topo_spec(size)
        b = build_topo_spec(size)
        assert a == b
        assert a.advertised_prefixes() == b.advertised_prefixes()
        assert a.local_only_prefixes() == b.local_only_prefixes()


def test_primary_hq_peer_targets_by_size() -> None:
    assert primary_hq_peer_targets("s") == [
        ("br1_edge", "wg_hq"),
        ("br2_edge", "wg_hq"),
    ]
    assert primary_hq_peer_targets("m") == [
        ("br1_edge", "wg_hq"),
        ("br2_edge", "wg_hq"),
        ("br3_edge", "wg_hq"),
        ("br4_edge", "wg_hq"),
    ]
    assert len(primary_hq_peer_targets("l")) == 8
    # Alias: no single-path spokes under full redundancy.
    assert single_path_hq_peer_targets("s") == primary_hq_peer_targets("s")


def test_allowed_ips_omits_one_remote_prefix() -> None:
    advertised = [
        "10.0.10.0/24",
        "10.0.20.0/24",
        "10.1.10.0/24",
        "10.2.10.0/24",
    ]
    local = ["10.1.10.0/24"]
    full = allowed_ips_for_spoke_hub_peer(
        advertised_prefixes=advertised,
        local_prefixes=local,
        hub_tunnel_ip="172.30.0.1",
    )
    assert full == ("172.30.0.1/32, 10.0.10.0/24, 10.0.20.0/24, 10.2.10.0/24")
    omitted = allowed_ips_for_spoke_hub_peer(
        advertised_prefixes=advertised,
        local_prefixes=local,
        hub_tunnel_ip="172.30.0.1",
        omit="10.0.20.0/24",
    )
    assert omitted == "172.30.0.1/32, 10.0.10.0/24, 10.2.10.0/24"
    assert "10.0.20.0/24" not in omitted
    assert "172.30.0.1/32" in omitted


def test_remote_advertised_prefixes_for_spoke() -> None:
    remotes = remote_advertised_prefixes_for_spoke("s", "br1")
    assert "10.0.20.0/24" in remotes
    assert "10.0.10.0/24" in remotes
    assert "10.10.10.0/24" in remotes  # dc2 corp
    assert "10.10.20.0/24" in remotes  # dc2 server
    assert "10.2.10.0/24" in remotes
    assert "10.1.10.0/24" not in remotes
    assert "10.0.40.0/24" not in remotes  # guest not advertised


def test_local_only_prefixes_exclude_overlay() -> None:
    spec_s = build_topo_spec("s")
    assert "10.0.40.0/24" in spec_s.local_only_prefixes()
    assert "10.0.10.0/24" not in spec_s.local_only_prefixes()
    spec_m = build_topo_spec("m")
    assert "10.0.30.0/24" in spec_m.local_only_prefixes()  # hq iot
    assert all(
        p not in spec_m.advertised_prefixes() for p in spec_m.local_only_prefixes()
    )


def test_edge_frr_vrf_import_stable() -> None:
    from nika.net_env.enterprise_branch.lab import (
        _render_edge_frr,
    )

    tunnels = {
        "wg_hq": (
            "172.30.0.2/30",
            "172.30.0.1",
            51820,
            "hq",
            True,
            200,
            "100.64.0.1",
        ),
    }
    kwargs = dict(
        hostname="br1_edge",
        asn=65001,
        router_id="100.64.0.17",
        is_hub=False,
        overlay_roles=("corp",),
        local_adv_prefixes=["10.1.10.0/24"],
        tunnels=tunnels,
        peer_asns={"hq": 65000, "br1": 65001},
    )
    a = _render_edge_frr(**kwargs)
    b = _render_edge_frr(**kwargs)
    assert a == b
    assert "router bgp 65001 vrf vrf_corp" in a
    assert "import vrf vrf_corp" in a
    assert "import vrf default" in a
    assert "vrf vrf_guest" not in a
