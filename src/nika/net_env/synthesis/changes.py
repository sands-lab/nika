"""Design changes that negative controls apply before netlab renders a candidate.

A counterexample or a modeled fault states its change on the routing design.
netlab renders the changed design with its own templates, so the static model
of the negative control needs no edits to generated configuration text.

``discard_route`` changes the netlab input topology. The other kinds change
netlab's transformed model of the design, which also expresses settings that
disagree between two peers, such as a wrong neighbor ASN.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Interface, IPv4Network
from typing import Literal

from pydantic import field_validator, model_validator

from nika.net_env.contract import ContractModel

# Fields each kind of change requires.
_REQUIRED = {
    "peer_asn": ("peer", "asn"),
    "local_asn": ("asn",),
    "deny_export": ("prefix",),
    "withdraw_origin": ("prefix",),
    "interface_area": ("prefix",),
    "rr_client": ("peer",),
    "discard_route": ("prefix",),
}


class DesignChange(ContractModel):
    """One change to a router's routing design.

    ``peer_asn`` sets the ASN a router expects from a neighbor; ``local_asn``
    sets the router's own BGP ASN. ``deny_export`` denies the entries that
    export ``prefix`` in the outbound policies of all neighbors, or of
    ``peer`` when given, which also affects other neighbors that share its
    policy. ``withdraw_origin`` stops originating ``prefix``.
    ``interface_area`` moves the interfaces addressed inside ``prefix`` to
    ``area``, or out of the IGP when ``area`` is None. ``rr_client`` removes
    the route-reflector client role of a neighbor. ``discard_route`` adds a
    static route that discards ``prefix``.
    """

    kind: Literal[
        "peer_asn",
        "local_asn",
        "deny_export",
        "withdraw_origin",
        "interface_area",
        "rr_client",
        "discard_route",
    ]
    node: str
    peer: str | None = None
    prefix: str | None = None
    asn: int | None = None
    area: str | None = None

    @model_validator(mode="after")
    def _fields(self):
        missing = [f for f in _REQUIRED[self.kind] if getattr(self, f) is None]
        if missing:
            raise ValueError(f"{self.kind} change requires {', '.join(missing)}")
        if self.peer is not None:
            IPv4Address(self.peer)
        if self.prefix is not None:
            IPv4Network(self.prefix)
        return self

    @field_validator("area", mode="before")
    @classmethod
    def _dotted_area(cls, value):
        if value is None:
            return None
        text = str(value)
        return str(IPv4Address(int(text) if "." not in text else text))


def apply_input_changes(topology: dict, changes) -> dict:
    """Apply the changes that netlab's input topology expresses."""
    for change in changes:
        if change.kind != "discard_route":
            continue
        node = topology["nodes"][change.node]
        modules = list(node.get("module", topology.get("module", [])))
        if "routing" not in modules:
            node["module"] = [*modules, "routing"]
        node.setdefault("routing.static", []).append(
            {"ipv4": change.prefix, "nexthop": {"discard": True}}
        )
    return topology


def _interfaces(node):
    yield from node.get("interfaces", [])
    if node.get("loopback"):
        yield node["loopback"]


def _prefixes(value) -> list[str]:
    # netlab lists the prefixes of a transformed prefix-list entry.
    return value if isinstance(value, list) else [value]


def _one(found, change):
    if not found:
        raise ValueError(f"{change.kind} change matches nothing on {change.node}")
    return found


def apply_model_changes(model, changes) -> None:
    """Apply the other changes to netlab's transformed model in place."""
    for change in changes:
        if change.kind == "discard_route":
            continue
        node = model["nodes"][change.node]
        bgp = node.get("bgp", {})
        neighbors = [
            n for n in bgp.get("neighbors", []) if n.get("ipv4") == change.peer
        ]
        if change.kind == "peer_asn":
            for neighbor in _one(neighbors, change):
                neighbor["as"] = change.asn
        elif change.kind == "local_asn":
            bgp["as"] = change.asn
        elif change.kind == "rr_client":
            for neighbor in _one(neighbors, change):
                neighbor.pop("rr_client", None)
        elif change.kind == "withdraw_origin":
            network = IPv4Network(change.prefix)
            advertised = [
                a for a in bgp.get("advertise", []) if IPv4Network(a["ipv4"]) == network
            ]
            for entry in advertised:
                bgp["advertise"].remove(entry)
            interfaces = [
                i
                for i in _interfaces(node)
                if i.get("bgp", {}).get("advertise")
                and IPv4Interface(i["ipv4"]).network == network
            ]
            for interface in interfaces:
                interface["bgp"]["advertise"] = False
            _one(advertised + interfaces, change)
        elif change.kind == "deny_export":
            routing = node.get("routing", {})
            lists = {
                name
                for name, entries in routing.get("prefix", {}).items()
                if any(change.prefix in _prefixes(e.get("ipv4")) for e in entries)
            }
            policies = {
                n["policy"]["out"]
                for n in bgp.get("neighbors", [])
                if n.get("policy", {}).get("out")
                and change.peer in (None, n.get("ipv4"))
            }
            entries = [
                entry
                for name in policies
                for entry in routing.get("policy", {}).get(name, [])
                if entry.get("action") == "permit"
                and entry.get("match", {}).get("prefix") in lists
            ]
            for entry in _one(entries, change):
                entry["action"] = "deny"
        elif change.kind == "interface_area":
            network = IPv4Network(change.prefix)
            selected = _one(
                [
                    i
                    for i in _interfaces(node)
                    if "ipv4" in i
                    and isinstance(i["ipv4"], str)
                    and IPv4Interface(i["ipv4"]).ip in network
                    and ("ospf" in i or "isis" in i)
                ],
                change,
            )
            for interface in selected:
                if change.area is None:
                    interface.pop("ospf", None)
                    interface.pop("isis", None)
                elif "ospf" in interface:
                    interface["ospf"]["area"] = change.area
                else:
                    raise ValueError(
                        f"interface_area change sets an OSPF area on {change.node}, "
                        "whose interface does not run OSPF"
                    )
