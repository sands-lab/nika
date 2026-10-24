"""Translate Kathara FRR router sketches to RouterOS configuration.

The translation keeps each router's design: interface addresses, static
routes, OSPF network-to-area bindings, BGP sessions with network origins and
FRR's open eBGP default, Linux bridged gateways and their DHCP relay. Linux
egress shaping (netem) stays on the container links. RouterOS installs one
best BGP path, so FRR's eBGP multipath has no equivalent; unsupported
statements are rejected rather than deployed with changed meaning.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from nika.net_env.native_routers import NativeConversion
from nika.net_env.utils.frr_sketch import parse_frr, parse_startup
from nika.net_env.utils.routeros.common import bridged_interface
from nika.net_env.utils.routeros.render import (
    render_base,
    render_bridge,
    render_dhcp_relay,
    render_ebgp,
    render_ospf,
    render_script,
)


@dataclass(frozen=True)
class RouterOsRouter:
    """A translated router: its script, data interfaces and link shaping."""

    config: str
    interfaces: tuple[str, ...]
    link_commands: tuple[str, ...] = ()


def translate_frr_router(hostname: str, frr_conf: str, startup: str) -> RouterOsRouter:
    """Translate one router; raise ``ValueError`` for unmapped statements."""
    parsed = parse_startup(startup)
    bgp, ospf, policy = parse_frr(frr_conf, hostname)
    if policy.prefix_lists or policy.route_maps or policy.ospf_costs:
        raise ValueError(
            "RouterOS translation does not keep FRR prefix lists, route maps "
            "or OSPF interface costs"
        )
    routed = [(name, cidr) for name, cidr in parsed.interfaces if name != "br0"]
    bridged = [cidr for name, cidr in parsed.interfaces if name == "br0"]
    data = [name for name, _ in routed] + list(parsed.bridge_ports or ())
    if not data:
        raise ValueError(f"Router {hostname!r} has no data interfaces")
    if (
        bgp is not None
        and bgp.requires_policy
        and any(asn != bgp.asn for asn in bgp.neighbors.values())
    ):
        raise ValueError("RouterOS translation needs FRR's open eBGP policy")
    sections = [render_base(hostname, [(bridged_interface(n), c) for n, c in routed])]
    if parsed.bridge_ports is not None:
        sections.append(
            render_bridge([bridged_interface(n) for n in parsed.bridge_ports], bridged)
        )
    sections.append(
        [
            f"/ip route add dst-address={prefix} gateway={hop}"
            for prefix, hop in parsed.static_routes
        ]
    )
    first_address = parsed.interfaces[0][1].split("/")[0] if parsed.interfaces else ""
    if ospf is not None:
        sections.append(
            render_ospf(
                ospf.router_id or first_address,
                {str(network): area for network, area in ospf.networks},
            )
        )
    if bgp is not None:
        sections.append(
            render_ebgp(
                bgp.asn,
                bgp.router_id or first_address,
                bgp.neighbors.items(),
                networks=bgp.networks,
            )
        )
    if parsed.dhcp_relay is not None:
        if not bridged:
            raise ValueError("A DHCP relay needs the bridged gateway address")
        sections.append(render_dhcp_relay(parsed.dhcp_relay[1], bridged[0]))
    return RouterOsRouter(render_script(*sections), tuple(data), tuple(parsed.shaping))


def convert_frr_routers(env, names) -> NativeConversion:
    """Redeploy the named routers of a built Kathara FRR environment on RouterOS.

    Links, container interface names, addressing and every other node are
    unchanged. An environment may render a router itself through
    ``native_router_config(profile, name)``; otherwise its FRR sketch is
    translated.
    """
    from nika.net_env.utils.routeros.common import configure_routeros_router
    from nika.runtime.spec import MachineInventory

    configs = {}
    custom = getattr(env, "native_router_config", None)
    for name in names:
        machine = env.lab.machines[name]
        router = custom("routeros", name) if custom else None
        if router is None:
            router = translate_frr_router(
                name,
                machine.fs.readtext("etc/frr/frr.conf"),
                env.lab.fs.readtext(f"{name}.startup"),
            )
        configure_routeros_router(
            env.lab,
            machine,
            router.config,
            router.interfaces,
            link_commands=router.link_commands,
        )
        identity = env.machine_identities[name]
        capabilities = {*identity.capabilities, "routeros"} - {"frr"}
        env.machine_identities[name] = replace(
            identity, capabilities=tuple(sorted(capabilities))
        )
        configs[name] = router.config
    env.metadata["machine_identities"] = MachineInventory(
        env.machine_identities
    ).to_dict()
    env.load_machines()
    return NativeConversion(configs, {})
