---
name: host-crash-skill
description: Investigate a host that does not respond to network or service probes.
---

# Unresponsive host

Probe the host's known IP and compare the result with a same-role peer. Check the path and any reachable services before attributing the failure to the host itself. A name-resolution failure alone does not establish that the host is down.

If the host responds to ICMP but shell or service calls time out, inspect resource contention. If several peers are unreachable, investigate their shared path and routing before choosing a fault. Select a root cause only when the evidence supports a current fault candidate.
