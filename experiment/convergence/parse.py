"""Read the node dumps that ``collect`` writes."""

from __future__ import annotations

import json
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any

# Host management networks, not lab forwarding: Docker's default bridge, which
# Kathara "bridged" interfaces join (Docker assigns addresses in attach order),
# and the pools NIKA draws per-lab Containerlab management subnets from
# (nika.net_env.utils.containerlab.mgmt_subnet).
MGMT_NETWORKS = (
    IPv4Network("172.17.0.0/16"),
    IPv4Network("172.100.0.0/16"),
    IPv4Network("172.101.0.0/16"),
)


def is_mgmt(address: str | IPv4Network) -> bool:
    """True for an IPv4 address or prefix inside a host management network."""
    net = (
        address
        if isinstance(address, IPv4Network)
        else IPv4Network(IPv4Address(address))
    )
    return any(net.subnet_of(pool) for pool in MGMT_NETWORKS)


def sections(raw: str) -> dict[str, str]:
    """Split one node's dump into its ``@@@ name`` sections."""
    out: dict[str, list[str]] = {}
    cur = None
    for line in raw.splitlines():
        if line.startswith("@@@ "):
            cur = line[4:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    return {k: "\n".join(v).strip() for k, v in out.items()}


def parse_json(text: str) -> Any:
    """JSON value of a section, or ``None`` when it is empty or not JSON."""
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError:
        return None


def gateway(hop: dict) -> str | None:
    """IPv4 gateway or RFC 5549 IPv6 (link-local) next hop of an ``ip -j route`` hop."""
    return hop.get("gateway") or (hop.get("via") or {}).get("host")


def link_local_owners(nodes: dict[str, dict[str, str]]) -> dict[str, str]:
    """IPv6 link-local address -> owning ``node:interface``.

    Link-local addresses derive from random MACs, so they change on every
    deploy; their owner does not.
    """
    owners = {}
    for node, secs in nodes.items():
        for link in parse_json(secs.get("addr", "")) or []:
            for a in link.get("addr_info", []):
                if str(a.get("local", "")).startswith("fe80"):
                    owners[a["local"]] = f"{node}:{link.get('ifname')}"
    return owners


def last_dump(run_dir: Path) -> Path | None:
    """The run's latest timed dump (``dump_<seconds>.json``)."""
    dumps = [p for p in run_dir.glob("dump_*.json") if p.stem.split("_")[1].isdigit()]
    return max(dumps, key=lambda p: int(p.stem.split("_")[1]), default=None)


def load_runs(root: Path, mode: str) -> dict[str, dict]:
    """Last dump of every finished run of ``root/<mode>``, by run name."""
    runs = {}
    for run_dir in sorted((root / mode).glob("run_*")):
        dump = last_dump(run_dir)
        if dump is not None and (run_dir / "meta.json").exists():
            runs[run_dir.name] = json.loads(dump.read_text())
    return runs


def failed_nodes(doc: dict) -> set[str]:
    """Nodes whose dump failed in this run."""
    return {n for n, v in doc["nodes"].items() if "error" in v}
