"""Parse the FRR router sketches of Kathara Ethernet environments.

Environments describe each router with Linux startup commands and an FRR
configuration. Native router translators read the same sketch so a converted
router keeps the environment's design. The parser records every statement it
knows; each translator rejects what its platform cannot express, and any
unknown statement is rejected here, so a design never deploys with silently
changed meaning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Interface, IPv4Network

# FRR statements without forwarding semantics (logging and the vty shell).
_IGNORED = re.compile(r"^(?:!.*|log .*|debug .*|line vty|frr (?:version|defaults) .*)$")
_ADDRESS = re.compile(r"ip addr add (\S+) dev (eth\d+|br0)")
_STATIC = re.compile(r"ip route add (default|[0-9./]+) via ([0-9.]+)(?: dev eth\d+)?")
_SHAPING = re.compile(r"tc qdisc (?:add|replace) dev (eth\d+) root netem .*")
_BRIDGE_PORT = re.compile(r"brctl addif br0 (eth\d+)")
_DHCP_RELAY = re.compile(r"dhcrelay +-i br0 -i (eth\d+) ([0-9.]+)")
_BRIDGE = {"brctl addbr br0", "ip link set br0 up"}
# Linux forwarding is irrelevant once a native router forwards the traffic.
_LINUX_ONLY = {"service frr start", "sysctl -w net.ipv4.ip_forward=1"}


@dataclass
class Bgp:
    asn: int
    router_id: str | None = None
    multipath_relax: bool = False
    maximum_paths: int | None = None
    requires_policy: bool = True
    networks: list[str] = field(default_factory=list)
    neighbors: dict[str, int] = field(default_factory=dict)
    # (neighbor, direction) -> route-map name
    route_maps: dict[tuple[str, str], str] = field(default_factory=dict)


@dataclass
class Ospf:
    router_id: str | None = None
    networks: list[tuple[IPv4Network, str]] = field(default_factory=list)


@dataclass
class Policy:
    """Exact-prefix lists, route maps and interface costs outside routing blocks.

    These are the forms that solved routing parameters take in the sketch.
    """

    prefix_lists: dict[str, list[str]] = field(default_factory=dict)
    # route-map -> [(seq, permit, prefix-list)]
    route_maps: dict[str, list[tuple[int, bool, str | None]]] = field(
        default_factory=dict
    )
    ospf_costs: dict[str, int] = field(default_factory=dict)


@dataclass
class Startup:
    interfaces: list[tuple[str, str]] = field(default_factory=list)
    static_routes: list[tuple[str, str]] = field(default_factory=list)
    shaping: list[str] = field(default_factory=list)
    # A Linux bridge ``br0`` over access ports, addressed as a gateway.
    bridge_ports: list[str] | None = None
    # (uplink interface, DHCP server) of a relay serving ``br0``.
    dhcp_relay: tuple[str, str] | None = None


def parse_startup(startup: str) -> Startup:
    parsed = Startup()
    for line in startup.splitlines():
        line = line.strip().removesuffix(" || true")
        if not line or line in _LINUX_ONLY:
            continue
        if match := _ADDRESS.fullmatch(line):
            IPv4Interface(match[1])
            parsed.interfaces.append((match[2], match[1]))
        elif match := _STATIC.fullmatch(line):
            prefix = "0.0.0.0/0" if match[1] == "default" else match[1]
            parsed.static_routes.append((str(IPv4Network(prefix)), match[2]))
        elif _SHAPING.fullmatch(line):
            parsed.shaping.append(line)
        elif line in _BRIDGE:
            parsed.bridge_ports = parsed.bridge_ports or []
        elif match := _BRIDGE_PORT.fullmatch(line):
            if parsed.bridge_ports is None:
                raise ValueError("Bridge port added before the bridge exists")
            parsed.bridge_ports.append(match[1])
        elif match := _DHCP_RELAY.fullmatch(line):
            parsed.dhcp_relay = (match[1], str(IPv4Address(match[2])))
        else:
            raise ValueError(f"Unsupported router startup command: {line!r}")
    names = [name for name, _ in parsed.interfaces]
    if len(set(names)) != len(names):
        raise ValueError("Native translation needs one address per interface")
    if any(_SHAPING.fullmatch(line)[1] not in names for line in parsed.shaping):
        raise ValueError("Linux shaping must target an addressed router interface")
    if "br0" in names and parsed.bridge_ports is None:
        raise ValueError("br0 is addressed but never created")
    return parsed


def parse_frr(frr_conf: str, hostname: str) -> tuple[Bgp | None, Ospf | None, Policy]:
    bgp = ospf = None
    policy = Policy()
    block = entry = None
    for raw in frr_conf.splitlines():
        line = raw.strip()
        indented = raw[:1].isspace()
        if not line or _IGNORED.fullmatch(line):
            block = block if indented else None
            continue
        if not indented:
            block = None
            if match := re.fullmatch(r"hostname (\S+)", line):
                if match[1] != hostname:
                    raise ValueError(f"FRR hostname {match[1]!r} != {hostname!r}")
                continue
            if match := re.fullmatch(r"router bgp (\d+)", line):
                # Later blocks of the same instance add statements, as in FRR.
                if bgp is not None and bgp.asn != int(match[1]):
                    raise ValueError("Native translation needs one BGP instance")
                bgp, block = bgp or Bgp(int(match[1])), "bgp"
                continue
            if line == "router ospf":
                if ospf is not None:
                    raise ValueError("Native translation needs one OSPF instance")
                ospf, block = Ospf(), "ospf"
                continue
            if match := re.fullmatch(
                r"ip prefix-list (\S+) seq \d+ permit ([0-9./]+)", line
            ):
                prefix = IPv4Network(match[2])
                policy.prefix_lists.setdefault(match[1], []).append(str(prefix))
                continue
            if match := re.fullmatch(r"route-map (\S+) (permit|deny) (\d+)", line):
                entry = [int(match[3]), match[2] == "permit", None]
                policy.route_maps.setdefault(match[1], []).append(entry)
                block = "route-map"
                continue
            if match := re.fullmatch(r"interface (eth\d+)", line):
                block, entry = "interface", match[1]
                continue
        elif block == "bgp" and _bgp_statement(bgp, line):
            continue
        elif block == "ospf":
            if match := re.fullmatch(r"(?:ospf )?router-id (\S+)", line):
                ospf.router_id = str(IPv4Address(match[1]))
                continue
            if match := re.fullmatch(r"network (\S+) area (\S+)", line):
                area = match[2]
                area = str(IPv4Address(int(area))) if area.isdigit() else area
                ospf.networks.append((IPv4Network(match[1]), str(IPv4Address(area))))
                continue
        elif block == "route-map":
            match = re.fullmatch(r"match ip address prefix-list (\S+)", line)
            if match and entry[2] is None:
                entry[2] = match[1]
                continue
        elif block == "interface":
            if match := re.fullmatch(r"ip ospf cost (\d+)", line):
                policy.ospf_costs[entry] = int(match[1])
                continue
        raise ValueError(f"Unsupported FRR statement: {line!r}")
    for name, entries in policy.route_maps.items():
        for _, _, plist in entries:
            if plist is None or plist not in policy.prefix_lists:
                raise ValueError(f"Route map {name} needs a defined exact prefix list")
    if bgp is not None:
        for name in bgp.route_maps.values():
            if name not in policy.route_maps:
                raise ValueError(f"Undefined route map {name!r}")
    return bgp, ospf, policy


def _bgp_statement(bgp: Bgp, line: str) -> bool:
    if match := re.fullmatch(r"bgp router-id (\S+)", line):
        bgp.router_id = str(IPv4Address(match[1]))
    elif line == "bgp bestpath as-path multipath-relax":
        bgp.multipath_relax = True
    elif match := re.fullmatch(r"maximum-paths (\d+)", line):
        bgp.maximum_paths = int(match[1])
    elif line == "no bgp ebgp-requires-policy":
        bgp.requires_policy = False
    elif match := re.fullmatch(r"network (\S+)", line):
        bgp.networks.append(str(IPv4Network(match[1])))
    elif match := re.fullmatch(r"neighbor (\S+) remote-as (\d+)", line):
        bgp.neighbors[str(IPv4Address(match[1]))] = int(match[2])
    elif match := re.fullmatch(r"neighbor (\S+) route-map (\S+) (in|out)", line):
        peer = str(IPv4Address(match[1]))
        if peer not in bgp.neighbors or (peer, match[3]) in bgp.route_maps:
            raise ValueError(f"Unsupported route map binding for {peer}")
        bgp.route_maps[peer, match[3]] = match[2]
    else:
        return False
    return True
