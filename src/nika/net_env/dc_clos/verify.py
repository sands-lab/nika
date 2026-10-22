"""Startup and full verification for the unified DC Clos scenario.

Both scale with the fabric: every node is deployed, every configured eBGP
session is Established with its planned peer, and (full check) every client
reaches every pod's DNS server and fetches every web service by name.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from nika.net_env.verify import (
    FRR_BGP_PEERS,
    BgpPeerQuery,
    build_lab_verify_result,
    exec_or_empty,
    host_has_ipv4,
    nodes_deployed,
    ping_unreachable,
)
from nika.runtime.base import LabRuntime
from nika.net_env.verify_collection import (
    VerifyCommand,
    collect_verify_commands,
    mesh_ping_command,
)
from nika.utils.parallel import bounded_parallel_map


def _bgp_failures(
    runtime: LabRuntime,
    bgp_neighbors: Mapping[str, Sequence[str]],
    bgp_peers: BgpPeerQuery,
) -> dict[str, list[str]]:
    """Return planned peers that are not Established, by router."""

    def missing(item: tuple[str, Sequence[str]]) -> list[str]:
        router, peers = item
        summary = exec_or_empty(runtime, router, bgp_peers.command, timeout=20)
        return sorted(set(peers) - bgp_peers.established_peers(summary))

    items = sorted(bgp_neighbors.items())
    return {
        router: peers
        for (router, _), peers in zip(
            items, bounded_parallel_map(missing, items), strict=True
        )
        if peers
    }


def _client_ipv4_failures(
    runtime: LabRuntime, client_addresses: Mapping[str, str]
) -> list[str]:
    return [
        client
        for client, address in sorted(client_addresses.items())
        if not host_has_ipv4(runtime, client, address)
    ]


def _service_commands(dns_servers, web_urls):
    return [
        ("dns", address, f"dig +short +time=2 +tries=2 @{address} {name} A")
        for address, name in dns_servers
    ] + [
        (
            "http",
            url,
            f"curl -s -o /dev/null -w '%{{http_code}}' --connect-timeout 3 -m 10 {url}/",
        )
        for url in web_urls
    ]


def _collect(
    runtime,
    bgp_neighbors,
    client_addresses,
    bgp_peers,
    dns_servers=(),
    web_urls=(),
):
    commands = [VerifyCommand(router, bgp_peers.command) for router in bgp_neighbors]
    for client in client_addresses:
        commands.append(VerifyCommand(client, "ip -4 -o addr show dev eth0", 10))
        commands.extend(
            VerifyCommand(client, command, 20)
            for _, _, command in _service_commands(dns_servers, web_urls)
        )
        commands.extend(
            VerifyCommand(client, mesh_ping_command(address), 30)
            for address, _ in dns_servers
        )
    return collect_verify_commands(runtime, commands)


def _client_service_failures(
    runtime: LabRuntime,
    client_addresses: Mapping[str, str],
    dns_servers: Sequence[tuple[str, str]],
    web_urls: Sequence[str],
) -> dict[str, dict[str, list[str]]]:
    """Return unreachable DNS servers and web URLs from every client."""

    def probe(client: str) -> dict[str, list[str]]:
        seen = {
            (kind, key): exec_or_empty(runtime, client, command, timeout=20).strip()
            for kind, key, command in _service_commands(dns_servers, web_urls)
        }
        return {
            "ping": ping_unreachable(
                runtime, client, [address for address, _ in dns_servers]
            ),
            "dns": [
                address
                for address, _ in dns_servers
                if seen.get(("dns", address)) != address
            ],
            "http": [url for url in web_urls if seen.get(("http", url)) != "200"],
        }

    clients = sorted(client_addresses)
    results = dict(zip(clients, bounded_parallel_map(probe, clients), strict=True))
    return {
        client: {kind: bad for kind, bad in failed.items() if bad}
        for client, failed in results.items()
        if any(failed.values())
    }


def verify_dc_clos_lab_startup(
    runtime: LabRuntime,
    *,
    scenario_name: str,
    nodes: Sequence[str],
    bgp_neighbors: Mapping[str, Sequence[str]],
    client_addresses: Mapping[str, str],
    bgp_peers: BgpPeerQuery = FRR_BGP_PEERS,
) -> dict[str, Any]:
    """Readiness: all nodes deployed, all eBGP sessions up, clients addressed."""
    runtime = _collect(runtime, bgp_neighbors, client_addresses, bgp_peers)
    bgp_failures = _bgp_failures(runtime, bgp_neighbors, bgp_peers)
    ipv4_failures = _client_ipv4_failures(runtime, client_addresses)
    checks = {
        "nodes_deployed": nodes_deployed(runtime, nodes),
        "bgp_sessions_established": not bgp_failures,
        "client_ipv4": not ipv4_failures,
    }
    return runtime.finish(
        build_lab_verify_result(
            scenario_name=scenario_name,
            verified=all(checks.values()),
            checks=checks,
            details={
                "node_count": len(nodes),
                "bgp_session_count": sum(
                    len(peers) for peers in bgp_neighbors.values()
                ),
                "failures": {
                    "bgp_not_established": bgp_failures,
                    "client_ipv4": ipv4_failures,
                },
            },
        )
    )


def verify_dc_clos_lab(
    runtime: LabRuntime,
    *,
    scenario_name: str,
    nodes: Sequence[str],
    bgp_neighbors: Mapping[str, Sequence[str]],
    client_addresses: Mapping[str, str],
    dns_servers: Sequence[tuple[str, str]],
    web_urls: Sequence[str],
    bgp_peers: BgpPeerQuery = FRR_BGP_PEERS,
) -> dict[str, Any]:
    """Startup checks plus every client reaching every pod's services."""
    runtime = _collect(
        runtime, bgp_neighbors, client_addresses, bgp_peers, dns_servers, web_urls
    )
    bgp_failures = _bgp_failures(runtime, bgp_neighbors, bgp_peers)
    ipv4_failures = _client_ipv4_failures(runtime, client_addresses)
    service_failures = _client_service_failures(
        runtime, client_addresses, dns_servers, web_urls
    )

    def clean(kind: str) -> bool:
        return not any(kind in failed for failed in service_failures.values())

    checks = {
        "nodes_deployed": nodes_deployed(runtime, nodes),
        "bgp_sessions_established": not bgp_failures,
        "client_ipv4": not ipv4_failures,
        "dns_servers_reachable": clean("ping"),
        "dns_servers_answer": clean("dns"),
        "web_http_by_name": clean("http"),
    }
    return runtime.finish(
        build_lab_verify_result(
            scenario_name=scenario_name,
            verified=all(checks.values()),
            checks=checks,
            details={
                "node_count": len(nodes),
                "bgp_session_count": sum(
                    len(peers) for peers in bgp_neighbors.values()
                ),
                "clients": sorted(client_addresses),
                "dns_servers": [address for address, _ in dns_servers],
                "web_urls": list(web_urls),
                "failures": {
                    "bgp_not_established": bgp_failures,
                    "client_ipv4": ipv4_failures,
                    "services": service_failures,
                },
            },
        )
    )
