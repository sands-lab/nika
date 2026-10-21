---
name: resource-contention-skill
description: Identify per-host resource and load faults — sender or receiver resource contention, sender application delay, web DoS attack. Use when a host shows stress/flood-tool processes, CPU spikes, TCP socket spikes, or slow localhost responses while same-role peers stay clean.
---

# Resource Contention and DoS

## What counts as direct evidence

Use `exec_shell` with `ps` or `pgrep` to identify running stress or HTTP flood processes. Installed binaries without a running process do not count.

One direct probe on the candidate to confirm ownership is enough — do not chain extra investigations once the family is confirmed.

## Not host_crash

`host_crash` means the container was killed/stopped/removed: no response at all, same-role peers clean. An isolated `exec_failed`/timeout on one web/app server while ICMP still answers is `sender_resource_contention`, not `host_crash`. Do not require seeing `stress-ng` in `ps` after the container is already too loaded to answer process probes.

## Not load_balancer_overload

Stay here only when the pressure signal is on a non-LB backend or client. If the flagged device is the load balancer, use `load-balancer-skill`.

## sender_resource_contention vs sender_application_delay

Both produce a slow backend. `sender_resource_contention` requires a resource signal (`stress_tool`, `cpu_hot_process`, `cpu_hot_service_daemon`, `tcp_estab_spike`, or an isolated exec timeout). `sender_application_delay` fires only when the backend is slow AND no resource signal is present. If any resource flag fires, it is NOT `sender_application_delay`.

## stress_tool vs http_flood_tool

`stress_tool` consumes local CPU/memory — the faulty device is the host running it. `http_flood_tool` drives abusive traffic at a victim — the faulty device is the victim, not the attacker.
