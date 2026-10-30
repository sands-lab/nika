"""Nokia SR Linux platform figures shared across environments."""

from __future__ import annotations

# Memory one SR Linux router needs: Containerlab ISP labs with 10-11 SR Linux
# routers peak at 14.2-15.7 GiB with their hosts (docs/benchmarks/scenario_perf.md).
# Firecracker sizes SR Linux lab VMs from it.
ROUTER_MEMORY_MIB = 1536
