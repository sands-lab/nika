"""Startup and full verification for the unified DC Clos scenario.

Both scale with the fabric: every node is deployed, every configured eBGP
session is Established with its planned peer, and (full check) every client
reaches every pod's DNS server and fetches every web service by name.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping, Sequence
from typing import Any

from nika.net_env.verify import (
    build_lab_verify_result,
    exec_or_empty,
    frr_bgp_established_peers,
    host_has_ipv4,
    nodes_deployed,
    ping_unreachable,
)
from nika.runtime.base import LabRuntime
from nika.utils.parallel import bounded_parallel_map


def _bgp_failures(
    runtime: LabRuntime, bgp_neighbors: Mapping[str, Sequence[str]]
) -> dict[str, list[str]]:
    """Return planned peers that are not Established, by router."""

    def missing(item: tuple[str, Sequence[str]]) -> list[str]:
        router, peers = item
        summary = exec_or_empty(
            runtime, router, "vtysh -c 'show bgp summary'", timeout=20
        )
        return sorted(set(peers) - frr_bgp_established_peers(summary))

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


def _client_probe_script(
    dns_servers: Sequence[tuple[str, str]], web_urls: Sequence[str]
) -> str:
    """One shell run per client: query every DNS server and GET every URL."""
    lines = []
    for address, name in dns_servers:
        lines.append(
            f"echo dns {address} $(dig +short +time=2 +tries=2 @{address} {name} A)"
        )
    for url in web_urls:
        lines.append(
            f"echo http {url} $(curl -s -o /dev/null -w '%{{http_code}}' "
            f"--connect-timeout 3 -m 10 {url}/)"
        )
    return "; ".join(lines)


def _client_service_failures(
    runtime: LabRuntime,
    client_addresses: Mapping[str, str],
    dns_servers: Sequence[tuple[str, str]],
    web_urls: Sequence[str],
) -> dict[str, dict[str, list[str]]]:
    """Return unreachable DNS servers and web URLs from every client."""
    script = _client_probe_script(dns_servers, web_urls)

    def probe(client: str) -> dict[str, list[str]]:
        output = exec_or_empty(
            runtime, client, f"sh -c {shlex.quote(script)}", timeout=120
        )
        seen: dict[tuple[str, str], str] = {}
        for line in output.splitlines():
            fields = line.split(maxsplit=2)
            if len(fields) >= 2 and fields[0] in {"dns", "http"}:
                seen[(fields[0], fields[1])] = fields[2] if len(fields) > 2 else ""
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
) -> dict[str, Any]:
    """Readiness: all nodes deployed, all eBGP sessions up, clients addressed."""
    bgp_failures = _bgp_failures(runtime, bgp_neighbors)
    ipv4_failures = _client_ipv4_failures(runtime, client_addresses)
    checks = {
        "nodes_deployed": nodes_deployed(runtime, nodes),
        "bgp_sessions_established": not bgp_failures,
        "client_ipv4": not ipv4_failures,
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details={
            "node_count": len(nodes),
            "bgp_session_count": sum(len(peers) for peers in bgp_neighbors.values()),
            "failures": {
                "bgp_not_established": bgp_failures,
                "client_ipv4": ipv4_failures,
            },
        },
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
) -> dict[str, Any]:
    """Startup checks plus every client reaching every pod's services."""
    bgp_failures = _bgp_failures(runtime, bgp_neighbors)
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
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details={
            "node_count": len(nodes),
            "bgp_session_count": sum(len(peers) for peers in bgp_neighbors.values()),
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
