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


def _common_prefix_lists() -> list[str]:
    return [
        "ip prefix-list BUSINESS seq 5 permit 203.0.113.0/24 le 24",
        "ip prefix-list BUSINESS seq 10 permit 203.0.114.0/24 le 24",
        "ip prefix-list BUSINESS seq 15 permit 203.0.115.0/24 le 24",
        "ip prefix-list BUSINESS seq 20 permit 198.51.100.0/24 le 24",
        "ip prefix-list BUSINESS seq 25 permit 198.51.101.0/24 le 24",
        "ip prefix-list BUSINESS seq 30 permit 198.51.102.0/24 le 24",
        "ip prefix-list INFRA seq 5 permit 10.0.0.0/8 le 32",
        "ip prefix-list INFRA seq 10 permit 10.255.0.0/16 le 32",
        "!",
        "route-map BGP-OUT permit 10",
        " match ip address prefix-list BUSINESS",
        "!",
        "route-map BGP-OUT deny 20",
        "!",
        "route-map BGP-IN permit 10",
        " match ip address prefix-list BUSINESS",
        "!",
        "route-map BGP-IN deny 20",
        "!",
    ]


def _render_ibgp(node: BgpNodePlan, plan: BgpPlan) -> str:
    lines = ["!", "! ISP BGP (iBGP RR)", "!"]
    lines.extend(_common_prefix_lists())
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
    lines = ["!", "! ISP BGP (eBGP)", "!"]
    lines.extend(_common_prefix_lists())
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
    lines.append(" !")
    lines.append(" address-family ipv4 unicast")
    for pref in sorted(node.originated, key=lambda p: p.prefix):
        lines.append(f"  network {pref.prefix}")
    for sess in sorted(node.sessions, key=lambda s: s.remote_ip):
        lines.append(f"  neighbor {sess.remote_ip} activate")
        lines.append(f"  neighbor {sess.remote_ip} next-hop-self")
        lines.append(f"  neighbor {sess.remote_ip} route-map BGP-IN in")
        lines.append(f"  neighbor {sess.remote_ip} route-map BGP-OUT out")
    lines.append(" exit-address-family")
    lines.append("!")
    return "\n".join(lines) + "\n"
