"""Translate Kathara FRR router sketches to native IOS XR configuration.

Ethernet environments describe each router with Linux startup commands and an
FRR configuration. This translation keeps that design on XRd for the subset
whose semantics map one to one: interface addresses, static routes, OSPF
network-to-area bindings with interface costs, and IPv4 BGP with network
origins, eBGP multipath, FRR's eBGP policy default and per-neighbor route maps
over exact-prefix lists (the form solved routing parameters take). Linux egress shaping (netem) still applies to the
traffic XR transmits; the XRd image has no ``tc``, so the commands are returned
for the environment to run in the router's network namespace. Any other
statement is rejected, so an unsupported design cannot deploy with silently
changed meaning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Interface, IPv4Network

from nika.net_env.native_routers import NativeConversion
from nika.net_env.utils.iosxr.common import iosxr_interface

# FRR statements without forwarding semantics (logging and the vty shell).
_IGNORED = re.compile(r"^(?:!.*|log .*|debug .*|line vty|frr (?:version|defaults) .*)$")
_ADDRESS = re.compile(r"ip addr add (\S+) dev (eth\d+)")
_STATIC = re.compile(r"ip route add (default|[0-9./]+) via ([0-9.]+)(?: dev eth\d+)?")
_SHAPING = re.compile(r"tc qdisc (?:add|replace) dev (eth\d+) root netem .*")
# Linux forwarding is irrelevant once XR forwards the router's traffic.
_LINUX_ONLY = {"service frr start", "sysctl -w net.ipv4.ip_forward=1"}
PASS_POLICY = "PASS"


@dataclass
class _Bgp:
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
class _Ospf:
    router_id: str | None = None
    networks: list[tuple[IPv4Network, str]] = field(default_factory=list)


@dataclass
class _Policy:
    """Exact-prefix lists, route maps and interface costs outside routing blocks."""

    prefix_lists: dict[str, list[str]] = field(default_factory=dict)
    # route-map -> [(seq, permit, prefix-list)]
    route_maps: dict[str, list[tuple[int, bool, str | None]]] = field(
        default_factory=dict
    )
    ospf_costs: dict[str, int] = field(default_factory=dict)


@dataclass
class _Startup:
    interfaces: list[tuple[str, str]] = field(default_factory=list)
    static_routes: list[tuple[str, str]] = field(default_factory=list)
    shaping: list[str] = field(default_factory=list)


def _parse_startup(startup: str) -> _Startup:
    parsed = _Startup()
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
        else:
            raise ValueError(f"Unsupported IOS XR router startup command: {line!r}")
    names = [name for name, _ in parsed.interfaces]
    if len(set(names)) != len(names):
        raise ValueError("IOS XR translation needs one address per interface")
    if any(_SHAPING.fullmatch(line)[1] not in names for line in parsed.shaping):
        raise ValueError("Linux shaping must target an addressed router interface")
    return parsed


def _parse_frr(
    frr_conf: str, hostname: str
) -> tuple[_Bgp | None, _Ospf | None, _Policy]:
    bgp = ospf = None
    policy = _Policy()
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
                    raise ValueError("IOS XR translation needs one BGP instance")
                bgp, block = bgp or _Bgp(int(match[1])), "bgp"
                continue
            if line == "router ospf":
                if ospf is not None:
                    raise ValueError("IOS XR translation needs one OSPF instance")
                ospf, block = _Ospf(), "ospf"
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
        raise ValueError(f"Unsupported FRR statement for IOS XR: {line!r}")
    for name, entries in policy.route_maps.items():
        for _, _, plist in entries:
            if plist is None or plist not in policy.prefix_lists:
                raise ValueError(f"Route map {name} needs a defined exact prefix list")
    if bgp is not None:
        for name in bgp.route_maps.values():
            if name not in policy.route_maps:
                raise ValueError(f"Undefined route map {name!r}")
    return bgp, ospf, policy


def _bgp_statement(bgp: _Bgp, line: str) -> bool:
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


@dataclass(frozen=True)
class XrRouter:
    """A translated router and the Linux commands its namespace still needs."""

    config: str
    interfaces: tuple[str, ...]
    netns_commands: tuple[str, ...] = ()


def translate_frr_router(hostname: str, frr_conf: str, startup: str) -> XrRouter:
    """Translate one router; raise ``ValueError`` for unmapped statements."""
    parsed = _parse_startup(startup)
    if not parsed.interfaces:
        raise ValueError(f"Router {hostname!r} has no addressed interfaces")
    bgp, ospf, policy = _parse_frr(frr_conf, hostname)
    names = {name for name, _ in parsed.interfaces}
    if not set(policy.ospf_costs) <= names or (policy.ospf_costs and ospf is None):
        raise ValueError("OSPF costs must name addressed OSPF interfaces")
    lines = [f"hostname {hostname}", "!"]
    # Interfaces precede routing blocks: XR resolves a command in the innermost
    # mode that accepts it.
    for name, address in parsed.interfaces:
        lines.extend(
            [
                f"interface {iosxr_interface(name)}",
                f" ipv4 address {address}",
                " no shutdown",
                "!",
            ]
        )
    if parsed.static_routes:
        lines.extend(["router static", " address-family ipv4 unicast"])
        lines.extend(f"  {prefix} {hop}" for prefix, hop in parsed.static_routes)
        lines.extend([" !", "!"])
    if ospf is not None:
        lines.extend(_render_ospf(ospf, parsed.interfaces, policy.ospf_costs))
    if bgp is not None:
        lines.extend(_render_policies(policy, set(bgp.route_maps.values())))
        lines.extend(_render_bgp(bgp))
    lines.extend(["end", ""])
    return XrRouter(
        "\n".join(lines),
        tuple(name for name, _ in parsed.interfaces),
        tuple(parsed.shaping),
    )


def _render_ospf(
    ospf: _Ospf, interfaces: list[tuple[str, str]], costs: dict[str, int]
) -> list[str]:
    # FRR enables OSPF on each interface whose primary address falls in a
    # ``network`` statement; XR names those interfaces under their area.
    areas: dict[str, list[str]] = {}
    for name, address in interfaces:
        matches = {
            area
            for network, area in ospf.networks
            if IPv4Interface(address).ip in network
        }
        if len(matches) > 1:
            raise ValueError("Overlapping OSPF area bindings are not translated")
        if matches:
            areas.setdefault(matches.pop(), []).append(name)
    lines = ["router ospf CORE"]
    if ospf.router_id:
        lines.append(f" router-id {ospf.router_id}")
    for area in sorted(areas, key=lambda value: int(IPv4Address(value))):
        lines.append(f" area {int(IPv4Address(area))}")
        for name in areas[area]:
            lines.append(f"  interface {iosxr_interface(name)}")
            if name in costs:
                lines.append(f"   cost {costs[name]}")
            lines.append("  !")
        lines.append(" !")
    lines.append("!")
    if not set(costs) <= {name for names in areas.values() for name in names}:
        raise ValueError("OSPF costs must name interfaces enabled for OSPF")
    return lines


def _render_policies(policy: _Policy, used: set[str]) -> list[str]:
    """Render FRR route maps as XR route policies with the same semantics.

    An FRR route map applies its first matching entry, and denies a route
    that no entry matches; an exact-prefix list matches only that prefix.
    """
    lines = []
    lists = sorted({plist for name in used for _, _, plist in policy.route_maps[name]})
    for plist in lists:
        prefixes = policy.prefix_lists[plist]
        lines.append(f"prefix-set {plist}")
        lines.extend(
            f"  {prefix}{',' if index < len(prefixes) - 1 else ''}"
            for index, prefix in enumerate(prefixes)
        )
        lines.extend(["end-set", "!"])
    for name in sorted(used):
        lines.append(f"route-policy {name}")
        for index, (_, permit, plist) in enumerate(sorted(policy.route_maps[name])):
            keyword = "if" if index == 0 else "elseif"
            lines.extend(
                [
                    f"  {keyword} destination in {plist} then",
                    f"    {'pass' if permit else 'drop'}",
                ]
            )
        lines.extend(["  else", "    drop", "  endif", "end-policy", "!"])
    return lines


def _render_bgp(bgp: _Bgp) -> list[str]:
    external = {peer for peer, asn in bgp.neighbors.items() if asn != bgp.asn}
    # FRR without ``ebgp-requires-policy`` accepts and advertises everything on
    # eBGP sessions. XR, like FRR's default, exchanges nothing without policy.
    open_policy = not bgp.requires_policy and external
    lines = []
    if open_policy:
        lines.extend([f"route-policy {PASS_POLICY}", " pass", "end-policy", "!"])
    lines.append(f"router bgp {bgp.asn}")
    if bgp.router_id:
        lines.append(f" bgp router-id {bgp.router_id}")
    if bgp.multipath_relax:
        lines.append(" bgp bestpath as-path multipath-relax")
    lines.append(" address-family ipv4 unicast")
    if bgp.maximum_paths:
        # FRR ``maximum-paths`` applies to eBGP paths; iBGP has its own knob.
        lines.append(f"  maximum-paths ebgp {bgp.maximum_paths}")
    lines.extend(f"  network {network}" for network in bgp.networks)
    lines.append(" !")
    for peer, asn in bgp.neighbors.items():
        lines.extend([f" neighbor {peer}", f"  remote-as {asn}"])
        lines.append("  address-family ipv4 unicast")
        for direction in ("in", "out"):
            name = bgp.route_maps.get((peer, direction))
            if name is None and open_policy and peer in external:
                name = PASS_POLICY
            if name is not None:
                lines.append(f"   route-policy {name} {direction}")
        lines.extend(["  !", " !"])
    lines.append("!")
    return lines


def convert_frr_routers(env, names) -> NativeConversion:
    """Redeploy the named routers of a built Kathara FRR environment as XRd.

    Links, addressing and every other node are unchanged. Returns the native
    configuration per converted router and the Linux shaping commands, per
    router, to run in its network namespace after deployment.
    """
    from dataclasses import replace

    from nika.net_env.utils.iosxr.common import configure_xrd_router
    from nika.runtime.spec import MachineInventory

    configs, netns_commands = {}, {}
    for name in names:
        machine = env.lab.machines[name]
        router = translate_frr_router(
            name,
            machine.fs.readtext("etc/frr/frr.conf"),
            env.lab.fs.readtext(f"{name}.startup"),
        )
        machine.fs.removetree("etc/frr")
        configure_xrd_router(env.lab, machine, router.config, router.interfaces)
        identity = env.machine_identities[name]
        capabilities = {*identity.capabilities, "iosxr"} - {"frr"}
        env.machine_identities[name] = replace(
            identity, capabilities=tuple(sorted(capabilities))
        )
        configs[name] = router.config
        if router.netns_commands:
            netns_commands[name] = router.netns_commands
    env.metadata["machine_identities"] = MachineInventory(
        env.machine_identities
    ).to_dict()
    env.load_machines()
    return NativeConversion(configs, netns_commands)
