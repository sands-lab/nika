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

from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface

from nika.net_env.native_routers import NativeConversion
from nika.net_env.utils.frr_sketch import (
    Bgp,
    Ospf,
    Policy,
    Startup,
    parse_frr,
    parse_startup,
)
from nika.net_env.utils.iosxr.common import iosxr_interface

PASS_POLICY = "PASS"


def _parse_startup(startup: str) -> Startup:
    try:
        parsed = parse_startup(startup)
    except ValueError as exc:
        raise ValueError(f"Unsupported IOS XR router startup command: {exc}") from exc
    # XRd Control Plane cannot route a bridged interface or relay DHCP.
    if parsed.bridge_ports is not None or parsed.dhcp_relay is not None:
        raise ValueError(
            "Unsupported IOS XR router startup command: Linux bridge or DHCP relay"
        )
    return parsed


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
    bgp, ospf, policy = parse_frr(frr_conf, hostname)
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
    ospf: Ospf, interfaces: list[tuple[str, str]], costs: dict[str, int]
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


def _render_policies(policy: Policy, used: set[str]) -> list[str]:
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


def _render_bgp(bgp: Bgp) -> list[str]:
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
