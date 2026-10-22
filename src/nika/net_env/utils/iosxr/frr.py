"""Translate Kathara FRR router sketches to native IOS XR configuration.

Ethernet environments describe each router with Linux startup commands and an
FRR configuration. This translation keeps that design on XRd for the subset
whose semantics map one to one: interface addresses, static routes, OSPF
network-to-area bindings, and IPv4 BGP with network origins, eBGP multipath and
FRR's eBGP policy default. Linux egress shaping (netem) still applies to the
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


@dataclass
class _Ospf:
    router_id: str | None = None
    networks: list[tuple[IPv4Network, str]] = field(default_factory=list)


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


def _parse_frr(frr_conf: str, hostname: str) -> tuple[_Bgp | None, _Ospf | None]:
    bgp = ospf = None
    block = None
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
                if bgp is not None:
                    raise ValueError("IOS XR translation needs one BGP instance")
                bgp, block = _Bgp(int(match[1])), "bgp"
                continue
            if line == "router ospf":
                if ospf is not None:
                    raise ValueError("IOS XR translation needs one OSPF instance")
                ospf, block = _Ospf(), "ospf"
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
        raise ValueError(f"Unsupported FRR statement for IOS XR: {line!r}")
    return bgp, ospf


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
    bgp, ospf = _parse_frr(frr_conf, hostname)
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
        lines.extend(_render_ospf(ospf, parsed.interfaces))
    if bgp is not None:
        lines.extend(_render_bgp(bgp))
    lines.extend(["end", ""])
    return XrRouter(
        "\n".join(lines),
        tuple(name for name, _ in parsed.interfaces),
        tuple(parsed.shaping),
    )


def _render_ospf(ospf: _Ospf, interfaces: list[tuple[str, str]]) -> list[str]:
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
            lines.extend([f"  interface {iosxr_interface(name)}", "  !"])
        lines.append(" !")
    lines.append("!")
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
        if open_policy and peer in external:
            lines.extend(
                [
                    f"   route-policy {PASS_POLICY} in",
                    f"   route-policy {PASS_POLICY} out",
                ]
            )
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
