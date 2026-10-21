---
name: acl-skill
description: Identify ACL and firewall faults from nftables or iptables rules. Use when traffic is selectively blocked by protocol or service (ARP, ICMP, HTTP, DNS, BGP, or packet-size filtering).
---

# ACL And Firewall Faults

## What This Family Means

- `arp_acl_block`: the device firewall blocks ARP itself, so neighbor discovery fails before normal IP traffic can work.
- `icmp_acl_block`: the device drops ICMP, so ping-based reachability looks broken even if some non-ICMP traffic could still work.
- `http_acl_block`: TCP port 80 is blocked on the affected device or path.
- `dns_port_blocked`: DNS packets on port 53 are blocked by firewall policy.
- `ospf_acl_block`: OSPF control-plane packets are blocked on a router even though interfaces and config may look normal.
- `bgp_acl_block`: TCP port 179 is blocked, so BGP traffic cannot form or sustain the session.
- `link_fragmentation_disabled`: packet-size filtering drops larger packets. This is a firewall/filter rule, not a physical link failure.

## Leading Signals

- The symptom is selective by protocol, not a full device outage.
- `nft list ruleset` or `iptables -L -n -v` shows a direct drop rule that matches the broken traffic.
- One implicated host can be healthy locally while the shared router path still owns the actual drop rule.

## Exact Calls

- `exec_shell(host_name="<flagged-device>", command="nft list ruleset")`
- `exec_shell(host_name="<flagged-device>", command="iptables -L -n -v")`

Inspect the implicated devices through MCP and match firewall rules against the affected protocol and path.

## Host Vs Router Ownership

- If one host is the repeated suspicious source across many destinations, check that host first.
- If many sources converge on one destination or one small destination set, inspect that destination once, then check the first shared router or core router with `nft list ruleset`.
- A clean destination-host ruleset does not clear `icmp_acl_block`; the matching drop rule may live on a router on the shared path.

## Guardrails

- Prefer `nft list ruleset`; many real drops are invisible in `iptables -L`.
- Check the affected host or first implicated router before broadening.
- For widespread ICMP-only `unknown` rows, prioritize one router ACL check before OSPF or service-layer expansion.
- ARP symptoms alone are not enough for `arp_cache_poisoning`; inspect `nft` first.
- The faulty device is the device that owns the matching drop rule.
