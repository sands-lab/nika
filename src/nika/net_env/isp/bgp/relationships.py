"""Provider and peer export contracts for three-AS ISP interconnects."""

from dataclasses import replace

from nika.net_env.isp.bgp.config import EBGP_BASE_ASN


def with_inter_as_roles(plan):
    """Give a multihomed customer two peering providers when all three meet.

    The ASN partition is the existing RPKI/RTBH partition. Graphs without a
    provider interconnect retain their transit topology.
    """
    if not plan.inventory.get("rpki"):
        return plan
    origin, customer, provider = range(EBGP_BASE_ASN, EBGP_BASE_ASN + 3)
    edges = {
        frozenset((s.local_asn, s.remote_asn))
        for s in plan.sessions
        if s.session_type == "ebgp"
    }
    if not all(
        frozenset(pair) in edges
        for pair in ((origin, customer), (customer, provider), (origin, provider))
    ):
        return plan
    roles = []
    for session in plan.sessions:
        if session.session_type != "ebgp":
            continue
        role = (
            "provider"
            if session.local_asn == customer
            else "customer"
            if session.remote_asn == customer
            else "peer"
        )
        roles.append(
            {
                "node": session.local_device,
                "neighbor": session.remote_ip,
                "remote_node": session.remote_device,
                "remote_asn": session.remote_asn,
                "role": role,
            }
        )
    return replace(plan, inventory={**plan.inventory, "inter_as_roles": roles})


def transit_export_map(neighbor: str) -> str:
    return "TRANSIT-OUT-" + neighbor.replace(".", "-")
