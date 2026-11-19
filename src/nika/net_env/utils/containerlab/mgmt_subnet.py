"""Per-lab Containerlab management subnets.

Every clab topology must use a distinct Docker mgmt ``ipv4-subnet``. Reusing
``172.100.100.0/24`` across labs causes deploy failures and leaves orphan
``br-*`` networks that block later runs until manually removed.
"""

from __future__ import annotations

import functools
import hashlib
import subprocess
from ipaddress import IPv4Network


def _docker_subnets() -> dict[str, str]:
    """IPv4 subnets of the host's Docker networks, mapped to network names."""
    try:
        names = subprocess.run(
            ["docker", "network", "ls", "-q"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.split()
        if not names:
            return {}
        out = subprocess.run(
            [
                "docker",
                "network",
                "inspect",
                "--format",
                "{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}",
                *names,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    used = {}
    for line in out.splitlines():
        name, *subnets = line.split()
        for subnet in subnets:
            used[subnet] = name
    return used


def _free(subnet: str, lab_name: str, used: dict[str, str]) -> bool:
    # A network of this lab keeps its subnet, so later lookups agree.
    return subnet not in used or lab_name in used[subnet]


@functools.cache
def mgmt_third_octet(lab_name: str) -> int:
    """Third octet in 1..254 for ``172.100.<octet>.0/24``.

    Starts from a hash of the lab name and skips subnets another Docker
    network already uses, such as one left by another session's lab.
    """
    digest = hashlib.blake2s(lab_name.encode(), digest_size=2).digest()
    start = digest[0] % 254
    used = _docker_subnets()
    for step in range(254):
        octet = 1 + (start + step) % 254
        if _free(f"172.100.{octet}.0/24", lab_name, used):
            return octet
    raise RuntimeError("No free Containerlab management subnet in 172.100.0.0/16")


def mgmt_ipv4_subnet(lab_name: str) -> str:
    return f"172.100.{mgmt_third_octet(lab_name)}.0/24"


def mgmt_ipv6_subnet(lab_name: str) -> str:
    third = mgmt_third_octet(lab_name)
    return f"3fff:172:100:{third:x}::0/64"


def mgmt_ipv4_address(lab_name: str, host_index: int) -> str:
    if not 2 <= host_index <= 254:
        raise ValueError(f"host_index must be 2..254, got {host_index}")
    return f"172.100.{mgmt_third_octet(lab_name)}.{host_index}"


def mgmt_ipv6_address(lab_name: str, host_index: int) -> str:
    third = mgmt_third_octet(lab_name)
    return f"3fff:172:100:{third:x}::{host_index}"


def mgmt_ipv4_network(lab_name: str, node_count: int) -> IPv4Network:
    """Management subnet with room for ``node_count`` nodes.

    Labs that fit keep the ``/24`` above. Larger labs take a ``/22`` from a
    separate ``172.101.0.0/16`` pool so they never overlap the ``/24`` pool.
    """
    if node_count <= 253:
        return IPv4Network(mgmt_ipv4_subnet(lab_name))
    if node_count > 1021:
        raise ValueError(f"Too many nodes for a management /22: {node_count}")
    start = hashlib.blake2s(lab_name.encode(), digest_size=2).digest()[1] % 64
    used = _docker_subnets()
    for step in range(64):
        subnet = f"172.101.{4 * ((start + step) % 64)}.0/22"
        if _free(subnet, lab_name, used):
            return IPv4Network(subnet)
    raise RuntimeError("No free Containerlab management subnet in 172.101.0.0/16")


def mgmt_ipv4_addresses(lab_name: str, node_count: int) -> list[str]:
    """Addresses for ``node_count`` nodes, skipping the bridge gateway (``.1``)."""
    network = mgmt_ipv4_network(lab_name, node_count)
    return [str(network[index + 2]) for index in range(node_count)]
