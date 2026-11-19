---
name: dns-fault-skill
description: Identify DNS server-side faults — wrong record, daemon down, port 53 filtered, or injected lookup latency. Use when hostname-based access fails or is slow while direct-IP access to the same target works.
---

# DNS Server-Side Faults

## What These Faults Mean

- `dns_record_error`: the DNS server answers, but the record points to an IP the topology never declared.
- `dns_service_down`: the DNS daemon (`named`) is absent or not listening on port 53, so the server cannot answer queries.
- `dns_port_blocked`: the daemon is alive but `nft`/`iptables` drops port 53 traffic.
- `dns_lookup_latency`: a `tc` qdisc (`netem delay` or `tbf`) on the DNS server's egress adds large per-query latency while record/listener/firewall all look healthy.

## Exit conditions

- If only one host's `/etc/resolv.conf` differs from peers and the DNS server itself is healthy, exit to `host-ip-skill` and submit `host_incorrect_dns` there. That RCA is NOT owned by this skill.
- If `dhcpd.conf` on the DHCP server pushes the wrong nameserver and affected hosts show that exact wrong resolver, exit to `dhcp-fault-skill` for `dhcp_option_dns`.

## Guardrails

- Do not call `dns_service_down` from query timeout alone; the timeout could be a firewall block or a qdisc delay. Check process, listener, AND firewall before naming `dns_service_down`.
- `dns_lookup_latency` needs the actual qdisc evidence on the DNS server's egress interface. High latency alone is not a fingerprint.
- One healthy client does not clear a server-side DNS fault.
