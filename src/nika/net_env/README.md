# Network Scenarios

NIKA registers 15 network scenarios across Kathara and Containerlab. Use
`nika env list` to list them and `nika env run <scenario>` to deploy one.
Scenarios marked `s/m/l` require `-s`.

| Scenario | Backend | Size | Documentation |
|----------|---------|------|---------------|
| `dc_clos_bgp` | Kathara | s/m/l | [Data-center Clos](kathara/data_center_routing/dc_clos_bgp/README.md) |
| `dc_clos_service` | Kathara | s/m/l | [Data-center Clos](kathara/data_center_routing/dc_clos_bgp/README.md#dc_clos_service) |
| `ospf_enterprise_static` | Kathara | s/m/l | [Enterprise OSPF](kathara/intradomain_routing/ospf_enterprise/README.md#ospf_enterprise_static) |
| `ospf_enterprise_dhcp` | Kathara | s/m/l | [Enterprise OSPF](kathara/intradomain_routing/ospf_enterprise/README.md#ospf_enterprise_dhcp) |
| `rip_small_internet_vpn` | Kathara | s/m/l | [RIP and WireGuard](kathara/intradomain_routing/rip_vpn/README.md) |
| `simple_bgp` | Kathara | fixed | [Simple BGP](kathara/interdomain_routing/simple_bgp/README.md) |
| `sdn_star` | Kathara | s/m/l | [SDN topologies](kathara/sdn/README.md#sdn_star) |
| `sdn_clos` | Kathara | s/m/l | [SDN topologies](kathara/sdn/README.md#sdn_clos) |
| `p4_bloom_filter` | Kathara | fixed | [P4 Bloom filter](kathara/p4/p4_bloom_filter/README.md) |
| `p4_counter` | Kathara | fixed | [P4 counters](kathara/p4/p4_counter/README.md) |
| `p4_int` | Kathara | fixed | [P4 INT](kathara/p4/p4_int/README.md) |
| `p4_mpls` | Kathara | fixed | [P4 MPLS](kathara/p4/p4_mpls/README.md) |
| `k8s_lab` | Kathara | fixed | [Fat-tree k3s](kathara/kubernetes/k8s_lab/README.md) |
| `llmd_lab` | Kathara | fixed | [llm-d k3s](kathara/kubernetes/llmd_lab/README.md) |
| `min3clos` | Containerlab | fixed | [SR Linux Clos](containerlab/min3clos/README.md) |

Kathara scenarios require `uv sync --extra kathara`; Containerlab scenarios
require `uv sync --extra containerlab`, Docker, and `clab` on `PATH`.
