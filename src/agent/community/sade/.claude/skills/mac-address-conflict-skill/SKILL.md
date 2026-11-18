---
name: mac-address-conflict-skill
description: Identify duplicate hardware (MAC) addresses on the same network segment. Use when a helper surfaces a concrete duplicate `link/ether` value or an ARP entry that flips between two MACs for one IP.
---

# MAC Address Conflict

## What This Fault Means

- `mac_address_conflict` means two different devices present the same Layer 2 hardware identity on the network.
- This can make ARP, switching, and control-plane behavior unstable or intermittent even when small ping samples sometimes pass.

## Leading Signals

- Two devices show the exact same `link/ether` MAC.
- Witness ARP data flips one MAC across multiple IPs, and direct device-side MAC collection confirms the duplicate.
- The decisive proof is the duplicate MAC itself, not only the downstream symptoms.

## Exact Calls

- `exec_shell(host_name="<host>", command="ip link show")`
- `exec_shell(host_name="<witness_host>", command="arp -n")`

## Required Tool Usage

- Use MCP inventory and direct `ip link show` output to compare devices on the affected segment.
- Direct `link/ether` comparison is stronger than ARP-table symptoms.

## Sweep Rules

- If the affected segment is unknown, collect interface MAC addresses through MCP.
- The default MAC and L2 sweeps must include server-side devices (for example load balancers and application servers), not only hosts, routers, and switches.
- Compare one full Layer 2 domain at a time: hosts, gateway, switch/bridge, then adjacent infrastructure if needed.
- Clean L3-L7 checks increase the priority of direct MAC comparison; they do not clear this fault.

## Guardrails

- Do not rule this out from sampled reachability alone.
- Do not rely on ARP tables alone when direct MAC comparison is available.
- Bridge membership such as `ethX -> br0` is topology information, not MAC-conflict proof by itself.
- Identical `link/ether` values on two different routers or hosts are a real L2 identity fault, not a measurement artifact.
