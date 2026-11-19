---
name: tc-fault-skill
description: Diagnose link capacity, latency, and packet corruption from path measurements and device counters.
---

# Link performance faults

Use the current fault candidates and compare the affected path with a healthy control:

- `link_capacity_bottleneck`: fan-in traffic exceeds a link's capacity, increasing delay or receive loss.
- `link_packet_corruption`: damaged packets increase checksum errors, retransmissions, or receive loss.
- `link_latency`: one direction of a link has excess delay.

Read interface counters with `device_read` on routers and switches. Use bounded endpoint probes to measure throughput, latency, and delivery. Hosts retain `exec_shell` for their own network state.

A qdisc can describe normal provisioning. Link impairments can also run outside the device, so an absent device qdisc does not clear a fault. Locate the affected attachment from path and counter evidence. Do not infer ownership from a shaping rule alone.
