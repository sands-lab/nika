---
name: load-balancer-skill
description: Identify load-balancer-side service faults. Use when the topology includes a load balancer (nginx, HAProxy) and the LB itself shows pressure — slow VIP, command or helper timeouts on the LB, failed IP lookups, or CPU/socket spikes while backends are healthy.
---

# load_balancer_overload

## Fingerprint (any 2 → submit)

- ICMP to the LB known IP succeeds. Use an IP from the task description, MCP inventory, or a host's resolver; do not rely on a name lookup that itself depends on the LB.
- HTTP/VIP probe from a client fails, times out, or returns an empty body.
- Shell command on the LB times out — `exec_shell` hangs or times out on the LB.

## Submit

- `is_anomaly=True`
- `root_cause_name=["load_balancer_overload"]`
- `faulty_devices=["<load_balancer_device_name>"]`

One direct probe to confirm the pattern is enough. Do not chain extra investigations once two fingerprints match.

## Not host_crash

`host_crash` means the container was killed/stopped/removed: no ICMP, no response of any kind, and the same-role baseline stays clean. Any partial LB response — ICMP reply, partial HTTP, localhost nginx answer — rules out `host_crash`.

## Not web_dos_attack

Switch only if a running HTTP flood tool (`ab`, `wrk`, `hping3`, `hey`, `httperf`, `siege`, `vegeta`, `locust`) is found on an attacker host and targets the LB/VIP. Probe-traffic log entries are not flood evidence.

## Not resource-contention-skill

Stay here when the LB itself is the flagged device. Route to `resource-contention-skill` only when the direct pressure signal is on a non-LB backend or client and the LB stays clean.
