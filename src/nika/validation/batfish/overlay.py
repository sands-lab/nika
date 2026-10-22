"""Project FRR routers with WireGuard tunnels and Linux VRFs for Batfish.

Restricted scope: concrete startup addresses, default-table static routes, each
declared WireGuard interface as a point-to-point routed link between its two
tunnel addresses, and business VRF subnets leaked into the default BGP table.
The pinned Batfish FRR parser does not support ``import vrf``, so the leak is
lowered to default-table origins; VRF-local tables are not modeled. Encryption, endpoint resolution, NAT and
traffic shaping are runtime concerns and stay outside the model.
"""

from __future__ import annotations

import json
import re
from ipaddress import IPv4Interface
from pathlib import Path

from nika.validation.base import ValidationSnapshot
from nika.validation.batfish.kathara import (
    export_native_routers,
    link_edges,
    startup_addresses,
    startup_static_routes,
)
from nika.validation.batfish.snapshot import (
    _cumulus_frr_snapshot,
    _edge,
    prepare_snapshot,
)


_VRF = re.compile(r"^vrf (\S+)\nexit-vrf\n", re.M)
_VRF_BGP = re.compile(
    r"^router bgp [0-9]+ vrf (\S+)\n(.*?)(?=^router bgp |^line vty|^!\n(?! )|\Z)",
    re.M | re.S,
)
_IMPORT = re.compile(r"^  import vrf (\S+)\n", re.M)


def project_vrf_imports(config: str) -> tuple[str, set[str]]:
    """Lower Linux business VRFs leaked into the default BGP table.

    Each per-VRF instance may only redistribute its connected subnets and import
    the default table. The default instance's ``import vrf`` then originates
    those subnets; the caller binds them as default-table ``network`` origins.
    VRF-local tables and reverse imports are outside this projection.
    """
    vrfs = set(_VRF.findall(config))
    for match in _VRF_BGP.finditer(config):
        body = {
            line.strip()
            for line in match[2].splitlines()
            if line.strip() and not line.strip().startswith("!")
        }
        allowed = {
            "address-family ipv4 unicast",
            "redistribute connected",
            "import vrf default",
            "exit-address-family",
            "no bgp ebgp-requires-policy",
        }
        if (
            match[1] not in vrfs
            or {line for line in body if not line.startswith("bgp router-id ")}
            - allowed
        ):
            raise ValueError(f"Unsupported VRF BGP instance {match[1]}")
    imported = {v for v in _IMPORT.findall(config) if v != "default"}
    if not imported <= vrfs:
        raise ValueError("Default BGP imports an undeclared VRF")
    config = _VRF_BGP.sub("", config)
    config = _VRF.sub("", config)
    config = _IMPORT.sub("", config)
    return config, imported


def build_overlay_snapshot(env, contract, root: Path, *, configs=None, shutdown=()):
    if shutdown:
        raise ValueError("The tunnel overlay projection models healthy state only")
    root = prepare_snapshot(root)
    (root / "configs").mkdir(parents=True, exist_ok=True)
    (root / "batfish").mkdir(exist_ok=True)
    routers = {
        name: machine
        for name, machine in env.lab.machines.items()
        if machine.fs is not None and machine.fs.exists("etc/frr/frr.conf")
    }
    # Native routers (such as XR providers) keep their vendor configuration.
    aliases = export_native_routers(env, root, configs, shutdown)
    tunnels: dict[str, list[tuple[str, str]]] = {}
    tunnel_addresses: dict[str, list[tuple[str, str]]] = {}
    for peer in env.bgp_import_peers():
        wg = routers[peer.node].fs.readtext(f"etc/wireguard/{peer.interface}.conf")
        (address,) = re.findall(r"^Address = ([0-9./]+)$", wg, re.M)
        network = str(IPv4Interface(address).network)
        tunnels.setdefault(network, []).append((peer.node, peer.interface))
        tunnel_addresses.setdefault(peer.node, []).append((address, peer.interface))
    for name, machine in routers.items():
        startup = env.lab.fs.readtext(f"{name}.startup")
        members = dict(
            re.findall(r"ip link set dev (eth[0-9]+) master (vrf_[a-z0-9_]+)", startup)
        )
        config = (
            configs[name]
            if configs is not None
            else machine.fs.readtext("etc/frr/frr.conf")
        )
        config, imported = project_vrf_imports(config)
        lines, origins = [], []
        for address, interface in startup_addresses(startup) + tunnel_addresses.get(
            name, []
        ):
            vrf = members.get(interface)
            if vrf is not None:
                if vrf not in imported:
                    continue
                origins.append(str(IPv4Interface(address).network))
            lines += [f"interface {interface}", f" ip address {address}", "!"]
        if origins:
            (asn,) = set(re.findall(r"^router bgp ([0-9]+)$", config, re.M))
            config += (
                f"router bgp {asn}\n address-family ipv4 unicast\n"
                + "".join(f"  network {prefix}\n" for prefix in sorted(origins))
                + " exit-address-family\n!\n"
            )
        statics = "".join(
            f"ip route {prefix} {gateway}\n"
            for prefix, gateway in startup_static_routes(startup)
        )
        (root / "configs" / f"{name}.cfg").write_text(
            _cumulus_frr_snapshot(
                name,
                "frr version 10.5\nfrr defaults traditional\n"
                + "\n".join(lines)
                + "\n"
                + statics
                + config,
            )
        )
    edges = link_edges(env, routers, aliases)
    for network, endpoints in sorted(tunnels.items()):
        if len(endpoints) != 2:
            raise ValueError(f"Tunnel {network} does not join exactly two interfaces")
        edges.append(_edge(*endpoints[0], *endpoints[1]))
    (root / "batfish" / "layer1_topology.json").write_text(json.dumps({"edges": edges}))
    return ValidationSnapshot(
        snapshot_id=contract.contract_id,
        path=root,
        metadata={
            "routers": sorted({*routers, *aliases}),
            "routing_model": "frr-overlay",
            "native_config_analysis": bool(aliases),
            **({"interface_aliases": aliases} if aliases else {}),
            "scope": "Default-table FRR BGP over WireGuard interfaces projected as routed point-to-point links, with imported business VRF subnets as origins; no VRF-local tables, encryption, NAT or endpoint resolution",
        },
    )
