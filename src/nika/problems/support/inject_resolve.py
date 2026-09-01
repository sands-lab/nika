"""Shared helpers for deterministic failure injection parameter resolution."""

from __future__ import annotations

import ipaddress

from nika.runtime.base import LabRuntime


def derive_incorrect_ip(runtime: LabRuntime, host: str, intf: str = "eth0") -> str:
    """Return a wrong CIDR by incrementing the host part of the current address."""
    current = runtime.get_host_ip(host, intf, with_prefix=True)
    if not current:
        raise ValueError(f"Cannot derive incorrect IP: no address on {host}:{intf}")
    network = ipaddress.ip_interface(current)
    prefix = network.network.prefixlen
    host_int = int(network.ip)
    for offset in (1, 2, 3, -1, -2):
        candidate = ipaddress.ip_address(host_int + offset)
        if candidate.is_multicast or candidate.is_reserved or candidate.is_loopback:
            continue
        return f"{candidate}/{prefix}"
    raise ValueError(f"Cannot derive incorrect IP for {host}:{intf}")


def derive_wrong_gateway(runtime: LabRuntime, host: str) -> str:
    """Return a wrong gateway by changing the last octet to 254."""
    gateway = runtime.get_default_gateway(host)
    if not gateway:
        raise ValueError(f"Cannot derive wrong gateway: no default route on {host}")
    parts = gateway.split(".")
    parts[-1] = "254"
    return ".".join(parts)


def resolve_victim_host(runtime: LabRuntime, router: str) -> str:
    """Pick a victim device connected to ``router``.

    Prefer client/pc/host names; otherwise any connected peer that is not an
    obvious fabric switch role. On router-only labs (e.g. ISP city
    slugs), fall back to the lexicographically first connected peer.
    """
    connected = list(runtime.get_connected_devices(router))
    client_hosts = sorted(
        dev for dev in connected if any(key in dev for key in ("client", "pc", "host"))
    )
    if client_hosts:
        return client_hosts[0]
    hosts = sorted(
        dev
        for dev in connected
        if not any(key in dev for key in ("switch", "router", "leaf", "spine"))
    )
    if hosts:
        return hosts[0]
    peers = sorted(dev for dev in connected if dev and dev != router)
    if peers:
        return peers[0]
    raise ValueError(f"No victim host found for router {router}")


def resolve_victim_host_ip(
    runtime: LabRuntime, router: str, *, with_prefix: bool = True
) -> str:
    """Return the data-plane IP (with prefix) for the victim host of ``router``."""
    victim = resolve_victim_host(runtime, router)
    iface = "eth1" if any(key in victim for key in ("client", "pc")) else "eth0"
    ip = runtime.get_host_ip(victim, iface, with_prefix=with_prefix)
    if ip is None:
        # Prefer the iface facing ``router`` when eth0 is not the peering link.
        for candidate in runtime.get_host_interfaces(victim):
            if candidate == "lo":
                continue
            ip = runtime.get_host_ip(victim, candidate, with_prefix=with_prefix)
            if ip is not None:
                break
    if ip is None:
        ip = runtime.get_host_ip(victim, with_prefix=with_prefix)
    if ip is None:
        raise ValueError(f"Cannot resolve IP for victim host {victim!r}")
    return ip


def resolve_intf(runtime: LabRuntime, host: str, intf_name: str = "eth0") -> str:
    """Validate that ``intf_name`` exists on ``host``."""
    interfaces = runtime.get_host_interfaces(host)
    if intf_name not in interfaces:
        raise ValueError(
            f"Interface {intf_name!r} not found on {host}; available: {interfaces}"
        )
    return intf_name
