---
name: resource-contention-skill
description: Identify per-host resource and load faults — sender or receiver resource contention, sender application delay, web DoS attack. Use when one host has a process or job hogging CPU, memory, or sockets, a throttled cgroup CPU quota, TCP socket spikes, or slow localhost responses while same-role peers stay clean.
---

# Resource Contention and DoS

## What counts as direct evidence

Resource evidence is operator-level and comes from a RUNNING process on the candidate. Use `exec_shell` with `ps`, `cat /proc/loadavg`, `ss -s`, and `cat /sys/fs/cgroup/cpu.max /sys/fs/cgroup/cpu.stat` to find a process or service hogging CPU or memory, many concurrent workers of one command such as a backup, compression, or indexing job, a container CPU quota that is low or mostly throttled, or a socket spike. Name the culprit by what it is (its command, owner, and service), and tie it to the symptom: the slow transfer is served or read by that host. Installed binaries without a running process do not count.

One direct probe on the candidate to confirm ownership is enough — do not chain extra investigations once the family is confirmed.

## Not host_crash

`host_crash` means the container was killed/stopped/removed: no response at all, same-role peers clean. An isolated `exec_failed`/timeout on one web/app server while ICMP still answers is `sender_resource_contention`, not `host_crash`. Do not require a readable process list after the container is already too loaded to answer process probes.

## Not load_balancer_overload

Stay here only when the pressure signal is on a non-LB backend or client. If the flagged device is the load balancer, use `load-balancer-skill`.

## sender_resource_contention vs sender_application_delay

Both produce a slow backend. `sender_resource_contention` requires a resource signal (a CPU-hot process or service daemon, worker fan-out, a low or throttled cgroup CPU quota, an ESTABLISHED socket spike, or an isolated exec timeout). `sender_application_delay` fires only when the backend is slow AND no resource signal is present. If any resource signal is present, it is NOT `sender_application_delay`.

## Local hog vs abusive clients

A local CPU or memory hog consumes the host's own resources — the faulty device is the host running it. Abusive request traffic (an HTTP flood, or connection spikes from a few sources) loads a victim — the faulty device is the victim, not the client sending it.
