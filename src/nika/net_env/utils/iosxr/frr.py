"""Translate a Kathara FRR router sketch to native IOS XR configuration.

Ethernet scenarios describe each router with Linux address commands and an FRR
configuration. This translation preserves that design on XRd for the subset
whose semantics map one to one: interface addresses and IPv4 BGP with network
origins, eBGP multipath and FRR's eBGP policy default. Any other statement is
rejected, so an unsupported design cannot deploy with silently changed meaning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Interface, IPv4Network

from nika.net_env.utils.iosxr.common import iosxr_interface

# FRR statements without forwarding semantics (logging and the vty shell).
_IGNORED = re.compile(r"^(?:!.*|log .*|debug .*|line vty|frr (?:version|defaults) .*)$")
_STARTUP_ADDRESS = re.compile(r"^ip addr add (\S+) dev (eth\d+)$")
_FRR_START = {"service frr start", "/etc/init.d/frr start"}
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


def startup_interfaces(startup: str) -> list[tuple[str, str]]:
    """Return ``(linux interface, address/len)`` from a router startup file."""
    addresses = []
    for line in startup.splitlines():
        line = line.strip()
        if not line or line in _FRR_START:
            continue
        match = _STARTUP_ADDRESS.fullmatch(line)
        if match is None:
            raise ValueError(f"Unsupported IOS XR router startup command: {line!r}")
        IPv4Interface(match[1])
        addresses.append((match[2], match[1]))
    if len({name for name, _ in addresses}) != len(addresses):
        raise ValueError("IOS XR translation needs one address per interface")
    return addresses


def _parse_bgp(frr_conf: str, hostname: str) -> _Bgp | None:
    bgp = None
    in_bgp = False
    for raw in frr_conf.splitlines():
        line = raw.strip()
        if not line or _IGNORED.fullmatch(line):
            in_bgp = in_bgp and raw[:1].isspace()
            continue
        if not raw[:1].isspace():
            in_bgp = False
            if match := re.fullmatch(r"hostname (\S+)", line):
                if match[1] != hostname:
                    raise ValueError(f"FRR hostname {match[1]!r} != {hostname!r}")
                continue
            if match := re.fullmatch(r"router bgp (\d+)", line):
                if bgp is not None:
                    raise ValueError("IOS XR translation needs one BGP instance")
                bgp, in_bgp = _Bgp(int(match[1])), True
                continue
        elif in_bgp and bgp is not None:
            if match := re.fullmatch(r"bgp router-id (\S+)", line):
                bgp.router_id = str(IPv4Address(match[1]))
                continue
            if line == "bgp bestpath as-path multipath-relax":
                bgp.multipath_relax = True
                continue
            if match := re.fullmatch(r"maximum-paths (\d+)", line):
                bgp.maximum_paths = int(match[1])
                continue
            if line == "no bgp ebgp-requires-policy":
                bgp.requires_policy = False
                continue
            if match := re.fullmatch(r"network (\S+)", line):
                bgp.networks.append(str(IPv4Network(match[1])))
                continue
            if match := re.fullmatch(r"neighbor (\S+) remote-as (\d+)", line):
                bgp.neighbors[str(IPv4Address(match[1]))] = int(match[2])
                continue
        raise ValueError(f"Unsupported FRR statement for IOS XR: {line!r}")
    return bgp


def translate_frr_router(
    hostname: str, frr_conf: str, startup: str
) -> tuple[str, list[str]]:
    """Return IOS XR configuration and the Linux interfaces XR takes over."""
    interfaces = startup_interfaces(startup)
    if not interfaces:
        raise ValueError(f"Router {hostname!r} has no addressed interfaces")
    bgp = _parse_bgp(frr_conf, hostname)
    lines = [f"hostname {hostname}", "!"]
    # Interfaces precede routing blocks: XR resolves a command in the innermost
    # mode that accepts it.
    for name, address in interfaces:
        lines.extend(
            [
                f"interface {iosxr_interface(name)}",
                f" ipv4 address {address}",
                " no shutdown",
                "!",
            ]
        )
    if bgp is not None:
        lines.extend(_render_bgp(bgp))
    lines.extend(["end", ""])
    return "\n".join(lines), [name for name, _ in interfaces]


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


def convert_frr_routers(env) -> dict[str, str]:
    """Redeploy every router of a built Kathara FRR scenario as XRd.

    Topology, links, hosts and services are unchanged. Returns the native
    configuration per router; unsupported router designs raise ``ValueError``.
    """
    from dataclasses import replace

    from nika.net_env.utils.iosxr.common import configure_xrd_router
    from nika.runtime.spec import MachineInventory

    configs = {}
    for name in env.routers:
        machine = env.lab.machines[name]
        frr_conf = machine.fs.readtext("etc/frr/frr.conf")
        startup = env.lab.fs.readtext(f"{name}.startup")
        config, interfaces = translate_frr_router(name, frr_conf, startup)
        machine.fs.removetree("etc/frr")
        configure_xrd_router(env.lab, machine, config, interfaces)
        identity = env.machine_identities[name]
        capabilities = {*identity.capabilities, "iosxr"} - {"frr"}
        env.machine_identities[name] = replace(
            identity, capabilities=tuple(sorted(capabilities))
        )
        configs[name] = config
    env.metadata["machine_identities"] = MachineInventory(
        env.machine_identities
    ).to_dict()
    env.load_machines()
    return configs
