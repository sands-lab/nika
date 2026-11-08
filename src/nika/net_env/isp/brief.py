"""Shared agent-facing ISP network brief (protocol metadata).

Live troubleshooting benchmarks typically give agents a short operator brief:
description, role inventory, topology edges, and protocol mode — not device
configs or ground truth. Config and runtime state stay on the tool path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from nika.net_env.probe_intent import ProbeIntent

if TYPE_CHECKING:
    from nika.net_env.isp.bgp.plan import BgpPlan
    from nika.net_env.isp.igp.plan import IspPlan
    from nika.net_env.isp.traffic.stubs import IspTrafficAttachment

EBGP_SCOPE_NOTE = (
    " Between ASes, BGP carries only business prefixes, and each AS gives the "
    "others transit; edge-stub customer subnets are internal to their own AS "
    "and are not exported."
)


def ebgp_scope_note(bgp_plan: "BgpPlan | None") -> str:
    """Design-intent sentence for eBGP ISPs, empty for other modes."""
    return EBGP_SCOPE_NOTE if bgp_plan is not None and bgp_plan.mode == "ebgp" else ""


def stub_probe_intent(
    bgp_plan: "BgpPlan | None", traffic: "IspTrafficAttachment"
) -> ProbeIntent | None:
    """Under eBGP, edge stubs only reach stubs inside their own AS."""
    if bgp_plan is None or bgp_plan.mode != "ebgp":
        return None
    asn_of = {node.device_name: node.asn for node in bgp_plan.nodes}
    by_asn: dict[int, list[str]] = {}
    for host in traffic.hosts:
        by_asn.setdefault(asn_of[host.router_device], []).append(host.host_name)
    site = bgp_plan.customer_site
    if site is not None:
        # Only its provider's AS routes back to the customer site's stubs.
        by_asn.setdefault(asn_of[site.provider], []).append(site.host_name)
    return ProbeIntent.from_partitions(by_asn.values())


def format_isp_network_info(
    base_info: str,
    *,
    inventory: Mapping,
    bgp_mode: str,
    device_profile: str | None = None,
) -> str:
    """Append SNDlib / protocol metadata to a role + topology inventory brief."""
    lines = [
        base_info.rstrip("\n"),
        f"SNDlib topology: {inventory['topology_name']}",
        (
            f"IGP: {inventory['igp']}; metric_strategy: {inventory['metric_strategy']}; "
            f"constant_metric: {inventory['constant_metric']}"
        ),
        f"BGP mode: {bgp_mode}",
    ]
    if device_profile is not None:
        lines.append(f"device_profile: {device_profile}")
    host_n = len(inventory.get("hosts") or [])
    lines.append(f"Edge stubs: {host_n}")
    lines.append(
        f"Inventory nodes: {inventory['node_count']}; links: {inventory['link_count']}"
    )
    return "\n".join(lines)


def design_notes(
    plan: "IspPlan", bgp_plan: "BgpPlan | None", *, router_services: bool
) -> str:
    """Design sentences for the scenario brief: tunnelled links and user traffic."""
    from nika.net_env.isp.igp.plan import tunnel_links

    notes = [
        f"{a} reaches {b} over a GRE tunnel (IP MTU {mtu})."
        for a, b, mtu in tunnel_links(plan)
    ]
    scope = (
        " inside its own AS" if bgp_plan is not None and bgp_plan.mode == "ebgp" else ""
    )
    if bgp_plan is not None and router_services:
        service = "the web services on the business prefixes"
    elif bgp_plan is not None:
        service = "the web services on the originators' stubs"
    else:
        service = "a web service at the busiest PoP"
    notes.append(
        "Customers at every PoP generate traffic at all times: each edge stub "
        "publishes a data set over HTTP (port 8080) and syncs the data set of "
        f"the PoP it has the largest SNDlib demand with{scope}, and some stubs "
        f"keep persistent sessions to {service}."
    )
    site = bgp_plan.customer_site if bgp_plan is not None else None
    if site is not None:
        asn_of = {node.device_name: node.asn for node in bgp_plan.nodes}
        notes.append(
            f"{site.host_name} is a customer site single-homed to "
            f"{site.provider} (AS {asn_of[site.provider]}) with prefix "
            f"{site.prefix}; it keeps sessions to the web service of "
            f"AS {site.partner_asn} at {site.service_address}. At the "
            f"customer's request, AS {asn_of[site.provider]} announces "
            f"{site.prefix} only to AS {site.partner_asn}, which keeps it "
            "internal."
        )
    return " " + " ".join(notes)
