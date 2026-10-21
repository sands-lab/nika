---
name: tc-fault-skill
description: Identify generic traffic-control faults — link bandwidth throttling, link packet corruption, incast traffic limitation. Use when tc qdisc output shows tbf or netem shaping on a network-path interface.
---

# Traffic Control Faults

## What These Faults Mean

- `link_bandwidth_throttling`: a device interface owns a `tbf` shaping rule that caps throughput.
- `link_high_packet_corruption`: a `netem corrupt` rule randomly damages packets, so TCP retransmits and throughput collapses.
- `incast_traffic_network_limitation`: the qdisc stack combines `netem` and `tbf`, creating fan-in or queueing behavior that looks worse than a simple bandwidth cap.

## Leading Signals

- `tc qdisc show` reveals a non-default shaping stack on the implicated interface.
- The qdisc layout itself is the decisive proof; RTT or throughput symptoms only tell you where to look.

## When To Use

- Quiet throughput degradation
- Extreme jitter with little or no packet loss
- Suspected qdisc or shaping on a server or router interface
- If ownership is still unclear, use `exec_shell` with `tc -s qdisc show` to find non-default qdisc stacks before targeted tc checks.

## Exact Calls

- `exec_shell(host_name="<host>", command="tc qdisc show dev <interface>")`
- `get_tc_statistics(host_name="<host>", intf_name="<interface>")`
- `exec_shell(host_name="<host>", command="tc -s qdisc show dev <interface>")`

## Required Tool Usage

- `get_tc_statistics` always needs both `host_name` and `intf_name`.
- Check the specific interface under test, not a default placeholder. If the bottleneck may be on a different interface, use that one.

## Guardrails

- The decisive signal is the qdisc stack itself, not just high RTT.
- Report only the device that actually owns the shaping rule.
- Sending clients are victims, not faulty devices, unless a different skill proves otherwise.
