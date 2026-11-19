---
name: ospf-fault-skill
description: Distinguish OSPF and routing service faults - ospf_interface_not_enabled, ospf_area_misconfiguration, ospf_acl_block, and routing_service_down (frr_service_down on published releases). Use when reachability failures follow routing paths, OSPF neighbors are missing, or routing daemons are suspect.
---

# OSPF and routing service faults

## What These Faults Mean

- `frr_service_down` on published releases `0.1.0` and `0.2.0`, or `routing_service_down` otherwise: the routing daemon stack is not running, even if config files still look valid.
- `ospf_interface_not_enabled`: the router is not correctly participating in OSPF adjacency or route origination, often because expected `network` statements are missing.
- `ospf_area_misconfiguration`: a specific adjacency-carrying link is placed in the wrong OSPF area relative to peer or topology expectations.
- `ospf_acl_block`: router firewall policy blocks OSPF packets even though interfaces and config may look otherwise healthy.

## Leading Signals

- One router or one routed segment loses paths while interfaces remain present.
- `show ip ospf neighbor` and `show running-config` prove adjacency and area state directly.
- Check process state through the router's platform tools to distinguish a stopped routing service from incorrect configuration.

## Read device state

Use `device_read(host_name="<router>", commands=["vtysh -c 'show ip ospf neighbor'", "vtysh -c 'show running-config'", "nft list ruleset"])` on Linux/FRR routers. Routers do not expose `exec_shell`.

Use the selected platform's CLI tool for native routers: `iosxr_exec`, `routeros_exec`, `srl_exec_cli`, or `sonic_exec_cli`. Compare protocol state, configuration, peer observations, and endpoint reachability. A failed CLI call alone does not prove that the routing service is down.

## Submit on match

When direct evidence on a router (process state, OSPF config, area assignment, or `nft` rule) is sufficient to identify the fault, submit. Do not chain extra probes once the family is confirmed.

## Guardrails

- Do not infer `ospf_area_misconfiguration` from LSDB differences alone.
- You need direct config evidence for the wrong area on a specific link.
- Clean service checks do not clear silent OSPF faults on redundant topologies.
- If hosts lose DHCP renewal behind one router, keep OSPF in play.
- Healthy OSPF state does not clear L2 identity faults: duplicate MACs can create intermittent or `status=unknown` routed symptoms while FRR and routes still look healthy, so compare device MAC addresses through MCP before concluding no anomaly.
