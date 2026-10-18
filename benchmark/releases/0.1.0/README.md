# nika-bench 0.1.0

Run it with `nika benchmark run --release 0.1.0`. [Run release 0.1.0](../../../docs/compat/release-0.1.0.md) covers the run steps, known limitations, and which cases run on restored original labs. Use [0.2.0](../0.2.0/README.md) for current runs. Machine metadata remains in [`RELEASE.yaml`](RELEASE.yaml); cases are in [`dev.yaml`](dev.yaml) and [`test.yaml`](test.yaml).

## Suite shape

| Split | Cases file | Cases |
| --- | --- | ---: |
| `dev` | `dev.yaml` | 56 |
| `test` | `test.yaml` | 56 |

Across both splits: **112** rows, **56** failure IDs (no healthy baselines). The published rows used **10** legacy scenario IDs; [Fixed rows](#fixed-rows) lists the rows that now name a current scenario.

## Scenario coverage (legacy IDs)

Counts sum `dev` + `test` for the published rows. These IDs are not in the current scenario catalog. For today's scenarios, read [Network scenario reference](../../../docs/operations/network-scenarios.md).

| Legacy scenario | Cases | Distinct problems |
| --- | ---: | ---: |
| `ospf_enterprise_dhcp` | 32 | 26 |
| `dc_clos_service` | 25 | 25 |
| `dc_clos_bgp` | 23 | 23 |
| `p4_bloom_filter` | 7 | 6 |
| `ospf_enterprise_static` | 6 | 6 |
| `p4_counter` | 5 | 5 |
| `sdn_clos` | 5 | 5 |
| `sdn_star` | 5 | 5 |
| `p4_mpls` | 2 | 1 |
| `rip_small_internet_vpn` | 2 | 1 |

Rough mapping to current docs (names differ; do not treat as identity):

- Clos / BGP / service → [Data-center Clos](../../../docs/operations/network-scenarios.md#data-center-clos-scenario)
- OSPF enterprise → [Campus LAN](../../../docs/operations/network-scenarios.md#campus-lan-scenario) / [Enterprise Branch](../../../docs/operations/network-scenarios.md#enterprise-branch-vpn-scenario)
- SDN → [SDN scenarios](../../../docs/operations/network-scenarios.md#sdn-scenarios)
- P4 → [P4 scenarios](../../../docs/operations/network-scenarios.md#p4-scenarios)

## Fixed rows

These rows differ from the published files. Each change keeps the original fault semantics. The first column counts rows across both splits.

| Rows | Published value | Current value | Reason |
| ---: | --- | --- | --- |
| 32 | `ospf_enterprise_dhcp` | `campus_lan`, same targets | The enterprise DHCP lab became `campus_lan`. |
| 3 | `ospf_enterprise_static`, `pc_2_1_1_1` rows | `campus_lan`, same targets | The static enterprise lab became `campus_lan`. |
| 3 | `ospf_enterprise_static` OSPF rows on `switch_server_access`, `switch_dist_1_2`, `switch_dist_2_3` | `campus_lan` on `server_access_router`, `router_dist_1_2`, `router_dist_2_3` | The same-role routers in the current lab. |
| 25 | `dc_clos_service` | `dc_clos`, same targets | The service Clos lab became `dc_clos`. |
| 8 | `dc_clos_bgp` router rows | `dc_clos`, same targets | `dc_clos` reproduces the router targets. |
| 15 | `dc_clos_bgp` rows on `pc_<pod>_<leaf>` hosts | `dc_clos` on the host behind the same leaf: `dns_pod<pod>` for leaf 0, `webserver<leaf-1>_pod<pod>` otherwise | `dc_clos` has one host per leaf and no `pc_*` hosts. Same pod, leaf, and host interface. |
| 2 | `sdn_clos` flow rule rows | `sdn_l3_clos`, same targets | The ONOS fabric reproduces the flow rule faults. Controller rows stay on the original POX lab. |
| 4 | `link_fragmentation_disabled`, `link_high_packet_corruption` | `mtu_mismatch`, `link_packet_corruption`, same parameters | Current failure IDs. Their ground truth names both MTU endpoints and the corrupted link. |
| 4 | `link_down`, `link_flap` interface ground truth | Link ground truth | Current ground truth owns cable faults by link. |
| 2 | `dns_lookup_latency` interface ground truth | DNS server node ground truth | The fault delays every packet that leaves the DNS server. |
| 2 | `incast_traffic_network_limitation` on the server interface | The congested router egress interface | Current ground truth names the bottleneck interface. |
| 2 | `load_balancer_overload` with only the load balancer name | Adds the current client and load parameters | The current failure measures load from explicit clients. |
| 2 | `sender_resource_contention` with only the server name | Adds a client and the stressed server's URLs | The defaults point at `web0.pod0`, so the probe missed the stressed server. |
| 2 | `load_balancer_overload` with `duration` | The current parameter set, with `duration_sec: '300'` | Current failure parameters. |

`RELEASE.yaml` lists the images and MCP servers that the executed scenarios need.

## Failure coverage

Legacy failure IDs overlap partially with today's registry. Authoritative current taxonomy: [Failure taxonomy and reference](../../../docs/operations/failures.md). Current release membership matrix: [Coverage matrix (0.2.0)](../../../docs/benchmarks/benchmark-configuration.md#coverage-matrix-scenario--failure).

## Scoring (when this version was active)

Rule-based RCA F1 (`n_trials=3`). See [Root-cause evaluation](../../../docs/benchmarks/root-cause-evaluation.md) for the current scoring contract.
