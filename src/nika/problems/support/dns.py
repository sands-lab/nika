"""DNS helpers shared by naming, policy, and security failures."""

from __future__ import annotations

from nika.problems.support.oracle import Check, OracleStep, addr_step, contains, toward


def dns_host_for_domain(
    net_env, domain: str, fallback: str | None = None
) -> str | None:
    """Return the DNS server that serves ``domain`` (e.g. dns_pod1 -> pod1)."""
    dns_servers = list((getattr(net_env, "servers", None) or {}).get("dns") or [])
    if not dns_servers:
        return fallback
    if len(dns_servers) == 1:
        return dns_servers[0]
    for host in dns_servers:
        # dc_clos: dns_pod{N} owns zone pod{N}
        if host.startswith("dns_") and host[len("dns_") :] == domain:
            return host
    return fallback or dns_servers[0]


def dns_client_steps(
    net_env, host: str, dig_expect: Check, *, evidence: bool
) -> list[OracleStep]:
    """Read DNS server *host*'s address, then ping and query it from a client."""
    client = next((h for h in net_env.hosts or [] if h != host), None)
    if not client:
        return []
    key = f"dns_addr_{host}"
    return [
        addr_step(host, "eth0", key),
        toward(
            client,
            key,
            "ping -c 3 -W 1 {ip} 2>&1 | tail -4",
            f"Check that {client} reaches the DNS host {host}.",
            contains(r"\b[1-9][0-9]* (packets )?received", "probes get replies"),
            timeout=15,
        ),
        toward(
            client,
            key,
            "dig +time=2 +tries=1 @{ip} localhost A 2>&1 "
            "| grep -E 'status|timed out|refused|reached|SERVER' | head -5",
            f"Query the DNS server {host} from {client}.",
            dig_expect,
            evidence=evidence,
            timeout=15,
        ),
    ]
