"""Concrete FRR IPv4 iBGP/RR subset projected to IOS XR for Batfish."""

from __future__ import annotations

import re
from ipaddress import IPv4Address, IPv4Interface, IPv4Network

from nika.net_env.utils.kathara.frr_origin import BGP_ORIGIN_IFACE


def split_frr_ibgp(config: str) -> tuple[str, str]:
    """Separate concrete protocol blocks, including interleaved tool fragments."""
    igp, bgp = [], []
    target = igp
    for line in config.splitlines(keepends=True):
        if line and not line[0].isspace() and not line.startswith(("!", "#")):
            if line.startswith(("interface ", "router ")):
                target = (
                    bgp
                    if (
                        line.strip() == f"interface {BGP_ORIGIN_IFACE}"
                        or line.startswith("router bgp ")
                    )
                    else igp
                )
            elif line.startswith(("ip prefix-list ", "route-map ")):
                target = bgp
            elif line.startswith(("hostname ", "log file ", "frr ", "ip forwarding")):
                target = igp
        target.append(line)
    return "".join(igp), "".join(bgp)


def project_frr_ibgp(fragment: str) -> str:
    """Preserve concrete origins, sessions, RR flags and admission policies.

    This accepts the ordinary IPv4 iBGP sketch, including counterexamples that
    change its ASN or remove announcements/policy clauses. It rejects features
    outside that subset rather than reconstructing policies from the design.
    """
    if not fragment.strip():
        return ""
    router = {}
    peers = {}
    prefixes = {}
    policies = {}
    addresses = []
    networks = []
    flags = set()
    section, clause, af = None, None, False

    def reject(line):
        raise ValueError(f"Unsupported FRR iBGP configuration: {line!r}")

    def assign(target, key, value):
        if key in target:
            reject(f"duplicate {key}")
        target[key] = value

    for raw in fragment.splitlines():
        line = raw.strip()
        if not line or line.startswith(("!", "#")):
            continue
        if line == f"interface {BGP_ORIGIN_IFACE}":
            if addresses or section == "origin":
                reject(line)
            section = "origin"
        elif line.startswith("ip address ") and section == "origin":
            address = IPv4Interface(line.removeprefix("ip address "))
            if address in addresses:
                reject(line)
            addresses.append(address)
        elif section == "origin" and (
            line == "no shutdown" or line.startswith("description ")
        ):
            continue
        elif match := re.fullmatch(
            r"ip prefix-list (\S+) seq ([0-9]+) permit ([0-9./]+)(?: le ([0-9]+))?",
            line,
        ):
            name, seq, prefix, maximum = match.groups()
            network = IPv4Network(prefix)
            maximum = int(maximum) if maximum is not None else network.prefixlen
            if not network.prefixlen <= maximum <= 32:
                reject(line)
            assign(prefixes.setdefault(name, {}), int(seq), (network, maximum))
            section = "global"
        elif match := re.fullmatch(r"route-map (\S+) (permit|deny) ([0-9]+)", line):
            name, action, seq = match.groups()
            clause = {"action": action}
            assign(policies.setdefault(name, {}), int(seq), clause)
            section = "policy"
        elif match := re.fullmatch(r"match ip address prefix-list (\S+)", line):
            if section != "policy":
                reject(line)
            assign(clause, "prefix_list", match[1])
        elif match := re.fullmatch(r"router bgp ([0-9]+)", line):
            asn = int(match[1])
            if not 1 <= asn <= 4294967295:
                reject(line)
            assign(router, "asn", asn)
            section = "router"
        elif section == "router":
            if match := re.fullmatch(r"bgp (router-id|cluster-id) ([0-9.]+)", line):
                assign(router, match[1], str(IPv4Address(match[2])))
            elif line in {"no bgp default ipv4-unicast", "no bgp network import-check"}:
                if line in flags:
                    reject(line)
                flags.add(line)
            elif line in {
                "no bgp ebgp-requires-policy",
                "bgp default show-hostname",
                "bgp default show-nexthop-hostname",
                "bgp bestpath as-path multipath-relax",
            }:
                # The declared ordinary iBGP subset has empty AS paths and no
                # community-based policy; these defaults do not change its RIB.
                flags.add(line)
                continue
            elif line == "address-family ipv4 unicast" and not af:
                af = True
            elif line == "exit-address-family" and af:
                af = False
            elif line.startswith("network ") and af:
                prefix = IPv4Network(line.removeprefix("network "))
                if prefix in networks:
                    reject(line)
                networks.append(prefix)
            elif match := re.fullmatch(
                r"no neighbor ([0-9.]+) send-community all", line
            ):
                if not af or str(IPv4Address(match[1])) not in peers:
                    reject(line)
            elif match := re.fullmatch(r"neighbor ([0-9.]+) (.+)", line):
                peer = peers.setdefault(str(IPv4Address(match[1])), {})
                attribute = match[2]
                if value := re.fullmatch(r"remote-as ([0-9]+)", attribute):
                    asn = int(value[1])
                    if af or not 1 <= asn <= 4294967295:
                        reject(line)
                    assign(peer, "asn", asn)
                elif attribute == "update-source lo" and not af:
                    assign(peer, "source", "Loopback0")
                elif attribute in {"activate", "route-reflector-client"} and af:
                    assign(peer, attribute, True)
                elif attribute.startswith("description ") and not af:
                    continue
                elif (
                    attribute
                    in {
                        "send-community standard",
                        "send-community large",
                        "send-community extended",
                    }
                    and af
                ):
                    continue
                elif value := re.fullmatch(r"route-map (\S+) (in|out)", attribute):
                    if not af:
                        reject(line)
                    assign(peer, value[2], value[1])
                else:
                    reject(line)
            else:
                reject(line)
        else:
            reject(line)
    # FRR also accepts an address family ending at configuration-file EOF.
    if (
        not {"asn", "router-id"} <= router.keys()
        or "no bgp default ipv4-unicast" not in flags
    ):
        reject("incomplete BGP instance")
    if any(prefix not in {a.network for a in addresses} for prefix in networks):
        reject("non-connected origin in the connected-prefix subset")
    if any("asn" not in peer for peer in peers.values()):
        reject("incomplete BGP peer")
    if flags & {
        "no bgp ebgp-requires-policy",
        "bgp bestpath as-path multipath-relax",
    } and any(peer["asn"] != router["asn"] for peer in peers.values()):
        reject("upstream BGP defaults are only modeled for same-AS iBGP peers")

    lines = []
    if addresses:
        lines.append("interface Loopback1")
        lines.extend(f" ipv4 address {address}" for address in addresses)
        lines.append("!")
    for name, entries in sorted(prefixes.items()):
        lines.extend(
            [
                f"prefix-set {name}",
                ",\n".join(
                    f" {network}"
                    + (f" le {maximum}" if maximum > network.prefixlen else "")
                    for _, (network, maximum) in sorted(entries.items())
                ),
                "end-set",
                "!",
            ]
        )
    for name, entries in sorted(policies.items()):
        lines.append(f"route-policy {name}")
        for _, entry in sorted(entries.items()):
            selector = entry.get("prefix_list")
            if selector is not None:
                if selector not in prefixes:
                    reject(f"undefined prefix-list {selector}")
                lines.append(f" if destination in {selector} then")
            lines.append("  done" if entry["action"] == "permit" else "  drop")
            if selector is not None:
                lines.append(" endif")
        lines.extend([" drop", "end-policy", "!"])
    lines.extend(
        [f"router bgp {router['asn']}", f" bgp router-id {router['router-id']}"]
    )
    if "cluster-id" in router:
        lines.append(f" bgp cluster-id {router['cluster-id']}")
    lines.append(" address-family ipv4 unicast")
    lines.extend(f"  network {prefix}" for prefix in networks)
    lines.append(" !")
    for address, peer in sorted(peers.items()):
        lines.extend([f" neighbor {address}", f"  remote-as {peer['asn']}"])
        if "source" in peer:
            lines.append(f"  update-source {peer['source']}")
        if peer.get("activate"):
            lines.append("  address-family ipv4 unicast")
            if peer.get("route-reflector-client"):
                lines.append("   route-reflector-client")
            for direction in ("in", "out"):
                if direction in peer:
                    if peer[direction] not in policies:
                        reject(f"undefined route-map {peer[direction]}")
                    lines.append(f"   route-policy {peer[direction]} {direction}")
            lines.append("  !")
        lines.append(" !")
    lines.append("!")
    return "\n".join(lines) + "\n"
