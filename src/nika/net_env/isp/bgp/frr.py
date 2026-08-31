"""Render FRR BGP configuration fragments for ISP BGP presets."""

from __future__ import annotations

from nika.net_env.isp.bgp.plan import BgpNodePlan, BgpPlan


def render_bgp_frr_fragment(node: BgpNodePlan, plan: BgpPlan) -> str:
    """Return BGP + route-map stanzas for one router (appended after IGP)."""
    if plan.mode == "ibgp_rr":
        return _render_ibgp(node, plan)
    if plan.mode == "ebgp":
        return _render_ebgp(node, plan)
    return ""


def merge_frr_conf(igp_conf: str, bgp_fragment: str) -> str:
    """Append BGP fragment to an IGP frr.conf."""
    if not bgp_fragment.strip():
        return igp_conf
    base = igp_conf.rstrip() + "\n"
    return base + "\n" + bgp_fragment.lstrip()


def _common_prefix_lists(*, extra_business: tuple[str, ...] = ()) -> list[str]:
    lines = [
        "ip prefix-list BUSINESS seq 5 permit 203.0.113.0/24 le 24",
        "ip prefix-list BUSINESS seq 10 permit 203.0.114.0/24 le 24",
        "ip prefix-list BUSINESS seq 15 permit 203.0.115.0/24 le 24",
        "ip prefix-list BUSINESS seq 20 permit 198.51.100.0/24 le 24",
        "ip prefix-list BUSINESS seq 25 permit 198.51.101.0/24 le 24",
        "ip prefix-list BUSINESS seq 30 permit 198.51.102.0/24 le 24",
    ]
    seq = 35
    for prefix in extra_business:
        # Already covered by 203.0.113/114 entries when those are leak targets.
        if prefix in (
            "203.0.113.0/24",
            "203.0.114.0/24",
            "203.0.115.0/24",
            "198.51.100.0/24",
            "198.51.101.0/24",
            "198.51.102.0/24",
        ):
            continue
        lines.append(f"ip prefix-list BUSINESS seq {seq} permit {prefix} le 24")
        seq += 5
    lines.extend(
        [
            "ip prefix-list INFRA seq 5 permit 10.0.0.0/8 le 32",
            "ip prefix-list INFRA seq 10 permit 10.255.0.0/16 le 32",
            "!",
        ]
    )
    return lines


def _leak_prefix_list(prefixes: tuple[str, ...]) -> list[str]:
    if not prefixes:
        return []
    lines: list[str] = []
    seq = 5
    for prefix in prefixes:
        lines.append(f"ip prefix-list LEAK seq {seq} permit {prefix} le 24")
        seq += 5
    lines.append("!")
    return lines


def _route_maps(
    *,
    rov_reject_invalid: bool,
    export_deny_prefixes: tuple[str, ...],
    rtbh_import_policy: bool = False,
    blackhole_community: str | None = None,
    discard_next_hop: str | None = None,
) -> list[str]:
    lines: list[str] = []
    if export_deny_prefixes:
        lines.extend(
            [
                "route-map BGP-OUT deny 5",
                " match ip address prefix-list LEAK",
                "!",
            ]
        )
    lines.extend(
        [
            "route-map BGP-OUT permit 10",
            " match ip address prefix-list BUSINESS",
            "!",
            "route-map BGP-OUT deny 20",
            "!",
        ]
    )
    if rtbh_import_policy and blackhole_community and discard_next_hop:
        lines.extend(
            [
                f"bgp community-list standard BH-SIGNAL permit {blackhole_community}",
                "!",
                "route-map BGP-IN permit 5",
                " match ip address prefix-list BUSINESS",
                " match community BH-SIGNAL",
                " set local-preference 200",
                f" set ip next-hop {discard_next_hop}",
                "!",
            ]
        )
    if rov_reject_invalid:
        lines.extend(
            [
                "route-map BGP-IN deny 5",
                " match rpki invalid",
                "!",
            ]
        )
    lines.extend(
        [
            "route-map BGP-IN permit 10",
            " match ip address prefix-list BUSINESS",
            "!",
            "route-map BGP-IN deny 20",
            "!",
        ]
    )
    return lines


def _leaker_outbound_route_maps(
    outbound_maps: tuple[tuple[str, str], ...],
) -> list[str]:
    if not outbound_maps:
        return []
    names = sorted({name for _, name in outbound_maps})
    lines: list[str] = []
    for name in names:
        lines.extend(
            [
                f"route-map {name} permit 10",
                " match ip address prefix-list BUSINESS",
                "!",
                f"route-map {name} deny 20",
                "!",
            ]
        )
    return lines


def _rtbh_discard_static(discard_next_hop: str | None) -> list[str]:
    if not discard_next_hop:
        return []
    return [f"ip route {discard_next_hop}/32 Null0", "!"]


def _rpki_stanza(cache: tuple[str, int] | None) -> list[str]:
    if cache is None:
        return []
    ip, port = cache
    return [
        "rpki",
        # FRR 10+ requires an explicit transport (tcp|ssh).
        f" rpki cache tcp {ip} {port} preference 1",
        " exit",
        "!",
    ]


def _render_ibgp(node: BgpNodePlan, plan: BgpPlan) -> str:
    lines = ["!", "! ISP BGP (iBGP RR)", "!"]
    if node.originated:
        lines.extend(["interface lo"])
        for prefix in sorted(node.originated, key=lambda item: item.prefix):
            prefixlen = prefix.prefix.rsplit("/", 1)[1]
            lines.append(f" ip address {prefix.ping_address}/{prefixlen}")
        lines.append("!")
    lines.extend(_common_prefix_lists())
    lines.extend(_route_maps(rov_reject_invalid=False, export_deny_prefixes=()))
    lines.extend(
        [
            f"router bgp {node.asn}",
            f" bgp router-id {node.router_id}",
            " no bgp default ipv4-unicast",
            " no bgp network import-check",
        ]
    )
    if node.cluster_id:
        lines.append(f" bgp cluster-id {node.cluster_id}")
    for sess in sorted(node.sessions, key=lambda s: (s.remote_ip, s.remote_device)):
        lines.append(f" neighbor {sess.remote_ip} remote-as {sess.remote_asn}")
        if sess.update_source:
            lines.append(
                f" neighbor {sess.remote_ip} update-source {sess.update_source}"
            )
    lines.append(" !")
    lines.append(" address-family ipv4 unicast")
    for pref in sorted(node.originated, key=lambda p: p.prefix):
        lines.append(f"  network {pref.prefix}")
    for sess in sorted(node.sessions, key=lambda s: s.remote_ip):
        lines.append(f"  neighbor {sess.remote_ip} activate")
        if sess.route_reflector_client:
            lines.append(f"  neighbor {sess.remote_ip} route-reflector-client")
        lines.append(f"  neighbor {sess.remote_ip} route-map BGP-IN in")
        lines.append(f"  neighbor {sess.remote_ip} route-map BGP-OUT out")
    lines.append(" exit-address-family")
    lines.append("!")
    return "\n".join(lines) + "\n"


def _render_ebgp(node: BgpNodePlan, plan: BgpPlan) -> str:
    rpki_profile = bool(plan.inventory.get("rpki"))
    rtbh_profile = bool(plan.inventory.get("rtbh"))
    if rpki_profile:
        title = "eBGP + RPKI"
    elif rtbh_profile:
        title = "eBGP + RTBH"
    else:
        title = "eBGP"
    lines = ["!", f"! ISP BGP ({title})", "!"]
    if node.originated:
        lines.extend(["interface lo"])
        for prefix in sorted(node.originated, key=lambda item: item.prefix):
            prefixlen = prefix.prefix.rsplit("/", 1)[1]
            lines.append(f" ip address {prefix.ping_address}/{prefixlen}")
        lines.append("!")
    lines.extend(_rpki_stanza(node.rpki_cache))
    extra = tuple(plan.inventory.get("leak_prefixes") or ())
    lines.extend(_common_prefix_lists(extra_business=extra))
    lines.extend(_leak_prefix_list(node.export_deny_prefixes))
    discard_nh = str(plan.inventory.get("discard_next_hop") or "") or None
    community = str(plan.inventory.get("blackhole_community") or "") or None
    lines.extend(_rtbh_discard_static(discard_nh if node.rtbh_import_policy else None))
    lines.extend(_leaker_outbound_route_maps(node.ebgp_outbound_route_maps))
    lines.extend(
        _route_maps(
            rov_reject_invalid=node.rov_reject_invalid,
            export_deny_prefixes=node.export_deny_prefixes,
            rtbh_import_policy=node.rtbh_import_policy,
            blackhole_community=community,
            discard_next_hop=discard_nh,
        )
    )
    outbound_by_neighbor = dict(node.ebgp_outbound_route_maps)
    lines.extend(
        [
            f"router bgp {node.asn}",
            f" bgp router-id {node.router_id}",
            " no bgp default ipv4-unicast",
            " no bgp network import-check",
            # Explicit policies required (do not disable ebgp-requires-policy).
        ]
    )
    for sess in sorted(node.sessions, key=lambda s: (s.remote_ip, s.remote_device)):
        lines.append(f" neighbor {sess.remote_ip} remote-as {sess.remote_asn}")
        if sess.update_source:
            lines.append(
                f" neighbor {sess.remote_ip} update-source {sess.update_source}"
            )
    lines.append(" !")
    lines.append(" address-family ipv4 unicast")
    for pref in sorted(node.originated, key=lambda p: p.prefix):
        lines.append(f"  network {pref.prefix}")
    for sess in sorted(node.sessions, key=lambda s: s.remote_ip):
        lines.append(f"  neighbor {sess.remote_ip} activate")
        if sess.session_type == "ebgp":
            lines.append(f"  neighbor {sess.remote_ip} next-hop-self")
            lines.append(f"  neighbor {sess.remote_ip} route-map BGP-IN in")
            out_map = outbound_by_neighbor.get(sess.remote_ip, "BGP-OUT")
            lines.append(f"  neighbor {sess.remote_ip} route-map {out_map} out")
        else:
            # Intra-AS iBGP: flood without LEAK export deny so borders can
            # re-advertise once eBGP BGP-OUT permits the leak prefixes.
            lines.append(f"  neighbor {sess.remote_ip} next-hop-self")
            if sess.route_reflector_client:
                lines.append(f"  neighbor {sess.remote_ip} route-reflector-client")
            lines.append(f"  neighbor {sess.remote_ip} route-map BGP-IN in")
    lines.append(" exit-address-family")
    lines.append("!")
    return "\n".join(lines) + "\n"
