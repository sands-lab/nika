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
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

import yaml

WIRED_SUFFIX = ".wired.clab.yml"
_HOST_CMD_TIMEOUT_SEC = 120.0
# Extra join budget per wire; large SNDlib graphs run thousands of netlink calls.
_HOST_CMD_TIMEOUT_PER_WIRE_SEC = 0.25
_WIRE_HOST_END_RE = re.compile(r"^host:(nw[0-9a-f]{12})$")
_IP_LINK_NAME_RE = re.compile(r"^\d+:\s+([^:@\s]+)")


def wire_end_name(lab_name: str, endpoint: str) -> str:
    """Host-side interface name of ``node:iface`` (unique per lab, 14 characters)."""
    digest = hashlib.blake2s(f"{lab_name}|{endpoint}".encode(), digest_size=6)
    return f"nw{digest.hexdigest()}"


def wired_topology_path(topology_file: Path) -> Path:
    """Return the wired topology path written next to ``topology_file``."""
    return topology_file.with_name(topology_file.name.split(".")[0] + WIRED_SUFFIX)


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
    path = wired_topology_path(topology_file)
    path.write_text(yaml.safe_dump(wired, sort_keys=False), encoding="utf-8")
    return path, wires


def wire_pairs_from_document(document: dict[str, Any]) -> list[tuple[str, str]]:
    """Return host-end pairs from a wired topology document.

    ``write_wired_topology`` emits two consecutive node-to-host links for each
    original node-to-node link. Those host ends form one wire pair. Links that
    were already attached to the host stay unpaired.
    """
    topology = document.get("topology") or {}
    pending: str | None = None
    wires: list[tuple[str, str]] = []
    for link in topology.get("links") or []:
        found = None
        for endpoint in link.get("endpoints") or []:
            match = _WIRE_HOST_END_RE.match(str(endpoint))
            if match:
                found = match.group(1)
                break
        if found is None:
            continue
        if pending is None:
            pending = found
        else:
            wires.append((pending, found))
            pending = None
    return wires


def load_wired_pairs(topology_file: Path) -> list[tuple[str, str]]:
    """Derive host-end pairs from the wired topology next to ``topology_file``."""
    path = wired_topology_path(topology_file)
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return wire_pairs_from_document(document)


def wire_ends_for_topology(topology_file: Path, lab_name: str) -> list[str]:
    """Host-end names from ``wire_end_name`` for this lab's node-to-node links."""
    if not topology_file.is_file():
        return []
    document = yaml.safe_load(topology_file.read_text(encoding="utf-8")) or {}
    _, wires = wire_links(document, lab_name)
    return [end for pair in wires for end in pair]


def host_iface_names(ip_link_output: str) -> set[str]:
    """Return interface names from ``ip -o link show`` output."""
    names: set[str] = set()
    for line in ip_link_output.splitlines():
        match = _IP_LINK_NAME_RE.match(line.strip())
        if match:
            names.add(match.group(1))
    return names


def leftover_wire_ends(ends: list[str], ip_link_output: str) -> list[str]:
    """Return this lab's host wire ends that still exist on the host."""
    present = host_iface_names(ip_link_output)
    return [end for end in ends if end in present]


def delete_script(ends: list[str]) -> str:
    """Shell that deletes each host interface in ``ends``."""
    lines = ["set -e"]
    for end in ends:
        lines.append(f"ip link delete dev {shlex.quote(end)}")
    return "\n".join(lines) + "\n"


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


def _run_host_script(script: str, *, timeout: float, what: str) -> None:
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
        raise RuntimeError(f"{what} timed out after {timeout:.0f}s") from exc
    if result.returncode:
        raise RuntimeError(f"{what} failed: {result.stderr}")


def join_wires(wires: list[tuple[str, str]]) -> None:
    """Forward every frame between the two host ends of each wire.

    The script is passed on stdin. Linux caps one argv string at
    ``MAX_ARG_STRLEN`` (32 pages, 128 KiB), and a large SNDlib graph's
    ``sh -c`` script exceeds that (``E2BIG``).
    """
    if not wires:
        return
    _run_host_script(
        join_script(wires),
        timeout=join_timeout_sec(len(wires)),
        what="Joining Containerlab wires",
    )


def remove_leftover_wire_ends(ends: list[str]) -> list[str]:
    """Delete leftover host wire ends of this lab; return the names removed."""
    if not ends:
        return []
    result = subprocess.run(
        ["ip", "-o", "link", "show"],
        capture_output=True,
        text=True,
        check=False,
        timeout=_HOST_CMD_TIMEOUT_SEC,
    )
    if result.returncode:
        raise RuntimeError(f"Listing host links failed: {result.stderr}")
    leftover = leftover_wire_ends(ends, result.stdout)
    if not leftover:
        return []
    _run_host_script(
        delete_script(leftover),
        timeout=join_timeout_sec(len(leftover)),
        what="Removing leftover Containerlab wires",
    )
    return leftover
