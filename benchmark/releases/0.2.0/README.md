# nika-bench 0.2.0

Frozen Dev/Test suite for evaluating network troubleshooting agents on injectable faults. Use this release for official runs and leaderboard submissions. Working matrices under `benchmark/working/` are for development only.

Machine loaders read [`RELEASE.yaml`](RELEASE.yaml). Case rows are in [`dev.yaml`](dev.yaml) and [`test.yaml`](test.yaml).

## Run

```shell
nika benchmark releases
nika benchmark run --release 0.2.0 --split test --result_dir results/my-run
```

Default split is `test`. Each case runs `n_trials=3` times (`case_count × 3` deterministic trials). Official and leaderboard scoring use rule-based RCA F1 only (`leaderboard_primary: rca_f1`; `judge_allowed: false`). Optional `nika eval judge` output is for local analysis and does not count toward submissions.

Operator reference: [Benchmark configuration](../../../docs/benchmarks/benchmark-configuration.md). Leaderboard pack/validate: [Leaderboard submission](../../../docs/benchmarks/leaderboard-submission.md).

## Suite shape

| Split | Cases file | Cases |
| --- | --- | ---: |
| `dev` | `dev.yaml` | 84 |
| `test` | `test.yaml` | 85 |

Across both splits: **169** executable rows, **29** scenario IDs, **75** registered failures plus **healthy** baselines (18 healthy rows). Both splits cover every registered failure and publish their labels.

## Case catalog

Each row below is one executable benchmark case. Read **Scenario** for the live network and its role. Read **Deployment** for the scale and, in ISP cases, the backend, device profile, IGP, and BGP mode. **Failure** gives the registered root-cause meaning. RCA scoring uses the **Ground-truth resource**.

Open the linked YAML file for the complete injection parameters and materialized labels. See the [network scenario reference](../../../docs/operations/network-scenarios.md) for topology behavior, the [failure reference](../../../docs/operations/failures.md) for injection and verification contracts, and [root-cause evaluation](../../../docs/benchmarks/root-cause-evaluation.md) for scoring semantics.

### `dev` split (84 cases)

Rows follow [`dev.yaml`](dev.yaml) order. Labels such as `dev-001` locate rows in this README; the benchmark runtime derives its own case keys from row content.

| Row | Scenario | Deployment | Failure | Ground-truth resource |
| --- | --- | --- | --- | --- |
| `dev-001` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `s` | `arp_acl_block`: ARP is blocked by an ACL. | node `br1_corp_pc` |
| `dev-002` | `k8s_lab`: FRR fat-tree carrying a six-node k3s cluster | fixed size | `arp_cache_poisoning`: ARP cache is poisoned with a false MAC mapping. | node `client` |
| `dev-003` | `isp_nobel-eu`: SNDlib nobel-eu ISP topology | size `m`; kathara/frr; isis; ibgp_rr | `bgp_acl_block`: BGP control-plane traffic is blocked by an ACL. | node `amsterdam` |
| `dev-004` | `isp_cost266`: SNDlib cost266 ISP topology | size `l`; kathara/frr; isis; ibgp_rr | `bgp_asn_misconfig`: BGP local ASN is misconfigured relative to peer expectation. | node `amsterdam` |
| `dev-005` | `isp_abilene_ebgp_rtbh`: SNDlib abilene ISP topology with eBGP and RTBH | size `s`; kathara/frr | `bgp_blackhole_community_leak`: A provider blackhole BGP community is leaked on export. | node `kscyng` |
| `dev-006` | `isp_janos-us`: SNDlib janos-us ISP topology | size `m`; kathara/frr; isis; ibgp_rr | `bgp_hijacking`: An unauthorized BGP prefix is advertised (hijack). | node `albany` |
| `dev-007` | `isp_geant`: SNDlib geant ISP topology | size `m`; kathara/frr; ospf; ebgp | `bgp_max_prefix_exceeded`: BGP maximum-prefix limit is exceeded on a session. | node `de1_de`<br>node `nl1_nl` |
| `dev-008` | `isp_pioro40`: SNDlib pioro40 ISP topology | size `l`; kathara/frr; isis; ibgp_rr | `bgp_missing_route_advertisement`: An expected BGP route advertisement is missing. | node `n7` |
| `dev-009` | `isp_abilene_ebgp_rpki`: SNDlib abilene ISP topology with eBGP and offline RPKI | size `s`; kathara/frr | `bgp_rpki_invalid_route_leak`: RPKI-invalid prefixes are leaked into BGP. | node `kscyng` |
| `dev-010` | `p4_dc_fabric`: BMv2 P4Runtime L3 Clos with ActionSelector ECMP | size `l` | `bmv2_switch_down`: BMv2 switch dataplane process is down. | node `leaf_1` |
| `dev-011` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `s` | `device_forwarding_packet_corruption`: A forwarding device silently corrupts selected packets. | node `leaf_2` |
| `dev-012` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `m` | `dhcp_missing_subnet`: DHCP server is missing a subnet configuration. | node `dhcp_server` |
| `dev-013` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `l` | `dhcp_service_down`: DHCP server process is down. | node `dhcp_server` |
| `dev-014` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `s` | `dhcp_spoofed_dns`: DHCP distributes a spoofed DNS server option. | node `dhcp_server` |
| `dev-015` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `l` | `dhcp_spoofed_gateway`: DHCP distributes a spoofed default gateway. | node `dhcp_server` |
| `dev-016` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `m` | `dhcp_spoofed_subnet`: DHCP subnet configuration is spoofed or removed for clients. | node `dhcp_server` |
| `dev-017` | `dc_clos`: FRR eBGP data-center Clos with DNS and HTTP services | size `s` | `dns_lookup_latency`: DNS lookups are abnormally slow. | interface `dns_pod0:eth0` |
| `dev-018` | `dc_clos`: FRR eBGP data-center Clos with DNS and HTTP services | size `m` | `dns_port_blocked`: DNS service port is blocked. | node `dns_pod0` |
| `dev-019` | `dc_clos`: FRR eBGP data-center Clos with DNS and HTTP services | size `l` | `dns_record_error`: DNS returns an incorrect record for a name. | node `dns_pod0` |
| `dev-020` | `dc_clos`: FRR eBGP data-center Clos with DNS and HTTP services | size `s` | `dns_service_down`: DNS server process is down. | node `dns_pod0` |
| `dev-021` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `m` | `flow_rule_loop`: SDN flow rules create a forwarding loop. | node `leaf_1` |
| `dev-022` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `l` | `flow_rule_shadowing`: A higher-priority SDN rule shadows intended forwarding. | node `spine_1` |
| `dev-023` | `k8s_lab`: FRR fat-tree carrying a six-node k3s cluster | fixed size | `frr_service_down`: FRR routing daemon is unavailable. | node `leaf_1_1` |
| `dev-024` | `dc_clos`: FRR eBGP data-center Clos with DNS and HTTP services | size `s` | `host_incorrect_dns`: Host is configured with an incorrect DNS resolver. | node `client_0` |
| `dev-025` | `k8s_lab`: FRR fat-tree carrying a six-node k3s cluster | fixed size | `host_incorrect_gateway`: Host default gateway is incorrect. | node `client` |
| `dev-026` | `llmd_lab`: L2 k3s cluster running a simulated llm-d inference service | fixed size | `host_incorrect_ip`: Host IP address is incorrect. | interface `client:eth0` |
| `dev-027` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `m` | `host_incorrect_netmask`: Host netmask/prefix length is incorrect. | interface `br1_corp_pc:eth0` |
| `dev-028` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `l` | `host_ip_conflict`: Two hosts are configured with the same IP address. | interface `service_2_1:eth0` |
| `dev-029` | `p4_dc_fabric`: BMv2 P4Runtime L3 Clos with ActionSelector ECMP | size `s` | `host_missing_ip`: Host interface has no IP address. | interface `client_3_1:eth0` |
| `dev-030` | `isp_geant_ebgp_rpki`: SNDlib geant ISP topology with eBGP and offline RPKI | size `m`; kathara/frr | `host_static_blackhole`: A static blackhole route drops traffic to a prefix locally. | node `at1_at` |
| `dev-031` | `llmd_lab`: L2 k3s cluster running a simulated llm-d inference service | fixed size | `http_acl_block`: HTTP traffic is blocked by an ACL. | node `client` |
| `dev-032` | `isp_janos-us-ca`: SNDlib janos-us-ca ISP topology | size `l`; kathara/frr; isis; none | `icmp_acl_block`: ICMP is blocked by an ACL. | node `pc_atlanta` |
| `dev-033` | `isp_abilene`: SNDlib abilene ISP topology | size `s`; kathara/frr; isis; none | `icmp_frag_needed_filter_misconfiguration`: ICMP Fragmentation Needed messages are filtered. | node `atlam5` |
| `dev-034` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `m` | `incast_traffic_network_limitation`: Incast traffic exceeds available network capacity. | interface `service_1_1:eth0` |
| `dev-035` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `s` | `int_insufficient_mtu_headroom`: INT encapsulation lacks sufficient MTU headroom. | interface `gateway_1:eth1` |
| `dev-036` | `llmd_lab`: L2 k3s cluster running a simulated llm-d inference service | fixed size | `k8s_clusterip_routing_broken`: Kubernetes ClusterIP forwarding is broken on a node. | node `controller` |
| `dev-037` | `k8s_lab`: FRR fat-tree carrying a six-node k3s cluster | fixed size | `k8s_coredns_isolated`: Path to cluster DNS (CoreDNS) is isolated/blocked. | Kubernetes `kube-system/Service/kube-dns` |
| `dev-038` | `llmd_lab`: L2 k3s cluster running a simulated llm-d inference service | fixed size | `k8s_networkpolicy_deny`: Kubernetes NetworkPolicy denies ingress to selected pods. | Kubernetes `llm-d/NetworkPolicy/nika-deny-ingress` |
| `dev-039` | `llmd_lab`: L2 k3s cluster running a simulated llm-d inference service | fixed size | `k8s_worker_apiserver_partition`: Worker is partitioned from the Kubernetes API server. | node `worker1` |
| `dev-040` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `l` | `lb_connection_state_exhaustion`: Load-balancer connection-state table is exhausted. | node `gateway_1` |
| `dev-041` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `m` | `lb_pending_connection_update_race`: Pending connection races an unsafe load-balancer pool update. | node `gateway_1` |
| `dev-042` | `min3clos`: five-node SR Linux eBGP Clos | fixed size | `link_capacity_bottleneck`: Logical link capacity is bottlenecked below demand. | link `client1:eth1--leaf1:e1-2` |
| `dev-043` | `isp_ta2`: SNDlib ta2 ISP topology | size `l`; kathara/frr; isis; none | `link_detach`: Network attachment is detached; the interface is gone from the node. | interface `n10:eth0` |
| `dev-044` | `isp_dfn-bwin`: SNDlib dfn-bwin ISP topology | size `s`; kathara/frr; isis; none | `link_down`: Carrier or operational link is down on the selected attachment. | link `berlin:eth0--frankfurt:eth0` |
| `dev-045` | `isp_nobel-germany`: SNDlib nobel-germany ISP topology | size `m`; kathara/frr; isis; none | `link_flap`: Logical link flaps between up and down. | link `berlin:eth0--hamburg:eth0` |
| `dev-046` | `isp_india35`: SNDlib india35 ISP topology | size `l`; kathara/frr; isis; none | `link_packet_corruption`: Packets on the logical link are corrupted in transit while the link stays up; applications see partial loss, TCP retransmissions, and reduced throughput. | link `n_0:eth0--n_24:eth0` |
| `dev-047` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `s` | `load_balancer_overload`: Software load balancer is overloaded by offered HTTP load. | node `load_balancer` |
| `dev-048` | `p4_dc_fabric`: BMv2 P4Runtime L3 Clos with ActionSelector ECMP | size `m` | `mac_address_conflict`: Two hosts share the same MAC address. | interface `web_2:eth0` |
| `dev-049` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `l` | `mtu_mismatch`: Path MTU is misconfigured on an intermediate hop. | interface `br1_edge:eth2` |
| `dev-050` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `s` | `nat_mapping_removed_without_drain`: NAT mapping is removed before active flows drain. | node `br1_edge` |
| `dev-051` | `isp_ta1`: SNDlib ta1 ISP topology | size `m`; kathara/frr; ospf; none | `ospf_acl_block`: OSPF control-plane traffic is blocked by an ACL. | node `n1` |
| `dev-052` | `isp_germany50`: SNDlib germany50 ISP topology | size `l`; kathara/frr; ospf; none | `ospf_area_misconfiguration`: OSPF area is misconfigured between neighbors. | node `aachen` |
| `dev-053` | `isp_dfn-bwin_ebgp_rtbh`: SNDlib dfn-bwin ISP topology with eBGP and RTBH | size `s`; kathara/frr | `ospf_neighbor_missing`: Expected OSPF neighbor adjacency is missing. | node `berlin` |
| `dev-054` | `p4_dc_fabric`: BMv2 P4Runtime L3 Clos with ActionSelector ECMP | size `m` | `p4_action_selector_member_misconfig`: A P4 ActionSelector member is misconfigured. | node `leaf_1` |
| `dev-055` | `p4_dc_fabric`: BMv2 P4Runtime L3 Clos with ActionSelector ECMP | size `l` | `p4_ecmp_group_member_missing`: A member is missing from a P4 ECMP group. | node `leaf_1` |
| `dev-056` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `s` | `p4_ecn_threshold_misconfiguration`: ECN marking threshold is misconfigured (marking too late). | interface `gateway_1:eth1` |
| `dev-057` | `p4_dc_fabric`: BMv2 P4Runtime L3 Clos with ActionSelector ECMP | size `m` | `p4_table_entry_misconfig`: A P4 forwarding table entry is misconfigured. | node `leaf_1` |
| `dev-058` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `l` | `p4_table_entry_missing`: A required P4 forwarding table entry is missing. | node `gateway_1` |
| `dev-059` | `p4_dc_fabric`: BMv2 P4Runtime L3 Clos with ActionSelector ECMP | size `s` | `p4_table_resource_exhaustion`: A P4 table has exhausted its capacity. | node `leaf_1` |
| `dev-060` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `m` | `p4_tcam_entry_corruption`: A forwarding/TCAM entry is silently corrupted for one flow. | node `gateway_3` |
| `dev-061` | `p4_dc_fabric`: BMv2 P4Runtime L3 Clos with ActionSelector ECMP | size `l` | `p4runtime_partial_write`: A P4Runtime update was only partially applied. | node `leaf_1` |
| `dev-062` | `p4_dc_fabric`: BMv2 P4Runtime L3 Clos with ActionSelector ECMP | size `s` | `p4runtime_pipeline_mismatch`: Loaded P4 pipeline does not match the intended program. | node `leaf_1` |
| `dev-063` | `llmd_lab`: L2 k3s cluster running a simulated llm-d inference service | fixed size | `receiver_resource_contention`: Receiver endpoint is under resource contention. | node `client` |
| `dev-064` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `m` | `sdn_controller_crash`: SDN controller is down. | node `onos` |
| `dev-065` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `l` | `sender_resource_contention`: Sender/server endpoint is under CPU resource contention. | node `web_1` |
| `dev-066` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `s` | `silent_egress_packet_loss`: Egress silently drops a subset of packets. | interface `gateway_1:eth1` |
| `dev-067` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `m` | `snat_port_pool_exhaustion`: SNAT source-port pool is exhausted. | node `br1_edge` |
| `dev-068` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `l` | `southbound_port_block`: Controller southbound channel port is blocked. | node `onos` |
| `dev-069` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `s` | `southbound_port_mismatch`: Controller southbound listen port mismatches switch config. | node `onos` |
| `dev-070` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `l` | `tcp_receive_window_limited`: Receiver TCP window/buffer is too small for the path. | node `br1_corp_pc2` |
| `dev-071` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `m` | `tcp_syn_flood_attack`: TCP SYN flood attack against a target service. | node `client_1` |
| `dev-072` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `s` | `vrf_dscp_remarking`: VRF edge incorrectly remarks high-priority DSCP. | interface `hq_edge:wg_br1` |
| `dev-073` | `dc_clos`: FRR eBGP data-center Clos with DNS and HTTP services | size `m` | `web_dos_attack`: Web service is under a denial-of-service attack. | node `webserver0_pod0` |
| `dev-074` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `l` | `wireguard_allowed_ips_misconfiguration`: WireGuard AllowedIPs omits a required remote prefix. | interface `br1_edge:wg_hq` |
| `dev-075` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `s` | `wireguard_peer_key_misconfiguration`: WireGuard peer public key is incorrect. | interface `br1_edge:wg_hq` |
| `dev-076` | `min3clos`: five-node SR Linux eBGP Clos | fixed size | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `dev-077` | `isp_di-yuan`: SNDlib di-yuan ISP topology | size `s`; containerlab/nokia_srlinux; ospf; ebgp | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `dev-078` | `isp_pdh`: SNDlib pdh ISP topology | size `s`; containerlab/nokia_srlinux; isis; ibgp_rr | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `dev-079` | `isp_dfn-bwin`: SNDlib dfn-bwin ISP topology | size `s`; containerlab/nokia_srlinux; ospf; ebgp | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `dev-080` | `isp_pdh`: SNDlib pdh ISP topology | size `s`; containerlab/nokia_srlinux; isis; none | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `dev-081` | `isp_dfn-gwin`: SNDlib dfn-gwin ISP topology | size `s`; containerlab/nokia_srlinux; isis; none | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `dev-082` | `isp_di-yuan`: SNDlib di-yuan ISP topology | size `s`; containerlab/nokia_srlinux; isis; none | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `dev-083` | `isp_pdh`: SNDlib pdh ISP topology | size `s`; containerlab/nokia_srlinux; ospf; ebgp | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `dev-084` | `isp_di-yuan`: SNDlib di-yuan ISP topology | size `s`; containerlab/nokia_srlinux; isis; ibgp_rr | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |

### `test` split (85 cases)

Rows follow [`test.yaml`](test.yaml) order. Labels such as `test-001` locate rows in this README; the benchmark runtime derives its own case keys from row content.

| Row | Scenario | Deployment | Failure | Ground-truth resource |
| --- | --- | --- | --- | --- |
| `test-001` | `dc_clos`: FRR eBGP data-center Clos with DNS and HTTP services | size `m` | `arp_acl_block`: ARP is blocked by an ACL. | node `client_0` |
| `test-002` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `l` | `arp_cache_poisoning`: ARP cache is poisoned with a false MAC mapping. | node `client_10_1` |
| `test-003` | `isp_pdh`: SNDlib pdh ISP topology | size `s`; containerlab/nokia_srlinux; isis; ibgp_rr | `bgp_acl_block`: BGP control-plane traffic is blocked by an ACL. | node `n1` |
| `test-004` | `k8s_lab`: FRR fat-tree carrying a six-node k3s cluster | fixed size | `bgp_asn_misconfig`: BGP local ASN is misconfigured relative to peer expectation. | node `leaf_1_1` |
| `test-005` | `isp_dfn-bwin_ebgp_rtbh`: SNDlib dfn-bwin ISP topology with eBGP and RTBH | size `s`; kathara/frr | `bgp_blackhole_community_leak`: A provider blackhole BGP community is leaked on export. | node `frankfurt` |
| `test-006` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `l` | `bgp_hijacking`: An unauthorized BGP prefix is advertised (hijack). | node `br1_edge` |
| `test-007` | `isp_abilene`: SNDlib abilene ISP topology | size `s`; kathara/frr; ospf; ebgp | `bgp_max_prefix_exceeded`: BGP maximum-prefix limit is exceeded on a session. | node `hstnng`<br>node `losang` |
| `test-008` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `m` | `bgp_missing_route_advertisement`: An expected BGP route advertisement is missing. | node `br1_edge` |
| `test-009` | `isp_geant_ebgp_rpki`: SNDlib geant ISP topology with eBGP and offline RPKI | size `m`; kathara/frr | `bgp_rpki_invalid_route_leak`: RPKI-invalid prefixes are leaked into BGP. | node `es1_es` |
| `test-010` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `s` | `bmv2_switch_down`: BMv2 switch dataplane process is down. | node `gateway_1` |
| `test-011` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `l` | `device_forwarding_packet_corruption`: A forwarding device silently corrupts selected packets. | node `router_core_2` |
| `test-012` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `l` | `dhcp_missing_subnet`: DHCP server is missing a subnet configuration. | node `dhcp_server` |
| `test-013` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `m` | `dhcp_service_down`: DHCP server process is down. | node `dhcp_server` |
| `test-014` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `l` | `dhcp_spoofed_dns`: DHCP distributes a spoofed DNS server option. | node `dhcp_server` |
| `test-015` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `s` | `dhcp_spoofed_gateway`: DHCP distributes a spoofed default gateway. | node `dhcp_server` |
| `test-016` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `s` | `dhcp_spoofed_subnet`: DHCP subnet configuration is spoofed or removed for clients. | node `dhcp_server` |
| `test-017` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `m` | `dns_lookup_latency`: DNS lookups are abnormally slow. | interface `dns_server:eth0` |
| `test-018` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `l` | `dns_port_blocked`: DNS service port is blocked. | node `dns_server` |
| `test-019` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `m` | `dns_record_error`: DNS returns an incorrect record for a name. | node `dns_server` |
| `test-020` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `m` | `dns_service_down`: DNS server process is down. | node `dns_server` |
| `test-021` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `s` | `flow_rule_loop`: SDN flow rules create a forwarding loop. | node `leaf_1` |
| `test-022` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `s` | `flow_rule_shadowing`: A higher-priority SDN rule shadows intended forwarding. | node `spine_1` |
| `test-023` | `isp_germany50`: SNDlib germany50 ISP topology | size `l`; kathara/frr; isis; none | `frr_service_down`: FRR routing daemon is unavailable. | node `aachen` |
| `test-024` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `m` | `host_incorrect_dns`: Host is configured with an incorrect DNS resolver. | node `pc_1_1_1_1` |
| `test-025` | `dc_clos`: FRR eBGP data-center Clos with DNS and HTTP services | size `l` | `host_incorrect_gateway`: Host default gateway is incorrect. | node `client_0` |
| `test-026` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `s` | `host_incorrect_ip`: Host IP address is incorrect. | interface `client_1:eth0` |
| `test-027` | `k8s_lab`: FRR fat-tree carrying a six-node k3s cluster | fixed size | `host_incorrect_netmask`: Host netmask/prefix length is incorrect. | interface `client:eth0` |
| `test-028` | `llmd_lab`: L2 k3s cluster running a simulated llm-d inference service | fixed size | `host_ip_conflict`: Two hosts are configured with the same IP address. | interface `web:eth0` |
| `test-029` | `llmd_lab`: L2 k3s cluster running a simulated llm-d inference service | fixed size | `host_missing_ip`: Host interface has no IP address. | interface `client:eth0` |
| `test-030` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `l` | `host_static_blackhole`: A static blackhole route drops traffic to a prefix locally. | node `br1_edge` |
| `test-031` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `m` | `http_acl_block`: HTTP traffic is blocked by an ACL. | node `client_1` |
| `test-032` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `s` | `icmp_acl_block`: ICMP is blocked by an ACL. | node `client_1` |
| `test-033` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `m` | `icmp_frag_needed_filter_misconfiguration`: ICMP Fragmentation Needed messages are filtered. | node `gateway_1` |
| `test-034` | `p4_dc_fabric`: BMv2 P4Runtime L3 Clos with ActionSelector ECMP | size `l` | `incast_traffic_network_limitation`: Incast traffic exceeds available network capacity. | interface `web_2:eth0` |
| `test-035` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `m` | `int_insufficient_mtu_headroom`: INT encapsulation lacks sufficient MTU headroom. | interface `gateway_1:eth1` |
| `test-036` | `k8s_lab`: FRR fat-tree carrying a six-node k3s cluster | fixed size | `k8s_clusterip_routing_broken`: Kubernetes ClusterIP forwarding is broken on a node. | node `controller` |
| `test-037` | `llmd_lab`: L2 k3s cluster running a simulated llm-d inference service | fixed size | `k8s_coredns_isolated`: Path to cluster DNS (CoreDNS) is isolated/blocked. | Kubernetes `kube-system/Service/kube-dns` |
| `test-038` | `k8s_lab`: FRR fat-tree carrying a six-node k3s cluster | fixed size | `k8s_networkpolicy_deny`: Kubernetes NetworkPolicy denies ingress to selected pods. | Kubernetes `word-ns/NetworkPolicy/nika-deny-ingress` |
| `test-039` | `k8s_lab`: FRR fat-tree carrying a six-node k3s cluster | fixed size | `k8s_worker_apiserver_partition`: Worker is partitioned from the Kubernetes API server. | node `worker1` |
| `test-040` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `s` | `lb_connection_state_exhaustion`: Load-balancer connection-state table is exhausted. | node `gateway_1` |
| `test-041` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `l` | `lb_pending_connection_update_race`: Pending connection races an unsafe load-balancer pool update. | node `gateway_1` |
| `test-042` | `isp_di-yuan`: SNDlib di-yuan ISP topology | size `s`; kathara/frr; isis; none | `link_capacity_bottleneck`: Logical link capacity is bottlenecked below demand. | link `n_10:eth0--n_9:eth3` |
| `test-043` | `dc_clos`: FRR eBGP data-center Clos with DNS and HTTP services | size `m` | `link_detach`: Network attachment is detached; the interface is gone from the node. | interface `client_0:eth0` |
| `test-044` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `l` | `link_down`: Carrier or operational link is down on the selected attachment. | link `br1_corp_pc2:eth0--br1_corp_pc3:eth0--br1_corp_pc4:eth0--br1_corp_pc:eth0--br1_edge:eth0` |
| `test-045` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `s` | `link_flap`: Logical link flaps between up and down. | link `client_1_1:eth0--leaf_1:eth1` |
| `test-046` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `m` | `link_packet_corruption`: Packets on the logical link are corrupted in transit while the link stays up; applications see partial loss, TCP retransmissions, and reduced throughput. | link `br1_edge:eth3--isp1_core:eth2` |
| `test-047` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `l` | `load_balancer_overload`: Software load balancer is overloaded by offered HTTP load. | node `load_balancer` |
| `test-048` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `s` | `mac_address_conflict`: Two hosts share the same MAC address. | interface `service_1_2:eth0` |
| `test-049` | `isp_geant_ebgp_rpki`: SNDlib geant ISP topology with eBGP and offline RPKI | size `m`; kathara/frr | `mtu_mismatch`: Path MTU is misconfigured on an intermediate hop. | interface `at1_at:eth0` |
| `test-050` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `l` | `nat_mapping_removed_without_drain`: NAT mapping is removed before active flows drain. | node `br1_edge` |
| `test-051` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `s` | `ospf_acl_block`: OSPF control-plane traffic is blocked by an ACL. | node `router_core_1` |
| `test-052` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `m` | `ospf_area_misconfiguration`: OSPF area is misconfigured between neighbors. | node `router_core_1` |
| `test-053` | `campus_lan`: hierarchical OSPF campus with DHCP, DNS, and a web farm | size `l` | `ospf_neighbor_missing`: Expected OSPF neighbor adjacency is missing. | node `router_core_1` |
| `test-054` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `s` | `p4_action_selector_member_misconfig`: A P4 ActionSelector member is misconfigured. | node `leaf_1` |
| `test-055` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `m` | `p4_ecmp_group_member_missing`: A member is missing from a P4 ECMP group. | node `leaf_1` |
| `test-056` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `l` | `p4_ecn_threshold_misconfiguration`: ECN marking threshold is misconfigured (marking too late). | interface `spine_1:eth9` |
| `test-057` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `s` | `p4_table_entry_misconfig`: A P4 forwarding table entry is misconfigured. | node `leaf_1` |
| `test-058` | `p4_dc_fabric`: BMv2 P4Runtime L3 Clos with ActionSelector ECMP | size `m` | `p4_table_entry_missing`: A required P4 forwarding table entry is missing. | node `leaf_1` |
| `test-059` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `l` | `p4_table_resource_exhaustion`: A P4 table has exhausted its capacity. | node `leaf_1` |
| `test-060` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `s` | `p4_tcam_entry_corruption`: A forwarding/TCAM entry is silently corrupted for one flow. | node `spine_1` |
| `test-061` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `m` | `p4runtime_partial_write`: A P4Runtime update was only partially applied. | node `leaf_1` |
| `test-062` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `l` | `p4runtime_pipeline_mismatch`: Loaded P4 pipeline does not match the intended program. | node `gateway_1` |
| `test-063` | `dc_clos`: FRR eBGP data-center Clos with DNS and HTTP services | size `s` | `receiver_resource_contention`: Receiver endpoint is under resource contention. | node `client_0` |
| `test-064` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `l` | `sdn_controller_crash`: SDN controller is down. | node `onos` |
| `test-065` | `llmd_lab`: L2 k3s cluster running a simulated llm-d inference service | fixed size | `sender_resource_contention`: Sender/server endpoint is under CPU resource contention. | node `web` |
| `test-066` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `m` | `silent_egress_packet_loss`: Egress silently drops a subset of packets. | interface `gateway_1:eth1` |
| `test-067` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `s` | `snat_port_pool_exhaustion`: SNAT source-port pool is exhausted. | node `br1_edge` |
| `test-068` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `m` | `southbound_port_block`: Controller southbound channel port is blocked. | node `onos` |
| `test-069` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `l` | `southbound_port_mismatch`: Controller southbound listen port mismatches switch config. | node `onos` |
| `test-070` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `s` | `tcp_receive_window_limited`: Receiver TCP window/buffer is too small for the path. | node `br1_corp_pc` |
| `test-071` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `l` | `tcp_syn_flood_attack`: TCP SYN flood attack against a target service. | node `client_1` |
| `test-072` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `m` | `vrf_dscp_remarking`: VRF edge incorrectly remarks high-priority DSCP. | interface `hq_edge:wg_br1` |
| `test-073` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `s` | `web_dos_attack`: Web service is under a denial-of-service attack. | node `web_2` |
| `test-074` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `s` | `wireguard_allowed_ips_misconfiguration`: WireGuard AllowedIPs omits a required remote prefix. | interface `br1_edge:wg_hq` |
| `test-075` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `l` | `wireguard_peer_key_misconfiguration`: WireGuard peer public key is incorrect. | interface `br1_edge:wg_hq` |
| `test-076` | `min3clos`: five-node SR Linux eBGP Clos | fixed size | `bgp_missing_route_advertisement`: An expected BGP route advertisement is missing. | node `leaf1` |
| `test-077` | `llmd_lab`: L2 k3s cluster running a simulated llm-d inference service | fixed size | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `test-078` | `dc_clos`: FRR eBGP data-center Clos with DNS and HTTP services | size `m` | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `test-079` | `dc_clos`: FRR eBGP data-center Clos with DNS and HTTP services | size `l` | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `test-080` | `k8s_lab`: FRR fat-tree carrying a six-node k3s cluster | fixed size | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `test-081` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `m` | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `test-082` | `p4_dc_gateway`: BMv2 gateway-spine-leaf fabric with ECMP, INT, ECN, queues, and load balancing | size `m` | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `test-083` | `sdn_l3_clos`: ONOS-controlled OVS L3 Clos with ECMP | size `l` | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `test-084` | `enterprise_branch`: hub-and-spoke enterprise WAN with WireGuard, eBGP, and VRFs | size `m` | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |
| `test-085` | `dc_clos`: FRR eBGP data-center Clos with DNS and HTTP services | size `s` | `healthy`: The runner injects no fault; the case checks the healthy scenario baseline. | None; healthy baseline |

## Runtime requirements

Allowed MCP servers and required container images are declared in [`RELEASE.yaml`](RELEASE.yaml) (`tools.allowed_mcp_servers`, `images.required`). Preflight checks them when you run a release. MCP overview: [MCP servers](../../../docs/agents/mcp-servers.md).
