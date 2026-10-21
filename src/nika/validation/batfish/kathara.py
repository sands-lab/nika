"""Export concrete Kathara FRR routers for independent route analysis.

Address statements are read from concrete startup commands. This exporter accepts
physical Ethernet router links only; it does not infer DHCP, bridges or tunnels.
"""

from __future__ import annotations
import json
import re
from pathlib import Path
from ipaddress import IPv4Interface
from nika.validation.base import ValidationSnapshot
from nika.validation.batfish.snapshot import (
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


def build_kathara_snapshot(
    env, contract, root: Path, *, configs=None, shutdown=(), routing_model="frr"
):
    if routing_model not in {"frr", "linux-static"}:
        raise ValueError("Unsupported routing projection")
    default_endpoints = set()
    root = prepare_snapshot(root)
    (root / "configs").mkdir(parents=True, exist_ok=True)
    (root / "batfish").mkdir(exist_ok=True)
    routers = {
        name: machine
        for name, machine in env.lab.machines.items()
        if machine.fs is not None and machine.fs.exists("etc/frr/frr.conf")
    }
    if not routers:
        raise ValueError("No concrete FRR configurations available.")
    for name, machine in routers.items():
        startup = env.lab.fs.readtext(f"{name}.startup")
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
        # FRR accepts global maximum-paths as IPv4-unicast shorthand; Batfish
        # parses its explicit address-family form. Keep the numeric limit.
        config = re.sub(
            r"^ maximum-paths ([0-9]+)$",
            r" address-family ipv4 unicast\n  maximum-paths \1\n exit-address-family",
            config,
            flags=re.MULTILINE,
        )
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
    edges = []
    for link in env.lab.links.values():
        endpoints = [
            (name, f"eth{number}")
            for name in link.machines
            if name in routers
            for number, interface in routers[name].interfaces.items()
            if interface.link.name == link.name
        ]
        if len(endpoints) == 2:
            edges.append(_edge(*endpoints[0], *endpoints[1]))
    (root / "batfish" / "layer1_topology.json").write_text(json.dumps({"edges": edges}))
    return ValidationSnapshot(
        snapshot_id=contract.contract_id,
        path=root,
        metadata={
            "routers": sorted(routers),
            "routing_model": routing_model,
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
