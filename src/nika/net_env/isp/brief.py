"""Shared agent-facing ISP network brief (protocol metadata).

Live troubleshooting benchmarks typically give agents a short operator brief:
description, role inventory, topology edges, and protocol mode — not device
configs or ground truth. Config and runtime state stay on the tool path.
"""

from __future__ import annotations

from typing import Mapping


def format_isp_network_info(
    base_info: str,
    *,
    inventory: Mapping,
    bgp_mode: str,
    device_profile: str | None = None,
) -> str:
    """Append SNDlib / protocol metadata to a role + topology inventory brief."""
    lines = [
        base_info.rstrip("\n"),
        f"SNDlib topology: {inventory['topology_name']}",
        (
            f"IGP: {inventory['igp']}; metric_strategy: {inventory['metric_strategy']}; "
            f"constant_metric: {inventory['constant_metric']}"
        ),
        f"BGP mode: {bgp_mode}",
    ]
    if device_profile is not None:
        lines.append(f"device_profile: {device_profile}")
    host_n = len(inventory.get("hosts") or [])
    lines.append(
        f"Edge stubs: {host_n} (traffic matrix chosen at `nika traffic run sndlib`)"
    )
    lines.append(
        f"Inventory nodes: {inventory['node_count']}; links: {inventory['link_count']}"
    )
    return "\n".join(lines)
