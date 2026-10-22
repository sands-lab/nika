---
name: host-crash-skill
description: Identify a device that has been killed, stopped, or removed from the topology. Use when a device is completely unresponsive — ICMP fails, shell never opens, no service response — while same-role peers stay clean.
---

# host_crash

## Fingerprint (all three must be true → submit)

- ICMP to the device's known IP fails. Look up the IP from a peer or the MCP inventory; do not rely on a name that the device itself resolves.
- Shell on the device times out or never opens — MCP shell probes consistently fail on that device.
- Same-role peer baseline stays clean. Other hosts/servers of the same kind remain reachable, the routing fabric is healthy, and no widespread cascade is in play.

All three must hold. Any partial response rules out `host_crash`.

## Submit

- `is_anomaly=True`
- `root_cause_name=["host_crash"]`
- `faulty_devices=["<device>"]`

## Not load_balancer_overload

If the unresponsive device is the load balancer and it still answers ICMP, localhost HTTP, or any partial VIP probe, that is probably `load_balancer_overload`. Use `load-balancer-skill`.

## Not sender/receiver_resource_contention

An isolated `exec_failed`/timeout on one web/app server or one client, while ICMP still answers or same-role peers are otherwise clean, is resource contention — not crash. Use `resource-contention-skill`. `host_crash` requires full silence and a clean same-role baseline.

## Not upstream cascade

If many same-role devices are unreachable together, or the routing/control-plane fabric is broken, the cause is upstream (link, ACL, OSPF/BGP, DHCP relay). Repeat the matching broad-search checks through MCP before submitting `host_crash`.
