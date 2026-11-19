---
name: bgp-fault-skill
description: Identify BGP routing faults — ASN misconfiguration, missing route advertisement, prefix hijacking, blackhole route leak. Use when reachability failures follow BGP-routed paths, BGP sessions fail to establish, or expected prefixes are missing from peers.
---

# BGP Faults

## What These Faults Mean

- `bgp_asn_misconfig`: the router's local ASN does not match what its peers expect, so BGP sessions fail to establish.
- `bgp_missing_route_advertisement`: a `network` statement for a locally-attached prefix is missing from the router's `router bgp` block, so peers never learn that prefix.
- `bgp_hijacking`: an unauthorized router advertises a prefix, diverting traffic from its legitimate origin.
- `bgp_blackhole_community_leak`: a blackhole community propagates beyond its intended scope and causes traffic to be dropped.
- `host_static_blackhole`: a static blackhole route drops traffic to a prefix locally.

## Required tool usage

- Use `device_read` with `host_name` and a list of commands on Linux/FRR routers. Use the platform CLI tool on native routers.
- Compare configuration and routing state with peer observations. A clean configuration alone does not show that a daemon is running.

## Guardrails

- A route being "missing or surprising" is not enough. You need direct config or routing-table evidence on the offending router.
- For `bgp_hijacking`, compare the advertised prefix and origin with the legitimate owner's route and peer observations.
- For `bgp_blackhole_community_leak`, inspect the advertised community and its propagation. A local static blackhole alone does not establish a community leak.
- For `host_static_blackhole`, inspect the local route and its effect on the affected prefix.
