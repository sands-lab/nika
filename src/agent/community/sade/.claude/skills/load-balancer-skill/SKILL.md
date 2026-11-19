---
name: load-balancer-skill
description: Inspect load-balancer reachability, VIP service, and backend health when requests fail or slow down.
---

# Load balancer diagnosis

Compare the VIP with direct backend requests. Check listener state, backend reachability, connection state, and firewall rules on the implicated devices.

A slow or unresponsive load balancer identifies a diagnostic target. Select a fault from the current candidate list only when its mechanism has direct evidence. A shell timeout or CPU spike alone does not establish a specific fault.
