"""Generate Nokia SR Linux configuration for ISP routers."""

from __future__ import annotations

from typing import Any

import yaml

from nika.net_env.isp.igp.frr import isis_net_from_router_id
from nika.net_env.isp.igp.ifaces import srl_ethernet_name, srl_subinterface
from nika.net_env.isp.igp.plan import PlannedInterface, PlannedNode


def render_srl_node_config(
    node: PlannedNode,
    *,
    igp: str,
    interfaces: tuple[PlannedInterface, ...] | None = None,
    bgp_block: dict[str, Any] | None = None,
    extra_loopback_addrs: tuple[str, ...] = (),
    include_routing_policy: bool = False,
) -> str:
    """Render one SRL gnmic update-file YAML for an ISP router."""
    ifaces = interfaces if interfaces is not None else node.interfaces
    if igp not in ("isis", "ospf"):
        raise ValueError(f"Unsupported IGP for SRL render: {igp!r}")

    interface_rows: list[dict[str, Any]] = []
    ni_ifaces: list[dict[str, str]] = [{"name": "system0.0"}]
    for iface in ifaces:
        eth = srl_ethernet_name(iface.name)
        sub = srl_subinterface(iface.name)
        interface_rows.append(
            {
                "name": eth,
                "admin-state": "enable",
                "subinterface": [
                    {
                        "index": 0,
                        "ipv4": {
                            "admin-state": "enable",
                            "address": [
                                {"ip-prefix": f"{iface.address}/{iface.prefixlen}"}
                            ],
                        },
                    }
                ],
            }
        )
        ni_ifaces.append({"name": sub})

    loopback_addrs = [{"ip-prefix": f"{node.loopback}/32"}]
    interface_rows.append(
        {
            "name": "system0",
            "admin-state": "enable",
            "subinterface": [
                {
                    "index": 0,
                    "ipv4": {
                        "admin-state": "enable",
                        "address": loopback_addrs,
                    },
                }
            ],
        }
    )

    # ixr-d2l allows only one IPv4 address per subinterface; put BGP originated
    # prefixes on dedicated lo0 subinterfaces instead of system0.0.
    if extra_loopback_addrs:
        lo_subs: list[dict[str, Any]] = []
        for idx, addr in enumerate(extra_loopback_addrs, start=1):
            prefix = addr if "/" in addr else f"{addr}/32"
            lo_subs.append(
                {
                    "index": idx,
                    "admin-state": "enable",
                    "ipv4": {
                        "admin-state": "enable",
                        "address": [{"ip-prefix": prefix}],
                    },
                }
            )
            ni_ifaces.append({"name": f"lo0.{idx}"})
        interface_rows.append(
            {
                "name": "lo0",
                "admin-state": "enable",
                "subinterface": lo_subs,
            }
        )

    protocols: dict[str, Any] = {}
    if igp == "isis":
        protocols["srl_nokia-isis:isis"] = _isis_block(node, ifaces)
    else:
        protocols["srl_nokia-ospf:ospf"] = _ospf_block(node, ifaces)
    if bgp_block:
        protocols["srl_nokia-bgp:bgp"] = bgp_block

    doc: dict[str, Any] = {
        "srl_nokia-interfaces:interface": interface_rows,
        "srl_nokia-network-instance:network-instance": [
            {
                "name": "default",
                "admin-state": "enable",
                "interface": ni_ifaces,
                "protocols": protocols,
            }
        ],
    }
    if include_routing_policy:
        from nika.net_env.isp.bgp.srl import routing_policy_document

        doc.update(routing_policy_document())
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def _isis_block(node: PlannedNode, interfaces: tuple[PlannedInterface, ...]) -> dict:
    net = isis_net_from_router_id(node.router_id)
    iface_rows: list[dict[str, Any]] = [
        {"interface-name": "system0.0", "passive": True}
    ]
    for iface in interfaces:
        row: dict[str, Any] = {
            "interface-name": srl_subinterface(iface.name),
            "circuit-type": "point-to-point",
            "level": [{"level-number": 2, "metric": iface.metric}],
        }
        if iface.passive:
            row["passive"] = True
        iface_rows.append(row)
    return {
        "instance": [
            {
                "name": "default",
                "admin-state": "enable",
                "level-capability": "L2",
                "net": [net],
                "interface": iface_rows,
                "level": [{"level-number": 2, "metric-style": "wide"}],
            }
        ]
    }


def _ospf_block(node: PlannedNode, interfaces: tuple[PlannedInterface, ...]) -> dict:
    iface_rows: list[dict[str, Any]] = [
        {"interface-name": "system0.0", "passive": True}
    ]
    for iface in interfaces:
        row: dict[str, Any] = {
            "interface-name": srl_subinterface(iface.name),
            "interface-type": "point-to-point",
            "metric": iface.metric,
        }
        if iface.passive:
            row["passive"] = True
        iface_rows.append(row)
    return {
        "instance": [
            {
                "name": "default",
                "admin-state": "enable",
                "version": "ospf-v2",
                "router-id": node.router_id,
                "area": [{"area-id": "0.0.0.0", "interface": iface_rows}],
            }
        ]
    }
