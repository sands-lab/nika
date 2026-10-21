---
name: ospf-fault-skill
description: Distinguish OSPF and FRR routing faults - ospf_neighbor_missing, ospf_area_misconfiguration, ospf_acl_block, and frr_service_down. Use when reachability failures follow routing paths, OSPF neighbors are missing, or routing daemons are suspect.
---

# OSPF And FRR Faults

## What These Faults Mean

- `frr_service_down`: the routing daemon stack is not running, even if config files still look valid.
- `ospf_neighbor_missing`: the router is not correctly participating in OSPF adjacency or route origination, often because expected `network` statements are missing.
- `ospf_area_misconfiguration`: a specific adjacency-carrying link is placed in the wrong OSPF area relative to peer or topology expectations.
- `ospf_acl_block`: router firewall policy blocks OSPF packets even though interfaces and config may look otherwise healthy.

## Leading Signals

- One router or one routed segment loses paths while interfaces remain present.
- `show ip ospf neighbor` and `show running-config` prove adjacency and area state directly.
- FRR process checks separate dead control plane from bad config.

## Exact Calls

- `exec_shell(host_name="<router>", command="ps aux | grep -E 'zebra|ospfd|watchfrr' | grep -v grep")`
- `exec_shell(host_name="<router>", command="vtysh -c 'show ip ospf neighbor'")`
- `exec_shell(host_name="<router>", command="vtysh -c 'show running-config'")`
- `exec_shell(host_name="<router>", command="nft list ruleset")`
- `frr_show_running_config(router_name="<router>")`
- `frr_show_ip_route(router_name="<router>")`

## Required Tool Usage

- FRR MCP tools use `router_name`, not `host_name`.
- If you are unsure of the FRR helper schema, use `exec_shell(host_name="<router>", command="vtysh -c '...'")`.
- `frr_show_running_config` and `frr_show_ip_route` are secondary evidence only; they can look healthy when FRR is down.

## Submit on match

When direct evidence on a router (process state, OSPF config, area assignment, or `nft` rule) is sufficient to identify the fault, submit. Do not chain extra probes once the family is confirmed.

## Guardrails

- Do not infer `ospf_area_misconfiguration` from LSDB differences alone.
- You need direct config evidence for the wrong area on a specific link.
- Clean service checks do not clear silent OSPF faults on redundant topologies.
- If hosts lose DHCP renewal behind one router, keep OSPF in play.
- Healthy OSPF state does not clear L2 identity faults: duplicate MACs can create intermittent or `status=unknown` routed symptoms while FRR and routes still look healthy, so compare device MAC addresses through MCP before concluding no anomaly.
