"""Host-side wires for Containerlab links.

Containerlab joins two nodes with one veth pair whose ends both sit in node
namespaces, so a link failure would have to act inside a node, where the
agent can see it. NIKA instead deploys each node-to-node link as two veths
into the host namespace and joins their host ends with a ``tc mirred``
redirect in each direction. Node interfaces keep their names and addresses;
the host ends give link failures a place outside every node, as Kathara's
bridges do (see ``nika.service.containerlab.host_tc``).
"""

from __future__ import annotations

import hashlib
import shlex
import subprocess
from pathlib import Path
from typing import Any

import yaml

WIRED_SUFFIX = ".wired.clab.yml"
_HOST_CMD_TIMEOUT_SEC = 120.0
# Extra join budget per wire; large SNDlib graphs run thousands of netlink calls.
_HOST_CMD_TIMEOUT_PER_WIRE_SEC = 0.25


def wire_end_name(lab_name: str, endpoint: str) -> str:
    """Host-side interface name of ``node:iface`` (unique per lab, 14 characters)."""
    digest = hashlib.blake2s(f"{lab_name}|{endpoint}".encode(), digest_size=6)
    return f"nw{digest.hexdigest()}"


def wire_links(
    document: dict[str, Any], lab_name: str
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """Return the document with node-to-node links wired through the host.

    Returns the rewritten document and the host-end pairs to join.
    """
    topology = document.get("topology") or {}
    nodes = set(topology.get("nodes") or {})
    links, wires = [], []
    for link in topology.get("links") or []:
        endpoints = link.get("endpoints") or []
        names = [str(endpoint).split(":", 1)[0] for endpoint in endpoints]
        if len(endpoints) != 2 or not set(names) <= nodes:
            links.append(link)
            continue
        extra = {key: value for key, value in link.items() if key != "endpoints"}
        ends = []
        for endpoint in endpoints:
            end = wire_end_name(lab_name, endpoint)
            links.append({"endpoints": [endpoint, f"host:{end}"], **extra})
            ends.append(end)
        wires.append((ends[0], ends[1]))
    wired = {**document, "topology": {**topology, "links": links}}
    return wired, wires


def write_wired_topology(
    topology_file: Path, lab_name: str
) -> tuple[Path, list[tuple[str, str]]]:
    """Write the wired copy next to ``topology_file`` and return it with its wires."""
    document = yaml.safe_load(topology_file.read_text(encoding="utf-8"))
    wired, wires = wire_links(document, lab_name)
    path = topology_file.with_name(topology_file.name.split(".")[0] + WIRED_SUFFIX)
    path.write_text(yaml.safe_dump(wired, sort_keys=False), encoding="utf-8")
    return path, wires


def join_script(wires: list[tuple[str, str]]) -> str:
    """Shell that brings each host end up and redirects it to its partner."""
    lines = ["set -e"]
    for pair in wires:
        for source, target in (pair, pair[::-1]):
            src, dst = shlex.quote(source), shlex.quote(target)
            lines += [
                f"ip link set dev {src} up",
                # clsact (not ingress) leaves the egress hook free for
                # failures that act on what a wire end delivers.
                f"tc qdisc replace dev {src} clsact",
                f"tc filter replace dev {src} ingress prio 1 matchall "
                f"action mirred egress redirect dev {dst}",
            ]
    return "\n".join(lines) + "\n"


def join_timeout_sec(wire_count: int) -> float:
    """Return the sudo budget for ``wire_count`` host-end pairs."""
    return _HOST_CMD_TIMEOUT_SEC + _HOST_CMD_TIMEOUT_PER_WIRE_SEC * max(wire_count, 0)


def join_wires(wires: list[tuple[str, str]]) -> None:
    """Forward every frame between the two host ends of each wire.

    The script is passed on stdin. Linux caps one argv string at
    ``MAX_ARG_STRLEN`` (32 pages, 128 KiB), and a large SNDlib graph's
    ``sh -c`` script exceeds that (``E2BIG``).
    """
    script = join_script(wires)
    timeout = join_timeout_sec(len(wires))
    try:
        result = subprocess.run(
            ["sudo", "-n", "sh"],
            input=script,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Joining Containerlab wires timed out after {timeout:.0f}s"
        ) from exc
    if result.returncode:
        raise RuntimeError(f"Joining Containerlab wires failed: {result.stderr}")
