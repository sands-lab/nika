"""Export concrete Kathara FRR routers for independent route analysis.

Address statements are read from concrete startup commands. This exporter accepts
physical Ethernet router links only; it does not infer DHCP, bridges or tunnels.
Routers that an environment runs on a native platform (``native_routers``) are
exported as their concrete native configuration for Batfish's vendor parser, so
one snapshot analyzes FRR and native routers together.
"""

from __future__ import annotations
import json
import re
from pathlib import Path
from ipaddress import IPv4Interface
from nika.validation.base import ValidationSnapshot
from nika.validation.batfish.snapshot import (
    normalize_frr_for_batfish,
    prepare_snapshot,
    _cumulus_frr_snapshot,
    _edge,
)


def startup_addresses(startup: str, *, include_loopback=False):
    interfaces = r"(?:eth|br)[0-9]+" + (r"|lo" if include_loopback else "")
    return re.findall(r"ip addr add ([0-9./]+) dev (" + interfaces + r")\b", startup)


def startup_static_routes(startup: str):
    return [
        ("0.0.0.0/0" if prefix == "default" else prefix, gateway)
        for prefix, gateway in re.findall(
            r"^ip route add (default|[0-9./]+) via ([0-9.]+)(?: dev (?:eth|br)[0-9]+)?$",
            startup,
            flags=re.MULTILINE,
        )
    ]


def export_native_routers(env, root: Path, configs, shutdown, endpoints=None):
    """Write each native router's concrete configuration for its vendor parser.

    Returns ``{router: {native interface: Linux interface}}`` aliases. Router
    addresses are added to ``endpoints`` when given. Routers analyzed through
    their FRR design are exported with the FRR routers instead.
    """
    from nika.net_env.native_routers import design_analyzed_routers, native_platform

    aliases = {}
    design_analyzed = design_analyzed_routers(env)
    for name, profile in sorted(getattr(env, "native_routers", {}).items()):
        if name in design_analyzed:
            continue
        validation = native_platform(profile).validation
        if validation is None:
            raise ValueError(f"No validation projection for native profile {profile}")
        config = (
            configs[name]
            if configs is not None and name in configs
            else env.deployment_configs[name]
        )
        disabled = [validation.interface(i) for n, i in shutdown if n == name]
        node = validation.node(name, config)
        aliases[name] = {validation.interface(i.name): i.name for i in node.interfaces}
        content = validation.batfish_config(config, disabled)
        addresses = [i.address for i in node.interfaces]
        if endpoints is not None:
            endpoints.update(addresses)
        (root / "configs" / f"{name}.cfg").write_text(content)
    return aliases


def link_edges(env, routers, aliases):
    """Layer-1 edges between exported routers, under each router's own names."""
    names = {name: {v: k for k, v in alias.items()} for name, alias in aliases.items()}
    edges = []
    for link in env.lab.links.values():
        endpoints = [
            (name, names[name].get(f"eth{number}") if name in names else f"eth{number}")
            for name in link.machines
            if name in routers or name in names
            for number, interface in env.lab.machines[name].interfaces.items()
            if interface.link.name == link.name
        ]
        if len(endpoints) == 2 and all(interface for _, interface in endpoints):
            edges.append(_edge(*endpoints[0], *endpoints[1]))
    return edges


def build_kathara_snapshot(
    env, contract, root: Path, *, configs=None, shutdown=(), routing_model="frr"
):
    if routing_model not in {"frr", "linux-static"}:
        raise ValueError("Unsupported routing projection")
    default_endpoints = set()
    root = prepare_snapshot(root)
    (root / "configs").mkdir(parents=True, exist_ok=True)
    (root / "batfish").mkdir(exist_ok=True)
    from nika.net_env.native_routers import design_analyzed_routers

    routers = {
        name: machine
        for name, machine in env.lab.machines.items()
        if machine.fs is not None and machine.fs.exists("etc/frr/frr.conf")
    }
    native = dict(getattr(env, "native_routers", {}))
    if not routers and not native:
        raise ValueError("No concrete FRR configurations available.")
    # Routers without a Batfish parser are analyzed as FRR from their design:
    # Linux addressing from the design startup, routing from the FRR rendering.
    design_analyzed = design_analyzed_routers(env)
    if (
        design_analyzed
        and routing_model == "frr"
        and (configs is None or not set(design_analyzed) <= set(configs))
    ):
        raise ValueError("Design-analyzed routers require their FRR rendering")
    routers.update({name: env.lab.machines[name] for name in design_analyzed})
    aliases = export_native_routers(env, root, configs, shutdown, default_endpoints)
    for name, machine in routers.items():
        startup = (
            env.native_sources[name][1]
            if name in design_analyzed
            else env.lab.fs.readtext(f"{name}.startup")
        )
        addresses = startup_addresses(startup)
        if not addresses:
            raise ValueError(f"No supported static Ethernet addressing on {name}")
        vrf_members = dict(
            re.findall(r"ip link set dev (eth[0-9]+) master (vrf_[a-z0-9_]+)", startup)
        )
        if vrf_members and routing_model == "frr":
            raise ValueError(
                "VRF interface attachment is outside this Ethernet FRR exporter; declare a scoped linux-static model with runtime overlay requirements"
            )
        if routing_model == "linux-static":
            addresses = [
                (address, interface)
                for address, interface in addresses
                if interface not in vrf_members
            ]
        default_endpoints.update(
            str(IPv4Interface(address).ip) for address, _ in addresses
        )
        interfaces = "".join(
            f"interface {interface}\n ip address {address}\n"
            + (" shutdown\n" if (name, interface) in shutdown else "")
            + "!\n"
            for address, interface in addresses
        )
        config = (
            configs[name]
            if configs is not None
            else f"hostname {name}\n"
            if name in design_analyzed
            else machine.fs.readtext("etc/frr/frr.conf")
        )
        if getattr(env, "rpki", False):
            node_plan = next(n for n in env.bgp_plan.nodes if n.device_name == name)
            if node_plan.rpki_cache:
                from nika.validation.batfish.rpki import project_frr_rpki

                rtr = env.bgp_plan.inventory["rpki_rtr"]
                slurm = json.loads(
                    env.lab.machines[rtr["machine"]].fs.readtext("tmp/slurm.json")
                )
                config = project_frr_rpki(config, slurm, cache=node_plan.rpki_cache)
        if routing_model == "linux-static":
            if configs is not None:
                raise ValueError(
                    "FRR mutations cannot be validated against a Linux static-routing projection"
                )
            config = f"hostname {name}\n"
        static_routes = startup_static_routes(startup)
        config += "".join(
            f"\nip route {prefix} {gateway}\n" for prefix, gateway in static_routes
        )
        config = normalize_frr_for_batfish(config)
        (root / "configs" / f"{name}.cfg").write_text(
            _cumulus_frr_snapshot(
                name,
                "frr version 10.5\nfrr defaults traditional\n" + interfaces + config,
            )
        )
    if routing_model == "linux-static":
        for intent in contract.intents:
            if (
                intent.property not in {"reachability", "isolation"}
                or intent.source.address not in default_endpoints
                or intent.destination.address not in default_endpoints
            ):
                raise ValueError(
                    "Linux static projection only verifies explicitly addressed default-table router endpoints; overlay and VRF requirements need runtime coverage"
                )
    edges = link_edges(env, routers, aliases)
    (root / "batfish" / "layer1_topology.json").write_text(json.dumps({"edges": edges}))
    return ValidationSnapshot(
        snapshot_id=contract.contract_id,
        path=root,
        metadata={
            "routers": sorted({*routers, *native}),
            "routing_model": routing_model,
            **(
                {
                    "native_routers": native,
                    "native_config_analysis": True,
                    "interface_aliases": aliases,
                }
                if native
                else {}
            ),
            **(
                {
                    "rpki_model": "RFC 6811 under the Routinator SLURM assertions; synchronized RTR and AS_SEQUENCE paths assumed; runtime checks cache and actual route admission"
                }
                if getattr(env, "rpki", False)
                else {}
            ),
            "scope": "Linux default-table connected/static router endpoints"
            if routing_model == "linux-static"
            else "FRR L3 router/gateway interfaces; no host/DHCP/bridge-forwarding/tunnel inference",
        },
    )
