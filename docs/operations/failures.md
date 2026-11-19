# Failure taxonomy and reference

The Sources column links each failure to its [public evidence](#public-sources): key passages, source locations, code correspondence, and evidence limits.

This reference is for benchmark authors and paper reviewers who analyze NIKA coverage. NIKA assigns each registered failure to one network subsystem. The registry keeps `root_cause_name` as the stable failure ID.

Run these commands against the installed checkout:

```shell
uv run nika failure list
uv run nika failure describe <failure_id>
```

The Verification evidence column states what `verify_fault` checks to gate `nika failure inject`; some checks also include selected behavioral conditions. The Trigger and signal column describes expected behavior under the stated traffic and topology, rather than a result guaranteed by an artifact check. Test-path **`evaluate_symptom`** (unified probe API under `src/nika/validation/symptom/`) checks user-visible impact separately; user workflows do not call it. See [Testing guide](../development/testing.md#problem-injection-testsnikaproblems).

`endpoint_cpu_contention` and `tcp_syn_flood_attack` run until recovery or session close. Their `duration` input remains accepted for stored task definitions; it does not set a timer.

## Taxonomy model

The `failure_domain` identifies the subsystem where the injected mechanism acts. The `security` domain contains attacks, spoofing, and poisoning failures.

| Axis | Accepted values | Interpretation |
| --- | --- | --- |
| `failure_domain` | `link_interface`, `routing_control_plane`, `forwarding_encapsulation_policy`, `service_networking`, `management_orchestration_plane`, `addressing_neighbor_naming`, `endpoint_application`, `traffic_queueing_resource`, `security` | Network subsystem that contains the failed mechanism. |

## Domain boundaries

The [working pool](../benchmarks/benchmark-configuration.md#working-matrix) records generated task coverage. The registry defines the domains below.

| Domain | Boundary |
| --- | --- |
| Link & Interface | Link attachment, interface state, flapping, packet corruption, and capacity bottlenecks. |
| Routing & Control Plane | BGP, OSPF, and IS-IS route computation, advertisements, adjacency, and routing-daemon availability. |
| Forwarding, Encapsulation & Policy | ACLs, static forwarding, RIB/FIB consistency, SDN rules, P4 pipelines, MTU behavior, WireGuard and Kubernetes VXLAN encapsulation, and Kubernetes NetworkPolicy. |
| Service Networking | Kubernetes ClusterIP forwarding, Service port mapping, Service selector matching, Ingress class selection and rate limits, and load-balancer operation and address advertisement. |
| Management & Orchestration Plane | Kubernetes API-server reachability, Kubernetes Deployment replica counts and readiness probes, node taints, SDN controller availability, controller southbound management channels, and switch identity at the controller. |
| Addressing, Neighbor & Naming | IP and MAC assignment, ARP, DHCP, DNS data, DNS reachability, and name resolution. |
| Endpoint & Application | Endpoint resource contention and application delay. |
| Traffic, Queueing & Resource | Offered load, queueing, and end-host resource constraints. |
| Security | Attacks, spoofing, and poisoning. |

The domains follow the failed network mechanism.

## Compatibility sets

The scenario labels below reproduce tag-subset matching (`TAGS` on each failure vs scenario tags; optional `COMPATIBLE_COLUMNS` when tags alone are too broad). A tag match means that the scenario exposes the required device family. Injection still requires a suitable target inside the selected scenario.

| Set | Scenario IDs |
| --- | --- |
| **All** | All registered scenarios |
| **Host L2** | `dc_clos`, `campus_lan`, `enterprise_branch`, `sdn_l3_clos`, `p4_dc_fabric`, `p4_dc_gateway`, `k8s_lab`, `llmd_lab` |
| **Routed host** | `dc_clos`, `campus_lan`, `enterprise_branch`, `k8s_lab` |
| **FRR** | `dc_clos`, `campus_lan`, `enterprise_branch`, `isp_*`, `k8s_lab` |
| **BGP** | `dc_clos`, `enterprise_branch`, `isp_*`, `k8s_lab` |
| **RPKI** | `isp_*` with `--bgp-mode ebgp` on Kathara FRR |
| **OSPF** | `campus_lan`, `isp_*` |
| **IS-IS** | `isp_*` |
| **DNS** | `dc_clos` and `campus_lan` |
| **HTTP** | `dc_clos`, `campus_lan`, `enterprise_branch`, `sdn_l3_clos`, `p4_dc_fabric`, `p4_dc_gateway`, and `llmd_lab` |
| **DHCP** | `campus_lan` |
| **SDN** | `sdn_l3_clos` |
| **P4** | `p4_dc_fabric`, `p4_dc_gateway` |
| **Kubernetes** | `k8s_lab`, `llmd_lab` |
| **VPN** | `enterprise_branch` |

For base `isp_*` scenarios, OSPF failures require `--igp ospf` and IS-IS failures require `--igp isis`; BGP failures require `--bgp-mode ibgp_rr` or `ebgp`. `bgp_ttl_security_mismatch` requires `--bgp-mode ebgp`. On Kathara FRR, every eBGP deployment carries the RPKI/ROV profile (with a Routinator cache) and, where the AS partition has the provider interconnect, the RTBH profile, so the RPKI-invalid-origin variant of `bgp_hijacking` runs where the unauthorized origin directly peers a non-ROV observer, and `bgp_blackhole_community_leak` runs where RTBH is present. Generic link failures and dual-backend BGP/ACL/static faults expand onto both Kathará+FRR and Containerlab, within the [Containerlab ISP size cap](network-scenarios.md#sndlib-isp-scenarios). Other ISP failures stay Kathará-only. SONiC routers route with FRR, so failures in the **FRR** set can also run on `dc_clos` and Containerlab ISP environments with `--device-profile sonic`, subject to backend, provider, and target checks. Endpoint CPU contention and RIB/FIB inconsistency require Kathara. [Router platforms](#router-platforms) lists which failures run on SR Linux, IOS XR, RouterOS, and SONiC.

The `bgp_route_leak` failure runs on Kathara FRR eBGP graphs whose AS partition has a multihomed customer and two peering providers. The healthy customer exports only locally originated routes upstream. Injection bypasses that restriction on one session, so the receiver prefers a customer transit path while the original origin ASN remains RPKI valid. The Oracle checks the received AS path and export policy to distinguish this failure from an origin hijack or a missing route. RFC 9234 roles and OTC describe a protocol mitigation; these environments use export filters rather than OTC enforcement.

The `link_latency` failure adds one-way delay to the controller-side link. Compare a business flow crossing it with an endpoint-to-gateway control path that avoids it, then use direct cross-link probes or traceroute to locate the delay. Its deployment compatibility follows `link_packet_corruption`.

## Device and link localization

`device_flow_blackhole` and `device_forwarding_packet_corruption` affect flow-selected traffic through the whole device. Their reference diagnoses compare two service paths that share that device but no data link. A deployment without two such paths cannot establish this device-level diagnosis from path evidence.

For black holes, compare repeated five-tuples, successful control flows, and ICMP reachability. For internal payload corruption, compare receiver TCP checksum counters before and after transfers on both paths. Interface counters provide context; these silent faults do not manufacture interface discard or CRC errors. Real ASIC queue discards and physical receive errors have different meanings, as described in [Juniper's drop-counter reference](https://www.juniper.net/documentation/us/en/software/junos/junos-overview/topics/concept/using-show-commands-for-packet-drops.html) and [Cisco's interface-error reference](https://www.cisco.com/c/en/us/support/docs/switches/catalyst-6500-series-switches/12027-53.html).

`link_packet_corruption` affects one link and also damages large ICMP probes. Its Oracle requires partial ICMP loss with rising receiver integrity-error counters. Device payload corruption preserves ICMP; device flow blackholing preserves ICMP and loses selected handshakes consistently. The audit runs these playbooks against each other at the same attachment.

## Router platforms

Failures that change a router's own state run on FRR by default. On SR Linux they run through the failure's Containerlab branch. On IOS XR, RouterOS and SONiC they run through a fault-operations provider: `src/nika/net_env/utils/<platform>/fault_ops.py` implements the `RouterFaultOps` operations of [`router_ops.py`](../../src/nika/problems/support/router_ops.py), and a failure targets a platform's routers only when the provider implements every operation in the failure's `native_router_operations`. Failures that act on the container link outside the router OS (`link_down`, `link_flap`, `link_capacity_bottleneck`, `link_packet_corruption`, `link_latency`) run on every platform the backend supports.

| Failure | IOS XR | RouterOS | SONiC |
| --- | --- | --- | --- |
| `link_down` | Drops carrier on the Kathara attachments while the interface remains present | Drops carrier on the Kathara attachments while the interface remains present | Drops carrier on the link attachments outside the router OS |
| `device_forwarding_packet_corruption` | Not a target | Applies the flow selector to controller-owned proxies on every data port | Applies the flow selector to the [host-side wires](network-scenarios.md#backend-requirements) on every data port |
| `bgp_asn_misconfig` | Renumbers `router bgp` | Sets `/routing bgp instance` `as=` | Rewrites `router bgp` in `frr.conf`, removes the live instance, and reloads the file |
| `bgp_missing_route_advertisement` | Withdraws the BGP `network` statement | Removes the prefix from the BGP output address list or VRF connected export | `no network` in FRR and `frr.conf` |
| `routing_service_down` | Excluded on XRd 26.2.1: stopped processes fail to restart | Excluded: no persistent routing-service stop operation | Stops SONiC's supervised FRR daemons |
| `ospf_area_misconfiguration` | Changes the interface OSPF area | Changes the OSPF area ID and restarts the instance | Changes the FRR network or interface area and saves the configuration |
| `ospf_interface_not_enabled` | Removes the interface from its `router ospf` area | Removes the interface template that covers the interface | Removes the FRR network or interface binding for one adjacency and saves the configuration |
| `bgp_hijacking` | Adds the prefix's first address on `Loopback1` and a BGP `network` statement | Adds the address on a bridge interface, the prefix to the BGP output network list, and an accept rule to the export filter | Adds the prefix's first address on `Loopback1` in CONFIG_DB and a BGP `network` statement in FRR |
| `bgp_acl_block` | Ingress and egress ACL `deny tcp any any eq bgp` on every addressed interface | `input` and `output` firewall filter rules that drop TCP port 179 | Unsupported; requires a native SONiC ACL implementation. |
| `ospf_acl_block` | Ingress ACL `deny ospf any any` on every addressed interface | `input` and `output` firewall filter rules that drop `protocol=ospf` | Unsupported; requires a native SONiC ACL implementation. |
| `host_static_blackhole` | Static `Null0` route | `/ip route add … blackhole` | FRR `ip route <prefix> blackhole` and `frr.conf` |
| `mtu_mismatch` | Sets the IPv4 MTU of the IOS XR interface | Sets the container interface MTU and the RouterOS ethernet port MTU | Writes the CONFIG_DB port MTU |
| `icmp_frag_needed_filter_misconfiguration` | IPv4 access list that denies ICMP packet-too-big, applied in both directions on every addressed interface | Firewall filter drop for ICMP type 3 code 4 | Unsupported; requires a native SONiC ACL implementation. |


Without `supported_backends`, a failure runs on Kathara and, on Containerlab, only on SONiC routers. A failure with an SR Linux implementation lists `containerlab` in `supported_backends`.

## Registered failures

### Link & Interface

On Kathara, link impairments run in a controller-owned VDE proxy outside the lab. Capacity limits, latency, packet corruption, and silent loss use `tc` on the proxy interfaces. `link_flap` toggles carrier on the lab TAP interfaces at both ends; nodes observe carrier transitions while the interfaces remain administratively up. Recovery removes the proxy and restores carrier. The injector preserves the lab interfaces and does not restart routing processes. Routing sessions can expire when an impairment interrupts their packets.

On Containerlab, link failures (`link_down`, `link_flap`, `link_capacity_bottleneck`, `link_packet_corruption`, `link_latency`) change carrier or apply `tc` on the [host-side wires](network-scenarios.md#backend-requirements). A down or flapping link drops carrier at both ends.

| Root cause | Failure ID | Scenarios | Parameters | Injection method | Trigger and signal | Verification evidence | Sources |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Link latency | `link_latency` | **All**, subject to the link corruption deployment constraints | `host_name`, optional `intf_name`, `delay_ms=100` | Adds one-way propagation delay in the Kathara controller proxy or Containerlab host-side wire. | Business-path RTT rises relative to an unaffected control path. | Reads back the external delay qdisc. Oracle probes local and cross-link latency and traces the link; ground truth labels the link. | [RIPE NCC, A Deep Dive Into the Baltic Sea Cable Cuts](#source-link_latency-1) |
| Link down | `link_down` | **All** | `host_name`, `intf_name=eth0` | Drops carrier on the logical link: Kathara brings down both point-to-point attachments; Containerlab brings down both host ends of the link's wire, so the nodes see lost carrier with the interface still administratively up. | The attachment reports operstate down; traffic crossing the link fails and routing adjacencies on it drop. | Endpoint operstate is down on the injected attachment (and peer when present). GT labels the undirected link TP set. | [iproute2, ip-link(8)](#source-link_down-1); [Linux kernel, Operational States](#source-link_down-2) |
| Link flap | `link_flap` | **All** | `host_name`, `intf_name=eth0`, `down_time=1`, `up_time=1` | Runs the flap loop outside the lab node. Kathara toggles both endpoint TAP carriers through a controller-owned VDE proxy. Containerlab uses a controller-host worker that toggles both host ends of the link's [host-side wire](network-scenarios.md#backend-requirements). Generated ISP access-link and SR Linux access-port tasks use a 12-second up period so carrier recovery settles before the next flap. | Sustained ping or protocol sessions show periodic loss and adjacency churn. | The benchmark controller confirms the hidden flap worker and restores the logical link during recovery or lab teardown. Test-path probes require a healthy baseline (≤5% loss), then observe periodic packet loss and carrier transitions with the configured flap periods. | [Linux kernel, Operational States](#source-link_flap-1) |
| Link capacity bottleneck | `link_capacity_bottleneck` | **All** | `host_name`, `intf_name=eth0`, `rate=200kbit`, `burst=64kb`, `limit=500kb` | Applies a controller-side token-bucket filter to traffic leaving `host_name:intf_name` (Kathara VDE proxy or Containerlab host veth peer), like a carrier circuit provisioned below the ordered rate. Endpoint interface qdisc stays ordinary. In environments with [business traffic](network-scenarios.md#business-traffic), benchmark rows shape an egress that the traffic loads with at least 1.5 Mbit/s of bulk TCP (`bulk_transit`) down to a 512 kbit/s tier with a 32 kB queue, so IGP and BGP control traffic still fits. On a declared fan-in workload, generation selects the forwarding egress toward its receiver and shapes it to 128 kbit/s with a 16 kB queue. | The loaded egress flat-tops at the shaped rate; bulk flows across it lose throughput and retransmit, and RTT across the link rises by the queue delay, while small pings still succeed. | The controller confirms the hidden TBF; short iperf3 transfers measure throughput well below an unthrottled path. GT labels the undirected link TP set. | [Alizadeh et al., Data Center TCP (DCTCP), SIGCOMM 2010](#source-link_capacity_bottleneck-1); [Phanishayee et al., Measurement and Analysis of TCP Throughput Collapse in Cluster-based Storage Systems, FAST 2008](#source-link_capacity_bottleneck-2); [Vasudevan et al., Safe and Effective Fine-grained TCP Retransmissions for Datacenter Communication, SIGCOMM 2009](#source-link_capacity_bottleneck-3); [Chen et al., Understanding TCP Incast Throughput Collapse in Datacenter Networks, WREN 2009](#source-link_capacity_bottleneck-4); [iproute2, tc-tbf(8)](#source-link_capacity_bottleneck-5) |
| Link packet corruption | `link_packet_corruption` | **All** | `host_name`, `intf_name` optional, `corruption_percentage=8` | Applies low-rate `tc netem corrupt` to IP frames leaving `host_name:intf_name`, in the Kathara VDE fault proxy or on the Containerlab link's host-side wire end. Non-IP frames such as ARP are lost at the same rate instead, as a receiver's FCS check would discard them. | The link stays up while probe traffic can show partial loss, TCP retransmissions increase, throughput falls, and applications remain partially usable. Endpoint qdisc queries stay ordinary. | The controller confirms the hidden corrupt qdisc and operstate up on both link ends. Test-path probes measure partial ping loss, iperf degradation, and HTTP success. GT labels the undirected link TP set. | [iproute2, tc-netem(8)](#source-link_packet_corruption-1) |
| Silent egress packet loss | `silent_egress_packet_loss` | `p4_dc_gateway` | `host_name`, `intf_name`, `bmv2_port`, `loss_basis_points=200`, `seed` (unused) | Drops, in the Kathara VDE fault proxy, `loss_basis_points` of the frames leaving `host_name:intf_name` toward the peer, at random (`netem loss`), like a defective transceiver or port transmit path that discards frames after the switch counted them. Nothing changes inside any lab node or in the P4 program: no table, register, qdisc, file, or process names the fault. Based on [Everflow, Section 7.2.2, paper page 9](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/08/everflow-sigcomm15.pdf). | Flows that ECMP-hash onto the port lose about two percent of their packets in that direction only; the reverse direction and the other uplinks stay clean. The link stays up and P4Runtime state matches the intent. The switch's egress packet counter for the port exceeds the peer's ingress counter for the same link. | INT reports observe packets after dequeue at the affected egress without a corresponding downstream ingress observation before the next queue. Queue measurements and endpoint probes accompany the delivery counts. | [Zhu et al., Packet-Level Telemetry in Large Datacenter Networks (Everflow), SIGCOMM 2015](#source-silent_egress_packet_loss-1) |

### Routing & Control Plane

| Root cause | Failure ID | Scenarios | Parameters | Injection method | Trigger and signal | Verification evidence | Sources |
| --- | --- | --- | --- | --- | --- | --- | --- |
| BGP route leak | `bgp_route_leak` | Kathara FRR base ISP eBGP graphs with two peering providers and a multihomed customer AS | `host_name`, optional `peer` | Bypasses the customer export guard on one upstream session. | The observer selects a path through the customer AS; the original origin remains RPKI valid and business traffic detours. | Confirms the export binding. Oracle checks the original AS-path origin, RPKI validity, received route and export policy; ground truth labels the customer border router. Recovery restores the prior binding. | [RFC 7908, Problem Definition and Classification of BGP Route Leaks](#source-bgp_route_leak-1); [RFC 9234, Route Leak Prevention and Detection Using Roles in UPDATE and OPEN Messages](#source-bgp_route_leak-2); [Cloudflare, How Verizon and a BGP Optimizer Knocked Large Parts of the Internet Offline Today](#source-bgp_route_leak-3) |
| BGP ASN mismatch | `bgp_asn_misconfig` | **BGP** | `host_name` | Changes the local ASN on the running BGP instance (FRR, SR Linux, IOS XR, RouterOS, or SONiC) and keeps the startup file in agreement. | Peers reject or reset sessions because the configured remote AS no longer matches. | Running configuration contains the changed ASN. | [RFC 4271, A Border Gateway Protocol 4 (BGP-4)](#source-bgp_asn_misconfig-1) |
| BGP blackhole community leak | `bgp_blackhole_community_leak` | **ISP eBGP with the RTBH profile** (`isp_*` with `--bgp-mode ebgp` where AS65002 peers an AS65003 router that also peers AS65001) | `host_name` (leaker from inventory) | Adds the RTBH provider blackhole community on the leaker AS export policy toward AS3 for a legitimately originated prefix. Kathara+FRR only. Inspired by [IMC 2018 RTBH without prefix hijacking](https://dl.acm.org/doi/10.1145/3278532.3278557). | AS3 installs discard forwarding for the prefix; data-plane probes fail; BGP sessions stay Established; origin ASN unchanged. | Origin preserved; community present on AS3; RTBH forwarding active; sessions Established. Test-path probes check that the observer loses reachability. `host_name` must equal the profile leaker. | [RFC 5635, Remote Triggered Black Hole Filtering with uRPF](#source-bgp_blackhole_community_leak-1) |
| BGP maximum-prefix limit too low | `bgp_max_prefix_limit_low` | **ISP eBGP** (`isp_*` with `--bgp-mode ebgp`; Kathara FRR) (combination member only) | `receiver_name`, `peer_name`, optional `neighbor_ip`, `maximum_prefix` | Sets one session's terminating limit at its current received count or one above it. | Control-plane evidence: the session stays Established with at most one prefix of headroom. | The configured limit matches the captured normal count; recovery restores the original limit. GT labels the receiver. | [RFC 7454, BGP Operations and Security](#source-bgp_max_prefix_limit_low-1) |
| BGP prefix surge | `bgp_prefix_surge` | **ISP eBGP** (`isp_*` with `--bgp-mode ebgp`; Kathara FRR) (combination member only) | `receiver_name`, `peer_name`, optional `neighbor_ip`, `flood_count` | Adds scattered `/24` announcements from `198.19.0.0/16` at the peer. Applies the supporting import/export permit to every BGP router. | Receiving tables grow; sessions with headroom stay Established. | The peer originates the additional routes; the symptom check compares the receiver's table with its healthy count. Recovery withdraws only these advertisements and supporting permits. GT labels the peer. | [Australian Senate, Chapter 2: The Optus network outage](#source-bgp_prefix_surge-1) |
| RPKI origin validation not enforced | `bgp_rov_not_enforced` | **RPKI** (`isp_*` with `--bgp-mode ebgp` on Kathara FRR, where route leaks are admitted) (combination member only) | `host_name` (defaults to the ROV observer) | Removes the invalid-deny sequence from BGP-IN on the ROV router. Keeps the RTR session and remaining import policy intact. Recovery restores the captured sequence and checks the complete policy. | Latent control-plane fault: the attached import policy no longer rejects RPKI-invalid routes while the cache remains connected. Alone, it leaves traffic unchanged. Combined with an invalid-origin leak, it admits invalid paths. | The invalid-deny sequence is absent, the remaining policy matches the snapshot, imports remain attached, and the cache is connected. GT labels the ROV router. | [RFC 6811, BGP Prefix Origin Validation](#source-bgp_rov_not_enforced-1); [FRR BGP documentation, Validating BGP Updates](#source-bgp_rov_not_enforced-2); [Cloudflare, How Verizon and a BGP Optimizer Knocked Large Parts of the Internet Offline Today](#source-bgp_rov_not_enforced-3) |
| Missing BGP advertisement | `bgp_missing_route_advertisement` | **BGP** | `host_name`, optional `prefix` / symptom probe fields | On ISP, targets a BGP originator and withdraws its originated prefix via FRR `no network` (OSPF untouched). Clos-style FRR environments remove BGP `network` statements from the running configuration and save it. Enterprise Site Edges disable BGP VRF `redistribute connected`. Containerlab SR Linux applies an export-policy withdraw (prefers `prefix` when set). IOS XR, RouterOS, and SONiC withdraw through their BGP network operations. | Peers stay reachable while they lose the selected prefix; IGP stub paths remain up. | FRR BGP config lacks the advertisement (or redistribute), SR Linux applies the withdrawal policy, or the platform no longer lists the prefix. | [FRRouting, BGP Networks](#source-bgp_missing_route_advertisement-1) |
| Routing service stopped | `routing_service_down` | **FRR** and SONiC routers (FRR under supervisord) | `host_name` | Stops the router's routing stack through its platform's process controls. | Dynamic adjacencies fail and route exchange stops. Existing forwarding entries can persist. | Routing processes are stopped; on FRR, zebra is absent and `vtysh` cannot connect. | [FRRouting, WATCHFRR](#source-routing_service_down-1) |
| OSPF area misconfiguration | `ospf_area_misconfiguration` | **OSPF** | `host_name` | Changes an OSPF network statement or interface area in the running daemon and the startup file. IOS XR, RouterOS, and SONiC use their OSPF area operations; SR Linux is unsupported. | Adjacency fails on links whose endpoints no longer agree on area. | The platform OSPF configuration shows the changed area; FRR also persists it in its startup file. | [RFC 2328, OSPF Version 2](#source-ospf_area_misconfiguration-1) |
| OSPF interface not enabled | `ospf_interface_not_enabled` | **OSPF** | `host_name` | Removes the FRR network statement or interface-area binding covering one adjacency from `frr.conf` and the daemon. Other platforms delete the OSPF interface of one adjacency from their running configuration. | The router stops forming expected OSPF adjacencies and loses learned routes. | The statement is absent from `frr.conf` and the running daemon, or the interface is absent from the platform's OSPF interfaces while the others remain. | [RFC 2328, OSPF Version 2](#source-ospf_interface_not_enabled-1) |
| OSPF duplicate router ID | `ospf_duplicate_router_id` | **OSPF** (Kathara FRR, IOS XR, RouterOS; Containerlab SR Linux) | `host_name` | Sets the OSPF router ID to another router's ID in the same area. FRR updates its running daemon and `frr.conf` and restarts OSPF; IOS XR and RouterOS use native router operations, and SR Linux updates the OSPF instance. It prefers the lowest non-adjacent router ID; when every router in those areas is a neighbor, it takes the lowest neighbor ID. Based on [Cisco, Troubleshooting Duplicate Router IDs with OSPF](https://www.cisco.com/c/en/us/support/docs/ip/open-shortest-path-first-ospf/23862-duplicate-router-id-ospf.html). | With a non-adjacent duplicate, the two routers may re-flood their router-LSAs against each other for a few minutes. Conflicting router-LSAs can remove routes to either duplicate's links and subnets while nonadjacent routers retain Full adjacencies; the outcome depends on LSA timing. With a neighbor's ID, that adjacency never forms. Hosts behind the losing router lose connectivity, and FRR logs nothing at its default level. | The running OSPF router ID equals the chosen duplicate. | [Cisco, Troubleshooting Duplicate Router IDs with OSPF](#source-ospf_duplicate_router_id-1) |
| BGP TCP MD5 password mismatch | `bgp_md5_password_mismatch` | `dc_clos`, `enterprise_branch`, `isp_abilene` with eBGP or iBGP route reflection (Kathara FRR, IOS XR, RouterOS) | `host_name`, optional `neighbor_ip` | Configures a password on one side of one established IPv4 BGP session and resets that session. FRR uses `neighbor ... password`, IOS XR uses `neighbor ... password clear`, and RouterOS uses `tcp-md5-key`. FRR first checks kernel TCP MD5 support. Benchmark targets in declared BGP plans originate a prefix. The enterprise IOS XR profile keeps its overlay edges on FRR. Based on [RFC 2385](https://www.rfc-editor.org/rfc/rfc2385). | The session leaves Established and the peer withdraws paths learned from the target. FRR exposes kernel TCP MD5 error counters; IOS XR logs invalid MD5 digests. Alternate BGP paths can preserve connectivity. | The selected neighbor has a password and its session is not Established. Recovery removes that password and waits for the original sessions; E2E checks also require the withdrawn paths to return. Ground truth identifies the target router. | [RFC 2385, Protection of BGP Sessions via the TCP MD5 Signature Option](#source-bgp_md5_password_mismatch-1) |
| OSPF area range suppression | `ospf_area_range_suppression` | **OSPF** (`campus_lan`: FRR, IOS XR; RouterOS size `s`) | `host_name`, `area` | Adds `not-advertise` area ranges on the area border router (ABR), or RouterOS `advertise=no`. The ranges exactly cover that ABR's non-backbone branch subnets. Targets require a single ABR per connected area component; campus area 1 has two separate branches, each with its own ABR. Based on [RFC 2328 §12.4.3 and Appendix C.2](https://www.rfc-editor.org/rfc/rfc2328#section-12.4.3). | Backbone Type-3 LSAs and remote routes to the branch disappear. Adjacencies remain Full and branch routers retain their intra-area reachability. Recovery removes the ranges and checks restored routes, summaries, and connectivity. | The ABR's running configuration contains every computed suppressing range. | [RFC 2328, OSPF Version 2](#source-ospf_area_range_suppression-1) |
| OSPF network type mismatch | `ospf_network_type_mismatch` | **OSPF** (Kathara FRR and RouterOS; scenarios with a cut-edge OSPF link) | `host_name`, `intf_name` | Changes one side of a cut-edge OSPF link between broadcast and point-to-point. FRR updates the running daemon and startup file; RouterOS changes the existing interface-template to `type=ptp` or `type=broadcast` and rejects templates shared by other interfaces. Benchmark rows can target either end of a link to a router with only one router neighbor, so the link carries that site's traffic. Based on [Cisco, Troubleshoot OSPF Route Database Issues](https://www.cisco.com/c/en/us/support/docs/ip/open-shortest-path-first-ospf/7112-26.html). IOS XR and [SR Linux 24.10](https://documentation.nokia.com/srlinux/24-10/books/routing-protocols/ospf.html) are unsupported: both drop the adjacency instead of retaining the required Full adjacency. | Hello and dead timers still match, so the adjacency reaches Full. The two router-LSAs describe the link differently (transit vs point-to-point), SPF's two-way check rejects it, and hosts behind the stub router lose connectivity. | The running OSPF network type on the interface is the injected value. | [Cisco, Troubleshoot Open Shortest Path First Route Database Issues](#source-ospf_network_type_mismatch-1) |
| OSPF hello interval mismatch | `ospf_hello_interval_mismatch` | **OSPF** (Kathara FRR, IOS XR, RouterOS, Containerlab SR Linux; cut-edge links serving hosts) | `host_name`, `intf_name` | Changes one interface's hello and dead intervals, choosing 5/20, 10/40, or 15/60 seconds so both differ from the neighbor. RouterOS changes the existing interface template by its stable ID and rejects shared templates. [SR Linux](https://documentation.nokia.com/srlinux/24-10/books/data-model-reference/srl_nokia-network-instance_0.html#d4839) changes `hello-interval` and `dead-interval` on the selected area interface. Based on [RFC 2328 section 10.5](https://www.rfc-editor.org/rfc/rfc2328#section-10.5). | Neighbors discard Hellos with different timers. The adjacency expires, learned routes withdraw, and hosts behind the cut edge lose reachability while interfaces stay up. Recovery restores the original timers and waits for Full adjacency and learned routes. | The target's running hello and dead intervals equal the injected values and differ from its neighbor's. Ground truth identifies the target router. | [RFC 2328, OSPF Version 2](#source-ospf_hello_interval_mismatch-1) |
| OSPF stub-area mismatch | `ospf_stub_area_mismatch` | `campus_lan`, area 1 (Kathara FRR, IOS XR, RouterOS; cut edges serving hosts) | `host_name`, `intf_name` | Declares area 1 stub on one router while its peer keeps a normal area: FRR and IOS XR `area 1 stub`; RouterOS sets `type=stub` on the matching `/routing ospf area` entry and restarts the owning instance to apply the changed Hello capability. Targets prefer native routers; campus IOS XR uses core routers because distribution gateways remain FRR. Sources: the standards [RFC 2328 sections 10.5 and A.2](https://www.rfc-editor.org/rfc/rfc2328) and [RFC 1583 section 4.5](https://freesoft.org/CIE/RFC/1583/24.htm), and the Cisco Community vendor troubleshooting document [OSPF routers do not form neighbor relationship due to a mismatch in hello parameters](https://community.cisco.com/t5/networking-knowledge-base/ospf-routers-do-not-form-neighbor-relationship-due-to-a-mismatch/ta-p/3131411). | Neighbors reject Hellos whose E-bit disagrees. RouterOS logs `external capability mismatch`. Full adjacency and learned routes disappear, and hosts behind the router lose reachability while interfaces stay up. Area IDs, timers, and network types retain their healthy values. Recovery restores normal area 1 and waits for Full adjacency and the original learned routes. | The target runs stub area 1 and its peer runs normal area 1. The symptom probe compares adjacency, route withdrawal, and host packet loss against a healthy baseline. Ground truth identifies the target router. | [RFC 2328, OSPF Version 2](#source-ospf_stub_area_mismatch-1) |
| IS-IS metric-style mismatch | `isis_metric_style_mismatch` | Base `isp_*` with `--igp isis` (Kathara FRR, IOS XR, RouterOS; benchmark rows use `--bgp-mode none`) | `host_name` | Sets the router to old-style (narrow) IS-IS metrics while its neighbors keep wide metrics: FRR `metric-style narrow` under `router isis`; IOS XR `metric-style narrow` under `address-family ipv4 unicast` in `router isis`; RouterOS `metric-type=old` on the IS-IS instance. Based on [RFC 5305](https://www.rfc-editor.org/rfc/rfc5305) wide metrics (TLV 135). SR Linux 24.10 accepts `metric-style narrow` but retains learned routes and Up adjacencies, so it is excluded. See [Nokia SR Linux IS-IS](https://documentation.nokia.com/srlinux/24-10/books/routing-protocols/is-is.html). | IS-IS adjacencies stay Up while neighbors stop installing the target's loopback and stub prefixes. The symptom probe checks the routers that held those routes before injection. | The running configuration uses narrow IS-IS metrics. Recovery sets wide metrics. | [FRRouting 10.5, ISIS](#source-isis_metric_style_mismatch-1) |
| IS-IS router level mismatch | `isis_router_level_mismatch` | Base `isp_*` with `--igp isis` (Kathara FRR, IOS XR, RouterOS, Containerlab SR Linux; benchmark rows use `--bgp-mode none`) | `host_name` | Sets the router to IS-IS Level-1 while its neighbors stay Level-2 only: FRR `is-type level-1` under `router isis`; IOS XR `is-type level-1` plus `circuit-type level-1` on IS-IS interfaces; RouterOS `levels=l1` on every IS-IS interface-template. An interface `circuit-type level-1` alone is ignored while the instance stays Level-2 only. Based on [RFC 1195](https://www.rfc-editor.org/rfc/rfc1195) Level-1/Level-2 adjacency rules and [Cisco, Configure IS-IS Adjacency and Area Types](https://www.cisco.com/c/en/us/support/docs/ip/integrated-intermediate-system-to-intermediate-system-is-is/200293-IS-IS-Adjacency-and-Area-Types.html). [SR Linux](https://documentation.nokia.com/srlinux/24-10/books/routing-protocols/is-is.html) sets `level-capability L1` and moves the existing instance and interface Level-2 settings to Level 1. Recovery restores the saved Level-2 settings. | IS-IS adjacencies with the router drop. The symptom probe requires each observed loopback or stub prefix to disappear from at least one former neighbor; cached routes can persist elsewhere until the LSP expires. | The running configuration uses IS-IS Level-1. Recovery restores the saved level. | [FRRouting 10.5, ISIS](#source-isis_router_level_mismatch-1) |
| IS-IS authentication mismatch | `isis_authentication_mismatch` | Base `isp_*` with `--igp isis` (Kathara FRR, IOS XR, Containerlab SR Linux; benchmark rows use `--bgp-mode none`) | `host_name` | Enables HMAC-MD5 hello authentication on the target router's non-passive IS-IS interfaces while neighbors remain unauthenticated: FRR `isis password md5`; IOS XR `hello-password hmac-md5 clear`. Based on [RFC 5304](https://www.rfc-editor.org/rfc/rfc5304). [SR Linux](https://documentation.nokia.com/srlinux/24-10/books/routing-protocols/is-is.html) configures an interface `authentication key` with `crypto-algorithm hmac-md5`, enables Hello authentication generation, and sets `check-received strict`. RouterOS 7.21.5 exposes no IS-IS authentication parameters. | Adjacencies with the router drop and its loopback or stub prefixes disappear from former neighbors. IS-IS circuits stay Up. | The running interface configuration requires MD5 hello authentication. Recovery removes authentication and waits for the baseline adjacencies and routes. | [RFC 5304, IS-IS Cryptographic Authentication](#source-isis_authentication_mismatch-1) |
| One-sided BGP TTL security | `bgp_ttl_security_mismatch` | **BGP** (Kathara FRR, IOS XR); base `isp_*` needs `--bgp-mode ebgp` | `host_name` | Enables one-sided TTL security (GTSM, [RFC 5082](https://www.rfc-editor.org/rfc/rfc5082)) on every BGP neighbor and resets the sessions. FRR adds `neighbor … ttl-security hops 1` to the running daemon and startup file; IOS XR adds `ttl-security` under each eBGP neighbor, the only neighbors on which IOS XR accepts it. RouterOS and SR Linux are unsupported by this failure. The peers keep their default TTL. Based on [AWS Direct Connect, Troubleshoot BGP TTL security](https://docs.aws.amazon.com/directconnect/latest/UserGuide/ts-ttl-security.html). | The router rejects low-TTL BGP traffic, so its eBGP peers, which send with TTL 1, cannot establish sessions. FRR also rejects iBGP packets that arrive below TTL 255 from peers more than one hop away; directly connected iBGP peers still get through. On FRR, the router stays in OpenSent or Active and its peers in OpenConfirm or Active. No ACL or firewall rule matches TCP 179, and a packet capture shows the peers' BGP packets arriving with TTL 1 (eBGP) or another value below 255 (multi-hop iBGP). | Every BGP neighbor in the running configuration carries the platform's TTL security setting. | [AWS Direct Connect, Troubleshoot BGP TTL security (GTSM) issues](#source-bgp_ttl_security_mismatch-1) |
| BGP IPv4 address family not activated | `bgp_address_family_not_activated` | `dc_clos`, `enterprise_branch`, base `isp_*` with eBGP or iBGP route reflection (`--device-profile routeros`, Kathara) | `host_name`, optional `neighbor_ip` | Removes `ip` from one established neighbor's `afi` and re-enables the connection to negotiate the changed family. The target must carry IPv4 prefixes before injection. Based on [RFC 4760 section 8](https://www.rfc-editor.org/rfc/rfc4760#section-8) and [RouterOS BGP address families](https://help.mikrotik.com/docs/spaces/ROS/pages/331612228/routing%2Bbgp). FRR and IOS XR are unsupported in these environments: removing their last active family leaves the session Idle. | The session reaches Established, its local `afi` omits `ip`, its received prefix count falls to zero, and its IPv4 routes from that peer disappear. `bgp_session_policy_missing` retains the negotiated IPv4 family and rejects routes through policy; inspect the local and remote address families and neighbor configuration to distinguish them. Alternate peers can preserve connectivity. | The connection configuration lacks `ip`. The symptom probe requires absent IPv4 negotiation and zero received prefixes against a healthy baseline. Recovery restores the neighbor's original family configuration, re-establishes its session, and waits for the original IPv4 prefixes. Ground truth identifies the target router. | [MikroTik RouterOS, /routing/bgp](#source-bgp_address_family_not_activated-1) |
| BGP session policy missing | `bgp_session_policy_missing` | `dc_clos` and `enterprise_branch` with `--device-profile routeros`; base `isp_*` with `--bgp-mode ebgp` and `--device-profile iosxr` or `routeros` | `host_name` | On IOS XR, removes the `route-policy` from every eBGP neighbor, so XR applies its [RFC 8212](https://www.rfc-editor.org/rfc/rfc8212) default. On RouterOS, sets each eBGP connection's `input.filter` and `output.filter-chain` to chains that no rule defines, which RouterOS treats as rejecting every route. Recovery restores the saved router configuration. Targets are native routers with eBGP sessions and attached hosts; FRR routers are not targets because the environments run FRR without `ebgp-requires-policy`. SR Linux is unsupported: its environments forward the host prefixes over the IGP, so dropping BGP routes leaves clients reachable. | Every eBGP session stays Established while no route crosses it in either direction, so hosts behind the router lose connectivity. XR shows `0!` in `show bgp summary` and notes that the neighbor has no policy; RouterOS logs only the sessions coming up. | XR: no eBGP neighbor carries a `route-policy`. RouterOS: every eBGP connection names input and output chains with no rules. | [RFC 8212, Default External BGP Route Propagation Behavior without Policies](#source-bgp_session_policy_missing-1); [MikroTik RouterOS, Route Selection and Filters](#source-bgp_session_policy_missing-2) |
| BGP session policy missing | `bgp_session_policy_missing` | `dc_clos` with `--device-profile iosxr` or `routeros`; `enterprise_branch` with `--device-profile routeros` | `host_name`, optional `peer` | On IOS XR, removes the `route-policy` from the selected eBGP neighbor, or every neighbor when `peer` is omitted, so XR applies its [RFC 8212](https://www.rfc-editor.org/rfc/rfc8212) default. On RouterOS, sets the selected eBGP connections' `input.filter` and `output.filter-chain` to chains that no rule defines, which RouterOS treats as rejecting every route. Recovery restores the selected peer's policy bindings, or the saved router configuration for whole-router scope. Single-failure targets are native routers with eBGP sessions and attached hosts; FRR routers are not targets because the environments run FRR without `ebgp-requires-policy`. SR Linux is unsupported: its environments forward the host prefixes over the IGP, so dropping BGP routes leaves clients reachable. | Selected eBGP sessions stay Established while no route crosses them in either direction. Other sessions can preserve connectivity; whole-router scope cuts off the attached hosts. XR shows `0!` in `show bgp summary` and notes that the neighbor has no policy; RouterOS logs only the sessions coming up. | XR: selected eBGP neighbors carry no `route-policy`, and the global unsafe-policy bypass is disabled. RouterOS: selected eBGP connections name input and output chains with no rules. Ground truth uses the directly connected interface for a selected peer and the node for whole-router scope. Single-failure benchmark targets retain whole-router scope. | [RFC 8212, Default External BGP Route Propagation Behavior without Policies](#source-bgp_session_policy_missing-1); [MikroTik RouterOS, Route Selection and Filters](#source-bgp_session_policy_missing-2) |
| VRF import route-target mismatch | `vrf_route_target_mismatch` | `enterprise_branch` (Kathara; FRR or RouterOS Site Edges, including FRR edges in the IOS XR profile) | `host_name`, `vrf_name=vrf_corp` | Replaces the selected business VRF's overlay import with a mistyped route target on RouterOS or the wrong source VRF on FRR, preserving its other imports and exports. Recovery restores the original imports and waits for remote routes and endpoint connectivity. Related incidents: the [NHS Wales incident report](https://swanseabay.nhs.wales/about-us/our-health-board/our-board-business/health-board-committees/digital-data-research-and-innovation-committee/may-2026/4-1i-appendix-1-wales-wide-outage-pir-pdf/) (a route-leaking change admitted an unintended default) and a [NANOG operator discussion](https://seclists.org/nanog/2011/Mar/457) (VPN updates lost RT communities). [RFC 4364 §4.3.1](https://www.rfc-editor.org/rfc/rfc4364.html#section-4.3.1) and the [MikroTik vendor configuration document](https://help.mikrotik.com/docs/spaces/ROS/pages/328206/Virtual+Routing+and+Forwarding+-+VRF) define import matching. The [FRR route-leaking reference](https://docs.frrouting.org/en/latest/bgp.html#clicmd-import-vrf-VRFNAME) defines source VRF imports. | The selected VRF loses remote-site routes and connectivity while BGP sessions, WireGuard tunnels, overlay routes, the local gateway, and other VRFs stay healthy. | The selected VRF imports the mistyped RT on RouterOS or `vrf_guest` instead of `default` on FRR. Test-path probes compare healthy and faulty routes, tunnel handshakes, and endpoint pings. GT labels the Site Edge node. | [RFC 4364, BGP/MPLS IP Virtual Private Networks](#source-vrf_route_target_mismatch-1); [FRRouting, BGP Route Leaking](#source-vrf_route_target_mismatch-2) |
| BGP prefix-list length mismatch | `bgp_prefix_list_length_mismatch` | `enterprise_branch` (Kathara; FRR or RouterOS site edges, including FRR edges in the IOS XR profile) | `host_name` | Changes the bound import filter from `10.0.0.0/8 le 24` to an exact `/8` match. RouterOS uses `dst == 10.0.0.0/8`. [FRR vendor semantics](https://docs.frrouting.org/en/latest/filter.html#ip-prefix-list) treat an omitted `le` as an exact-length match, and the [Cisco vendor configuration example](https://www.cisco.com/c/en/us/support/docs/ip/border-gateway-protocol-bgp/13750-22.html) shows length-bounded filters; the [BitFolk 2016 incident report](https://mailman.bitfolk.com/lurker/message/20160501.204902.6ff9c7e5.en.html) describes a filter rebuild that rejected more-specific routes. The [RouterOS vendor reference](https://help.mikrotik.com/docs/spaces/ROS/pages/74678285/Route+Selection+and+Filters) defines prefix equality and length matching; [Mahajan et al., SIGCOMM 2002](https://www.caida.org/archive/networkingbib/entries/mahajan02understanding/) provide a broader measurement study. | BGP sessions remain Established and import policies remain bound, but remote business `/24`s disappear and site traffic fails. ISP allow-lists already enumerate exact `/24`s, and `dc_clos` has no equivalent length-bounded import filter. | The bound import filter matches only `/8`. Test-path probes require healthy reachability and learned routes, then route loss and 100% ping loss with the same Established peers. Recovery restores the length range and waits for the original Established peers and learned routes. Ground truth identifies the router whose filter changed. | [FRRouting, Filtering](#source-bgp_prefix_list_length_mismatch-1); [MikroTik RouterOS, Route Selection and Filters](#source-bgp_prefix_list_length_mismatch-2) |

### Forwarding, Encapsulation & Policy

| Root cause | Failure ID | Scenarios | Parameters | Injection method | Trigger and signal | Verification evidence | Sources |
| --- | --- | --- | --- | --- | --- | --- | --- |
| RIB/FIB inconsistency | `rib_fib_inconsistency` | `dc_clos`, `campus_lan`, and `isp_*` (Kathara FRR) | `host_name` (FRR router with an attached endpoint and a reachable remote endpoint) | Replaces one selected dynamic route's Linux main-table next hop with the attached endpoint's address, retaining its prefix, routing protocol, and metric. The FRR best path stays unchanged. Recovery deletes the divergent entry so zebra reinstalls the original path. | Traffic to the selected destination fails while the router's BGP, OSPF, or IS-IS sessions and FRR route remain healthy. | The selected FRR route is unchanged, while the matching kernel route uses the injected next hop with its original protocol and metric. Recovery checks restored forwarding, unchanged sessions, and endpoint reachability. | [FRRouting 10.5.3, zebra/rt_netlink.c](#source-rib_fib_inconsistency-1) |
| Silent forwarding-device corruption | `device_forwarding_packet_corruption` | `dc_clos`, `campus_lan`, `enterprise_branch`, `sdn_l3_clos` (SR Linux and SONiC), and the RouterOS deployments of `dc_clos`, `campus_lan` and `enterprise_branch` | `forwarding_device`, `intf_name` (observation anchor), `seed` | Applies one deterministic flow selector to every data port of the forwarding device, outside the node. Selected TCP payloads change after ingress validation and before the modeled egress FCS generation; their TCP checksum stays unchanged. Locally originated routing traffic passes. | Independent service paths through different ports produce new TCP checksum errors at their receivers. The Oracle requires two paths that share the target device and no link. | The controller checks that every data-port classifier is attached. | [Linux BPF helper reference, bpf-helpers(7)](#source-device_forwarding_packet_corruption-1); [Stone and Partridge, When the CRC and TCP Checksum Disagree, SIGCOMM 2000](#source-device_forwarding_packet_corruption-2) |
| MTU mismatch | `mtu_mismatch` | `dc_clos`, `campus_lan`, `enterprise_branch`, `k8s_lab`, Kathara `isp_abilene` (IS-IS, OSPF, iBGP RR, eBGP), every SONiC environment, and the IOS XR and RouterOS deployments of `dc_clos`, `campus_lan`, `enterprise_branch` and base `isp_*` without BGP | `host_name` (intermediate L3 router), `intf_name`, `mtu=500`. Legacy id `link_fragmentation_disabled` resolves to this failure. | Sets the interface MTU on that one interface only, so it no longer matches the far end of its link: `ip link set … mtu` on Linux/FRR, CONFIG_DB on SONiC, and the interface IPv4 MTU on IOS XR, and the container plus ethernet port on RouterOS. | OSPF over the link stops at ExStart/Exchange on the Database Description MTU check ([RFC 2328 section 10.6](https://www.rfc-editor.org/rfc/rfc2328#section-10.6)); routed DF packets that leave through the interface elicit ICMP Fragmentation Needed (type 3 / code 4). Small packets can pass. DF packets exceeding this MTU fail until the sender adapts; working PMTUD can allow TCP transfers to resume. | `ip link` or the platform port reports the lowered MTU. GT labels the misconfigured interface. | [RFC 1191, Path MTU Discovery](#source-mtu_mismatch-1) |
| Per-flow forwarding black hole | `device_flow_blackhole` | `dc_clos`, `campus_lan`, `enterprise_branch`, `sdn_l3_clos` (Kathara; FRR/Linux or OVS, plus RouterOS on converted routers); `dc_clos` (Containerlab; SONiC or SR Linux) | `forwarding_device`, `intf_name` (observation anchor), `seed` | Applies the same seeded five-tuple hash to every data port, dropping all packets of selected forwarded TCP and UDP flows. ICMP and locally originated routing traffic pass. The impairment runs on controller-owned proxies or host-side wires. | Some fixed TCP tuples consistently time out while others connect and ICMP succeeds. The Oracle requires this behavior on two service paths that share the device and no link. Native interface discard counters can remain healthy, as in the silent black holes reported by Pingmesh. | The controller checks that every data-port classifier is attached. | [Guo et al., Pingmesh: A Large-Scale System for Data Center Network Latency Measurement and Analysis, SIGCOMM 2015](#source-device_flow_blackhole-1) |
| Kubernetes VXLAN overlay neighbor stale | `k8s_vxlan_neighbor_stale` | **Kubernetes** | `control_node` optional, `node_name`, `peer_name` | Replaces the permanent overlay neighbor on `flannel.1` for the peer node's VTEP with a locally administered unicast MAC that differs from the peer's current VTEP MAC in the last octet and that no node uses, like the address of a recreated `flannel.1`. This is an authored stale-mapping model; the injector does not recreate `flannel.1` or establish a flannel restart defect. Injection skips any MAC already in use on a node's `flannel.1`. Encapsulated frames toward that VTEP then have no usable VXLAN destination. k3s's default CNI is flannel VXLAN on UDP 8472. Based on [RFC 7348](https://www.rfc-editor.org/rfc/rfc7348) and [flannel VXLAN](https://github.com/flannel-io/flannel#vxlan). | Pod IPs on the peer node are unreachable from `node_name` while pods on other nodes stay reachable from it, the same peer pods stay reachable from a third node, and the node-to-node underlay and Kubernetes API stay healthy. | `ip neigh show dev flannel.1` lists the injected MAC for the peer VTEP. | [flannel, VXLAN backend implementation](#source-k8s_vxlan_neighbor_stale-1) |
| ARP ACL block | `arp_acl_block` | `campus_lan`, `dc_clos`, `enterprise_branch`, `sdn_l3_clos`, `p4_dc_fabric`, `p4_dc_gateway` | `host_name` | Adds an nftables ARP drop and flushes the neighbor cache, including `nud permanent` / static gateway entries used by Clos and P4 labs. Not compatible with `k8s_lab` / `llmd_lab` (client images lack nftables offline). | New ARP resolution fails; cached and permanent entries are removed so local-subnet and off-subnet traffic that needs ARP stops. | An ARP drop appears in nftables. | [Netfilter, nft(8)](#source-arp_acl_block-1) |
| Routing control-plane ACL block | `bgp_acl_block` | **BGP** | `host_name` | Drops TCP source and destination port 179: nftables on FRR, the SR Linux ACL equivalent, an interface ACL on IOS XR and a firewall filter on RouterOS. | BGP sessions reset or cannot establish; routes learned through those sessions disappear. | The port-179 drop exists in the platform's packet filter. | [Netfilter, nft(8)](#source-bgp_acl_block-1) |
| Switch/router crash | `bmv2_switch_down` | **P4** | `host_name` | Kills `simple_switch` / `simple_switch_grpc` on a BMv2 node (benchmark cases target a leaf or gateway on the probe path). | Every path through that switch fails and the P4 control CLI cannot reach the process. | No matching BMv2 dataplane process is running. | [P4lang, Behavioral Model targets](#source-bmv2_switch_down-1) |
| DNS transport blocked | `dns_port_blocked` | **DNS**, **Kubernetes** | `host_name` for a DNS server; `control_node`, `node_name` and DNS Service parameters for Kubernetes | Adds nftables drops for TCP and UDP destination port 53. Packets are dropped; the resolver does not generate an empty DNS answer. In Kubernetes, node firewall rules drop DNS Service and pod traffic on UDP and TCP port 53. | DNS queries time out while the BIND process and direct IP connectivity remain healthy. | Both DNS port rules appear in nftables. The Kubernetes adapter also checks the running CoreDNS pods and node filter. | [RFC 1035, Domain Names: Implementation and Specification](#source-dns_port_blocked-1); [Kubernetes, Debugging DNS Resolution](#source-dns_port_blocked-2) |
| Flow rule loop | `flow_rule_loop` | **SDN** | `host_name`, `host_name_2` | Installs paired IPv4 rules through ONOS, matching each selected inter-switch ingress port and applying TTL decrement plus OpenFlow `IN_PORT` output. | Traffic on the affected attachments reflects toward its incoming link and can loop with the adjacent path. | Both selected switches have the expected-priority IPv4 ingress-port rule with `IN_PORT` output. The checker does not assert TTL decrement. | [Open vSwitch, Using OpenFlow](#source-flow_rule_loop-1) |
| Flow rule shadowing | `flow_rule_shadowing` | **SDN** | `host_name` | Installs, through ONOS, an IPv4 drop for one rack `/24` (the highest-numbered rack other than the switch's own) at a priority above the per-prefix forwarding rules. ARP, LLDP, and other prefixes keep their intended rules. | Flows to that rack that cross the switch are dropped (all of them through a leaf, the ECMP share through a spine). Other racks, ARP, and ONOS links are unaffected. The rule's packet counter matches the lost packets. | OVS reports the prefix-scoped drop flow. | [Open vSwitch, ovs-ofctl(8)](#source-flow_rule_shadowing-1) |
| Host static blackhole | `host_static_blackhole` | **BGP** | `host_name` | Installs a static blackhole route for a remote endpoint prefix (preferring a non-locally-attached subnet) without advertising the route. FRR, SR Linux, IOS XR, RouterOS, and SONiC each install it through their route operations. | Traffic matching the victim prefix drops at the target router. | The native platform reports the discard route, or the Linux kernel route table contains the blackhole prefix. Reachability is checked separately. | [iproute2, ip-route(8)](#source-host_static_blackhole-1) |
| ICMP Fragmentation Needed filtering misconfiguration | `icmp_frag_needed_filter_misconfiguration` | `p4_dc_gateway`, plus the Linux/FRR, IOS XR and RouterOS deployments shared with `mtu_mismatch` | `host_name` (`gateway_1` on P4; intermediate router) | On `p4_dc_gateway`, inserts a P4Runtime ACL entry that drops ICMP Destination Unreachable / Fragmentation Needed (type 3/code 4), including the messages the gateway sends for its 1400-byte client access circuit, following [RFC 2923 section 2.1](https://www.rfc-editor.org/rfc/rfc2923.html). On Linux routers, installs an nftables drop for the same ICMP and ages out the path MTUs end hosts had cached (as `net.ipv4.route.mtu_expires` would). IOS XR and RouterOS install the equivalent filter through their packet-filter operations. | On `p4_dc_gateway`, services never learn the client's path MTU, so large downloads to clients behind the gateway stall while small requests still work. On `isp_abilene` variants, `atlam5` sits on the GRE-backhauled link (IP MTU 1476): data sets served by `pc_atlam5` stall while ping, TCP connects, and small replies still work. Elsewhere on Linux routers, combined with `mtu_mismatch` on the same path, large DF transfers black-hole. | P4Runtime reads the type 3/code 4 entry in the gateway `icmp_acl` (next to its baseline timestamp and address-mask drops), or the platform's packet filter lists the frag-needed drop. | [RFC 2923, TCP Problems with Path MTU Discovery](#source-icmp_frag_needed_filter_misconfiguration-1) |
| Host ACL block | `host_acl_block` | `dc_clos`, `campus_lan`, `enterprise_branch`, `sdn_l3_clos`, **P4**, `isp_*`, **Kubernetes** | `host_name`, `protocol=icmp` (`icmp` or `http`) | Adds a host packet-filter drop for ICMP or TCP port 80, selected by `protocol`. | The selected protocol fails while the other protocol and neighbor resolution remain healthy. | The host packet filter contains the selected drop rule. | [Netfilter, nft(8)](#source-host_acl_block-1) |
| Kubernetes policy deny | `k8s_networkpolicy_deny` | **Kubernetes** | `control_node` optional, `namespace`, `pod_selector`, `policy_name`, `symptom_host`, `symptom_url`, `control_url` | Applies a deny-all-ingress `NetworkPolicy` to selected pods. | Ingress to the selected Pods fails. Pods remain Running; kubelet probes can also be blocked, so Ready may flap. A sibling route is an optional isolation observation. | Policy exists, selected Pods are Running, and the symptom URL fails. Pod readiness and control URL success are reported but do not gate verification. | [Kubernetes, Network Policies](#source-k8s_networkpolicy_deny-1) |
| Routing control-plane ACL block | `ospf_acl_block` | **OSPF** | `host_name` | Drops IP protocol 89 in the router's packet filter: nftables on FRR, the CPM filter on SR Linux, an ingress interface ACL on IOS XR and the firewall filter on RouterOS. | OSPF neighbors time out and learned routes withdraw while unrelated protocols can pass. | The packet filter contains the OSPF drop. | [Netfilter, nft(8)](#source-ospf_acl_block-1) |
| Forwarding table entry misconfig | `p4_table_entry_misconfig` | `p4_dc_fabric`, `p4_dc_gateway` | `host_name`, optional `target_ip` and `observer_device` | Points a destination prefix at the wrong ActionSelector group through P4Runtime. | Matching traffic uses the wrong ECMP group while BMv2 stays up. | P4Runtime Read shows the recorded wrong group on the selected prefix. | [P4.org API Working Group, P4Runtime Specification 1.4.1](#source-p4_table_entry_misconfig-1); [Zhu et al., Packet-Level Telemetry in Large Datacenter Networks (Everflow), SIGCOMM 2015](#source-p4_table_entry_misconfig-2) |
| Forwarding table entry missing | `p4_table_entry_missing` | `p4_dc_fabric`, `p4_dc_gateway` | `host_name` | Deletes a destination prefix from `ipv4_lpm` through P4Runtime. | Packets needing that prefix miss or follow the default action. | A successful P4Runtime Read still contains other routes but lacks the selected prefix. | [P4.org API Working Group, P4Runtime Specification 1.4.1](#source-p4_table_entry_missing-1) |
| ActionSelector member misconfig | `p4_action_selector_member_misconfig` | `p4_dc_fabric`, `p4_dc_gateway` | `host_name` | Rewrites one ActionSelector member to a bogus egress port through P4Runtime. | Hashed flows that select that member fail; other members and other racks still forward. | P4Runtime member port differs from intent. | [P4.org API Working Group, P4Runtime Specification 1.4.1](#source-p4_action_selector_member_misconfig-1) |
| ECMP group member missing | `p4_ecmp_group_member_missing` | `p4_dc_fabric`, `p4_dc_gateway` | `host_name` | Deletes one member from a destination ActionSelector group. | Group membership shrinks; remaining members and other prefixes still forward. | P4Runtime Read shows the group without the selected member and with at least one remaining member. | [P4.org API Working Group, P4Runtime Specification 1.4.1](#source-p4_ecmp_group_member_missing-1) |
| P4Runtime pipeline mismatch | `p4runtime_pipeline_mismatch` | `p4_dc_fabric`, `p4_dc_gateway` | `host_name` | At inject time, compiles a drop-all P4 program and loads it with `SetForwardingPipelineConfig` on one switch. | Forwarding through that switch dies; other switches stay healthy. | A successful pipeline Read reports P4Info that differs from the intended pipeline. | [P4.org API Working Group, P4Runtime Specification 1.4.1](#source-p4runtime_pipeline_mismatch-1) |
| P4Runtime partial write | `p4runtime_partial_write` | `p4_dc_fabric`, `p4_dc_gateway` | `host_name` | The controller re-derives the selector-group IDs of a multi-path route and a local host route (for example after a primary failover), stores that intent, and sends one `CONTINUE_ON_ERROR` Write: both group MODIFYs, then both routes as INSERTs instead of MODIFYs. BMv2 applies the group rewrites and rejects the route INSERTs with `ALREADY_EXISTS`. Based on [Reitblatt et al., Abstractions for Network Update, SIGCOMM 2012](https://doi.org/10.1145/2342356.2342427) and the non-atomic `CONTINUE_ON_ERROR` batch semantics of the [P4Runtime specification (Write RPC, batching and ordering of updates)](https://p4lang.github.io/p4runtime/spec/v1.4.1/P4Runtime-Spec.html). | Each route sits on a group holding the other route's next hops: traffic for the multi-path route is delivered to the local host, and traffic for that host loops until its TTL expires, while untouched routes forward. The controller's write log shows the batch with two failed updates, and P4Runtime Read matches the new intent for the groups but not for the routes. | P4Runtime Read shows both groups rewritten and both routes still on their old group IDs. | [P4.org API Working Group, P4Runtime Specification 1.4.1](#source-p4runtime_partial_write-1) |
| P4 table resource exhaustion | `p4_table_resource_exhaustion` | `p4_dc_fabric`, `p4_dc_gateway` | `host_name` | Fills `ipv4_lpm` with host routes up to its declared size. A diagnostic first confirms a healthy insertion succeeds and cleans up the temporary route. | The full table rejects a new route with a resource error; existing routes continue forwarding. Duplicate-key, malformed-write, and transport errors do not establish exhaustion. | Admission and Oracle each attempt an unused route and require resource rejection with no diagnostic entry left behind. Recovery removes owned filler routes and verifies insertion works again. Independent symptom probes also check transit forwarding. | [P4.org API Working Group, P4Runtime Specification 1.4.1](#source-p4_table_resource_exhaustion-1) |
| CORP VRF DSCP mis-remarking | `vrf_dscp_remarking` | **VPN** (`enterprise_branch` s/m/l) | `host_name` (Site Edge: Branch, HQ, or DC2), `intf_name` (LAN→overlay WireGuard egress), `src_host` / `dst_host` (CORP EF path that exits that iface), `direction=lan_to_overlay`, optional `corp_prefix`. | Installs nftables mangle on the Site Edge that rewrites CORP EF (DSCP 46) to CS0 (0) for packets leaving `intf_name`. Targets are limited to egresses where the scenario's [business traffic](network-scenarios.md#business-traffic) carries EF media next to best-effort bulk (`hq_edge` `wg_br1`). Starts no traffic and adds no congestion. | The scenario's EF video leaves its reserved class and joins best effort, where the file sync already uses 60% of the ceiling: BE fair queueing drops the video above its share, so the receiving softphone logs sustained loss of about half of its packets, and the EF class on that egress stops counting it. Pings and the sync stay fast, and WG, eBGP, VRF RIB, and other paths stay up. | nft rule present and its counter shows remarked packets. | [RFC 2474, Definition of the Differentiated Services Field](#source-vrf_dscp_remarking-1) |
| WireGuard AllowedIPs omit of a remote business prefix | `wireguard_allowed_ips_misconfiguration` | **VPN** (`enterprise_branch` s/m/l) | `host_name` (Branch Edge), `intf_name` (primary HQ peer, e.g. `wg_hq`), `target_prefix` (one remote advertised CORP/SERVER prefix, e.g. `10.0.20.0/24`). Dual-homed spokes are eligible because overlay BGP stays up and primary local-pref keeps the broken path. | Healthy labs still use catch-all `AllowedIPs = 0.0.0.0/0` with `Table = off`. Inject rewrites only the Branch→Hub peer AllowedIPs to an explicit allowlist that keeps the Hub tunnel `/32` and other remote enterprise prefixes, omitting `target_prefix`, then applies it with `wg syncconf`. Does not clear BGP or edit FRR. | Handshake continues; overlay BGP stays Established; the target prefix remains in FRR BGP/RIB and Linux routing via the WG iface; Edge can still reach the Hub tunnel address; LAN traffic to the omitted prefix fails while other business prefixes, other Branches, and underlay stay healthy. | Conf AllowedIPs omits `target_prefix` and retains Hub tunnel `/32`; latest handshake > 0; BGP neighbor Established; route to the target prefix still present via the WG iface. | [WireGuard, Cryptokey Routing](#source-wireguard_allowed_ips_misconfiguration-1) |
| Wrong WireGuard peer public key | `wireguard_peer_key_misconfiguration` | **VPN** (`enterprise_branch` s/m/l; RouterOS s) | `host_name` (Branch Edge), `intf_name`, `scope` (`branch` by default, or `interface`). | Replaces the Hub peer public key on every branch tunnel, or only `intf_name` for interface scope. Keeps interfaces up and resets the affected BGP sessions. See [Latent backup-path task](#latent-backup-path-task) for the failover sources. | The affected tunnels cannot handshake. Branch scope interrupts cross-site traffic; a standby-only key error stays latent while the primary carries traffic. | The affected peers have the wrong key and no successful handshake. Ground truth labels only the rewritten interfaces. Recovery restores the Hub identities and waits for their BGP sessions. | [WireGuard, Conceptual Overview](#source-wireguard_peer_key_misconfiguration-1) |

#### Latent backup-path task

A primary WAN outage can expose an existing error on the standby tunnel. The `standby_tunnel_key_exposed` task in the [failure combination catalog](../../src/nika/problems/combinations/cases.yaml) pairs `wireguard_peer_key_misconfiguration` on `br1_edge:wg_hq_b` with `link_down` on the branch attachment to `isp1`. Its schedule injects the standby error first, then interrupts the primary path. Ground truth identifies both root causes.

Run its generated tasks with the Oracle:

```shell
uv run nika benchmark run --config benchmark/working/pool/enterprise_branch/combinations/standby_tunnel_key_exposed.yaml --agent oracle
```

The [combination catalog](#failure-combinations) records the sources, deployment choices, timing, and tested interaction. [Incident scheduling](../benchmarks/scheduling.md) describes how to author a separate timeline.

### Service Networking

| Root cause | Failure ID | Scenarios | Parameters | Injection method | Trigger and signal | Verification evidence | Sources |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Kubernetes node firewall drop | `k8s_node_firewall_drop` | **Kubernetes** | `control_node`, `node_name`, `service_name`, `namespace`, and `service_cidr` optional | Adds raw-table destination drops before kube-proxy DNAT on one node, for one ClusterIP or the whole Service CIDR. | Pods on that node cannot reach ClusterIP services while direct endpoint traffic and Kubernetes objects can remain healthy. | Raw PREROUTING and OUTPUT drops exist and the Service object remains intact. | [Kubernetes, Virtual IPs and Service Proxies](#source-k8s_node_firewall_drop-1) |
| Kubernetes Service target port mismatch | `k8s_service_target_port_mismatch` | **Kubernetes** | `control_node`, `service_name`, `namespace` (all optional) | Sets the first Ingress-backed or InferencePool endpoint-picker Service's `targetPort` to its Service `port` (the usual slip), or to the next port, through a spec patch that also updates the last-applied annotation. Recovery restores the original spec. Based on [Kubernetes, Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/). | Requests for that Service (Ingress HTTP, or the llmd_lab chat-completions call) return 5xx or time out while other application paths answer. Pods stay Ready and the Service keeps its endpoints, which now carry the wrong port. | The Service's `targetPort` equals the injected port. | [Kubernetes, Debug Services](#source-k8s_service_target_port_mismatch-1) |
| Kubernetes Service selector mismatch | `k8s_service_selector_mismatch` | **Kubernetes** | `control_node`, `service_name`, `namespace` (all optional) | Sets one label on the first Ingress-backed or InferencePool endpoint-picker Service's selector so it no longer matches the Pods, through a spec patch that also updates the last-applied annotation. Recovery restores the original spec. Based on [Kubernetes, Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/). | Requests for that Service (Ingress HTTP, or the llmd_lab chat-completions call) return 5xx or time out. The Service has no endpoints. Pods that the original selector matched stay Running and Ready. | The Service selector equals the injected labels. | [Kubernetes, Debug Services](#source-k8s_service_selector_mismatch-1) |
| Kubernetes Ingress class mismatch | `k8s_ingress_class_mismatch` | `k8s_lab` | `control_node` (optional) | Sets `ingressClassName` on the first live Ingress by namespace/name to an undefined class, and updates the last-applied annotation. Recovery restores the original spec and annotation. Based on [Kubernetes IngressClass](https://kubernetes.io/docs/concepts/services-networking/ingress/#ingress-class). | The controller returns its default 404 for the Ingress paths while the Service, Pods, and endpoints stay healthy. | The Ingress references the injected class and no IngressClass defines it. | [Kubernetes, Ingress](#source-k8s_ingress_class_mismatch-1) |
| Kubernetes Ingress rate limit misconfiguration | `k8s_ingress_rate_limit_misconfig` | `k8s_lab` (`llmd_lab` serves its traffic through AgentGateway, not ingress-nginx) | `control_node` (optional) | Sets ingress-nginx `limit-rpm` on the first Ingress carrying the declared HTTP workload to one quarter of its conservative per-path request rate, rounded down with a minimum of one request per minute. Keeps the default burst multiplier of five and updates the last-applied annotation. Recovery restores the annotation and requires the baseline burst to fully succeed. Based on [ingress-nginx rate limiting](https://kubernetes.github.io/ingress-nginx/user-guide/nginx-configuration/annotations/#rate-limiting) and [NGINX limit_req](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html). | A request burst partly succeeds and returns at least 30% HTTP 503; backend endpoints remain reachable, and controller logs report `limiting requests`. | The live and last-applied Ingress annotations contain the selected minute limit. Ground truth identifies the control-plane node. | [Ingress-Nginx Controller, Annotations](#source-k8s_ingress_rate_limit_misconfig-1) |
| Load-balancer advertisement missing | `lb_advertisement_missing` | **Kubernetes** | `control_node` (optional) | Points every MetalLB `BGPAdvertisement` and `L2Advertisement` at an address pool that does not exist, through a spec patch that also updates the last-applied annotation. Recovery restores the original pools. Based on [MetalLB configuration](https://metallb.universe.tf/configuration/). | MetalLB keeps the LoadBalancer IPs assigned but speakers withdraw them: BGP sessions, when present, stay up without the service prefixes; in L2 mode ARP for the VIP goes unanswered. Clients outside the cluster lose the address. | No advertisement names an existing IPAddressPool. | [MetalLB, Configuration](#source-lb_advertisement_missing-1) |
| Load-balancer connection-state exhaustion | `lb_connection_state_exhaustion` | `p4_dc_gateway` | `host_name=gateway_1`, `max_connections=4` | Lowers the connection limit of the gateway's L4 agent (`max_connections` in `/etc/lb-agent/config.json`, healthy value 256, the `lb_conn_table` size) below the 8 to 10 VIP connections that its client site keeps open. The injector changes only that setting; the site's own sessions fill the table, as normal load fills an undersized Linux connection-tracking table (`nf_conntrack_max`). Connections the learner cannot pin follow the pool hash, as in [SilkRoad, section 4.3](https://www.cs.yale.edu/homes/yu-minlan/writeup/sigcomm17.pdf). | New connections through that gateway are not pinned, and the LB management endpoint reports rejected admissions. Each routine pool update (every 1 to 3 minutes) moves the unpinned connections to the other backend, which resets them. Pinned connections and the other gateways keep working, and P4Runtime Read shows occupancy at or above the lowered limit, including existing connections awaiting drain. | `lb_get_state` reports the lowered limit, connection occupancy and rejected admissions. | [Miao et al., SilkRoad: Making Stateful Layer-4 Load Balancing Fast and Cheap Using Switching ASICs, SIGCOMM 2017](#source-lb_connection_state_exhaustion-1) |
| Pending load-balancer update race | `lb_pending_connection_update_race` | `p4_dc_gateway` | `host_name=gateway_1`, `learning_delay_ms=8000`, `seed` | Slows the gateway connection learner from its healthy 5 ms to `learning_delay_ms`, so new connections stay pending (not yet pinned in `lb_conn_table`) for that long. The gateway's routine pool update (a VIP version flip every 1 to 3 minutes in every case, no TransitTable pin) then races them, as in [SilkRoad, section 4.3](https://www.cs.yale.edu/homes/yu-minlan/writeup/sigcomm17.pdf). | Connections opened within `learning_delay_ms` before a pool update move to the new DIP and are reset; connections learned earlier keep their DIP. The business keep-alive sessions see resets and retry, and the LB management endpoint records each update and its pending connection count. | `lb_get_state` reports the learning delay, active and pending connections, and pool update events. | [Miao et al., SilkRoad: Making Stateful Layer-4 Load Balancing Fast and Cheap Using Switching ASICs, SIGCOMM 2017](#source-lb_pending_connection_update_race-1) |
| SNAT port-pool exhaustion | `snat_port_pool_exhaustion` | `enterprise_branch` | `host_name=br1_edge`, `source_prefix=10.1.40.0/24`, `public_ip=198.18.1.10`, `port_start=40000`, `port_end=40063` | Installs an nftables SNAT rule that limits BR1 GUEST TCP flows to 64 source ports on a routed public alias. | Existing allocations remain usable. As the guest sessions of the scenario's business traffic reconnect into the pool, page loads exceed it: new connections to the public service intermittently fail or wait for SYN retries, and conntrack counts insert failures. | `nft list table ip nat` contains the configured SNAT port range. | [Netfilter, NAT HOWTO](#source-snat_port_pool_exhaustion-1) |
| NAT mapping removed before drain | `nat_mapping_removed_without_drain` | `enterprise_branch` | `host_name=br1_edge`, `source_prefix=10.1.40.0/24`, `nat_ip_a=198.18.1.10`, `nat_ip_b=198.18.1.11`, `wan_interface=eth2` | Directs new GUEST TCP mappings to NAT_IP_B, removes the routed NAT_IP_A alias, and deletes NAT_IP_A conntrack mappings. This models the address-drain requirement in [Cloud NAT troubleshooting](https://cloud.google.com/nat/docs/troubleshooting). | The guest sessions of the scenario's business traffic that are live at inject break: their packets now leave from NAT_IP_B and the replies to NAT_IP_A have no state. Clients time out and reconnect through NAT_IP_B, while the public service keeps retransmitting to NAT_IP_A. | NAT_IP_A is absent from `eth2`, NAT_IP_B remains assigned, and the moved source prefix has no TCP mapping using NAT_IP_A. The native provider checks that no mapping uses NAT_IP_A. | [Netfilter, conntrack-tools user manual](#source-nat_mapping_removed_without_drain-1) |

### Management & Orchestration Plane

| Root cause | Failure ID | Scenarios | Parameters | Injection method | Trigger and signal | Verification evidence | Sources |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Kubernetes API-server partition | `k8s_worker_apiserver_partition` | **Kubernetes** | `control_node` optional, `node_name` optional, `apiserver_port=6443`, `apiserver_address` optional | Adds raw-table drops on one worker for TCP traffic to the k3s API server. | After the node-monitor grace period, the worker becomes NotReady and `kubectl logs` or `exec` fails. Its pods keep running on their pod IPs but leave Service endpoints, so services backed only by that worker fail. ICMP reachability survives. | Drop rules exist and the Kubernetes Node becomes NotReady; verification allows up to 240 seconds. | [Kubernetes, Nodes](#source-k8s_worker_apiserver_partition-1) |
| SDN controller crash | `sdn_controller_crash` | **SDN** | `host_name` | Kills the ONOS Karaf and JVM processes. | Existing switch flows can survive until they expire; new flows cannot be programmed. | No ONOS, Karaf, or Java controller process is running. | [Open vSwitch, ovs-vswitchd.conf.db(5)](#source-sdn_controller_crash-1) |
| Southbound port unreachable | `southbound_port_block` | **SDN** | `host_name`, `southbound_port=6653` | Adds an nftables drop for the controller's OpenFlow port in the `inet filter` input, forward, and output chains. ONOS keeps listening on the port. | Switch connection attempts to the controller time out while the controller process and its listener remain present. | The exact `tcp dport <port> drop` rule is in every hooked chain and the controller still listens on the port. | [Netfilter, nft(8)](#source-southbound_port_block-1) |
| Southbound port mismatch | `southbound_port_mismatch` | **SDN** | `host_name`, `mismatched_port=6633`, `original_port=6653` | Sets the ONOS OpenFlow listener (`openflowPorts`) to `mismatched_port` only. Switches keep targeting `original_port`. NIKA adds no firewall rule. | Switches get connection refused on `original_port` and drop their controller sessions while ONOS runs and listens on `mismatched_port`. | ONOS `openflowPorts` excludes `original_port`, nothing listens on `original_port`, the controller listens on `mismatched_port`, and every OVS bridge targets `original_port`. Disconnection is a separate symptom check. | [ONOS 2.7.0, OpenFlowControllerImpl.java](#source-southbound_port_mismatch-1) |
| OpenFlow datapath ID conflict | `switch_datapath_id_conflict` | **SDN** | `host_name` (a switch) | Sets the switch's `other-config:datapath-id` to that of the first other switch in the fabric, as a cloned switch configuration would. Recovery restores the original ID. Based on the `datapath-id` column of [ovs-vswitchd.conf.db(5)](https://www.openvswitch.org/support/dist-docs/ovs-vswitchd.conf.db.5.html). | ONOS keeps the switch that connected first under that ID and closes each new channel from the target as a duplicate, so the target reconnects every few seconds and stays unavailable in ONOS; its installed flows keep forwarding. ONOS logs `Duplicated dpid`. | The switch's datapath ID equals the copied one. | [ONOS 2.7.0, OFChannelHandler.java](#source-switch_datapath_id_conflict-1) |
| CoreDNS forwarding loop | `k8s_coredns_forward_loop` | **Kubernetes** | `control_node` (optional) | Sets `forward . 127.0.0.1` in ConfigMap `kube-system/coredns` and recreates the CoreDNS Pods. Recovery restores the original Corefile and waits for Ready CoreDNS, DNS answers, and successful in-cluster application checks. Based on [CoreDNS loop detection](https://coredns.io/plugins/loop/#troubleshooting-loops-in-kubernetes-clusters) and [Kubernetes DNS debugging](https://kubernetes.io/docs/tasks/administer-cluster/dns-debugging-resolution/). | CoreDNS enters CrashLoopBackOff and logs `Loop ... detected`. Existing monitoring requests through Service names fail while nodes and application Pods remain Ready. | The Corefile contains the injected root-zone forward directive and the CoreDNS Pods are in CrashLoopBackOff. | [CoreDNS, loop plugin](#source-k8s_coredns_forward_loop-1) |
| Kubernetes readiness probe misconfiguration | `k8s_readiness_probe_misconfig` | **Kubernetes** | `control_node`, `deployment_name`, `namespace` (all optional) | Selects a Deployment on an Ingress or InferencePool path and changes or adds its readiness probe to use an unserved port. Refuses Deployments backing several observable Services. Recovery restores the original spec and waits up to 15 minutes for Ready Pods and endpoints. Based on [Kubernetes, Configure Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/) and [Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/). | Pods stay Running but not Ready, EndpointSlice conditions report `ready: false`, kubelet events report readiness probe failures, and application traffic fails. | The Deployment template contains the changed readiness probe. | [Kubernetes, Configure Liveness, Readiness and Startup Probes](#source-k8s_readiness_probe_misconfig-1) |
| Kubernetes Deployment replicas zero | `k8s_deployment_replicas_zero` | **Kubernetes** | `control_node`, `deployment_name`, `namespace` (all optional) | When `deployment_name` is set, resolves that Deployment, the Service that selects its Pods, and the Ingress or InferencePool path to that Service, and refuses targets with no user-visible path. Otherwise scales the Deployment behind the first Ingress backend or InferencePool endpoint-picker Service. Sets `spec.replicas` to 0 through a spec patch that also updates the last-applied annotation. Recovery restores the original spec and replica count. Based on [Kubernetes, Scaling a Deployment](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#scaling-a-deployment). | The Service has no endpoints and requests for that application fail (Ingress HTTP, or the llmd_lab chat-completions call). Other Deployments keep serving. | The Deployment's `spec.replicas` is 0. | [Kubernetes, Deployments](#source-k8s_deployment_replicas_zero-1) |
| Kubernetes node NoExecute taint | `k8s_node_noexecute_taint` | `k8s_lab` | `control_node` optional, `node_name` optional, `taint_key=maintenance`, `taint_value=true` | Adds a NoExecute taint on a node whose local PersistentVolume backs a business database. Pods that do not tolerate it are evicted; the volume's node affinity keeps the replacement Pending. Recovery removes the taint. Based on [Kubernetes taints and tolerations](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/). | The application that uses that volume fails. A sibling application on another node still answers. The Node stays Ready. | The Node has the injected taint. | [Kubernetes, Taints and Tolerations](#source-k8s_node_noexecute_taint-1) |

### Addressing, Neighbor & Naming

| Root cause | Failure ID | Scenarios | Parameters | Injection method | Trigger and signal | Verification evidence | Sources |
| --- | --- | --- | --- | --- | --- | --- | --- |
| MAC address conflict | `mac_address_conflict` | **Host L2** except `llmd_lab` | `host_name`, `host_name_2` | Copies `host_name_2`'s `eth0` MAC address to `host_name`. | On a shared learning bridge, traffic from both hosts can move the shared MAC between ports. Separate routed links need not show MAC flapping. | Both interfaces report the same MAC address. | [Cisco, Troubleshoot MAC Flaps/Loop on Cisco Catalyst Switches](#source-mac_address_conflict-1) |
| DHCP missing subnet | `dhcp_missing_subnet` | **DHCP** | `host_name`, `host_name_2`, `subnet` optional | Removes the client's subnet declaration from the legitimate server's `dhcpd.conf`, restarts `isc-dhcp-server`, and renews DHCP client leases. | Clients in that subnet cannot obtain a lease while other subnets continue. | The subnet declaration is absent from `dhcpd.conf`. | [ISC DHCP 4.4, dhcpd.conf](#source-dhcp_missing_subnet-1) |
| DHCP service stopped | `dhcp_service_down` | **DHCP** | `host_name`, `service_name=isc-dhcp-server` | Stops the configured service, kills remaining `dhcpd` processes, and forces DHCP clients to renew. The host remains running. | New leases and forced renewals fail. Without forced renewal, an existing lease can hide server unavailability until renewal. | `dhcpd` is absent from the process table. | [RFC 2131, Dynamic Host Configuration Protocol](#source-dhcp_service_down-1) |
| Incorrect authoritative DNS record | `dns_record_error` | **DNS** | `host_name`, `target_website`, `target_domain`, `wrong_ip` optional | Replaces the target A record in the BIND zone and reloads the service. | Queries for one name return the wrong address while other DNS records can remain correct. | The zone contains the wrong IP and the running DNS server returns it. | [RFC 1035, Domain Names: Implementation and Specification](#source-dns_record_error-1) |
| DNS service stopped | `dns_service_down` | **DNS** | `host_name`, `service_name=named` | Kills the BIND `named` process while leaving the host running. | DNS-dependent web access fails while direct IP traffic can remain healthy. | `named` is absent from the process table. | [BIND 9, named manual](#source-dns_service_down-1) |
| Incorrect resolver configuration | `host_incorrect_dns` | **DNS** | `host_name`, `fake_dns_ip` optional | Replaces the nameserver entries in `/etc/resolv.conf`, preserving search and options. When omitted, the wrong resolver is an unused address near the original resolver or gateway. Removes matching static web-name overrides and, on DHCP hosts, installs a renewal hook for the wrong resolver. | Name-based requests fail or resolve through the wrong server while direct IP requests still work. | `/etc/resolv.conf` contains the fake resolver. | [Linux man-pages, resolv.conf(5)](#source-host_incorrect_dns-1) |
| Host IP misconfig | `host_incorrect_gateway` | **Routed host** | `host_name`, `new_gateway` optional | Adds an invalid or caller-selected default gateway after reapplying the host address. | Same-subnet traffic can work while off-subnet traffic fails. | The default route output contains the injected gateway; deep probes use a cross-subnet destination. | [RFC 1122, Requirements for Internet Hosts: Communication Layers](#source-host_incorrect_gateway-1) |
| Host IP misconfig | `host_incorrect_ip` | **Host L2** | `host_name`, `incorrect_ip` optional | Replaces the target's `eth0` address; NIKA derives a wrong address when omitted. | The host leaves its intended subnet or conflicts with routing assumptions; peers can no longer reach the previous address. | The current address differs from the captured original (or equals the supplied wrong address when no original was captured). Test-path probes check reachability separately. | [iproute2, ip-address(8)](#source-host_incorrect_ip-1) |
| Incorrect netmask | `host_incorrect_netmask` | **Routed host** | `host_name`, `netmask_prefix=8` | Replaces the `eth0` prefix length. | The host treats remote destinations as on-link or routes local destinations through its gateway. | `eth0` has exactly `netmask_prefix`. On-link versus gateway routing depends on that prefix and the probe destination. | [RFC 1122, Requirements for Internet Hosts: Communication Layers](#source-host_incorrect_netmask-1) |
| Host IP conflict | `host_ip_conflict` | **Host L2** | `host_name`, `host_name_2` | Replaces `host_name_2`'s IPv4 address and default gateway with those of `host_name`; the first host is unchanged. | The second host loses its previous address. On a shared L2 segment, traffic to the shared address can follow either host as ARP ownership changes. | Both `eth0` interfaces report the same IPv4 address. | [RFC 5227, IPv4 Address Conflict Detection](#source-host_ip_conflict-1) |
| Host IP misconfig | `host_missing_ip` | **Host L2** | `host_name`, `intf_name=eth0` | Deletes the selected IPv4 address from the interface. The no-address verification assumes it was the interface's only global IPv4 address. | Local and remote IP traffic fails while the link can remain up. | The interface has no global IPv4 address. | [iproute2, ip-address(8)](#source-host_missing_ip-1) |
| DHCP DNS option misconfiguration | `dhcp_option_dns` | **DHCP** | `host_name`, `host_name_2`, `wrong_dns` optional (derived unused address) | Changes the DNS option on the legitimate DHCP server and renews leases. | Renewed clients send queries to the wrong resolver while direct IP traffic can work. | `dhcpd.conf` contains the incorrect DNS option. | [RFC 2132, DHCP Options and BOOTP Vendor Extensions](#source-dhcp_option_dns-1) |
| DHCP gateway option misconfiguration | `dhcp_option_gateway` | **DHCP** | `host_name`, `host_name_2` | Changes the legitimate DHCP server's target subnet router option to an address ending in `.254`, then renews leases. | Renewed clients lose off-subnet traffic while local LAN traffic can work. | `dhcpd.conf` contains the incorrect router option. | [RFC 2132, DHCP Options and BOOTP Vendor Extensions](#source-dhcp_option_gateway-1) |
| DHCP subnet option misconfiguration | `dhcp_option_subnet` | **DHCP** | `host_name`, `host_name_2`, `subnet` optional, `wrong_netmask=255.255.255.252` | Adds an incorrect `option subnet-mask` to the client's subnet declaration on the legitimate server and renews leases. The declaration stays. | Renewed clients get an address with a prefix that excludes their gateway, so off-subnet traffic fails. | The subnet declaration exists once and its `option subnet-mask` equals `wrong_netmask`. | [RFC 2132, DHCP Options and BOOTP Vendor Extensions](#source-dhcp_option_subnet-1) |

### Endpoint & Application

For these HTTP failures, `sender` is the server that sends the response body and `receiver` is the client that downloads it.

| Root cause | Failure ID | Scenarios | Parameters | Injection method | Trigger and signal | Verification evidence | Sources |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Endpoint CPU contention | `endpoint_cpu_contention` | **HTTP** (canonical: `dc_clos`) | `host_name` (contended endpoint), `client_host`, optional `server_host`, transfer URLs, `cpu_quota=0.05`, `stress_cpus=16` | Applies a CPU quota and starts competing CPU-bound batch workers on the selected endpoint. | A starved sender serves bulk HTTP slowly while small requests succeed; a starved receiver drains TCP slowly and limits the peer through its receive window. Peer probes check network reachability. The endpoint resource identifies the sender or receiver. | Batch workers run and the CPU quota matches. Recovery restores the quota and stops the owned workers. | [Dan Luu and David Mackey, The container throttling problem (Twitter operational analysis)](#source-endpoint_cpu_contention-1); [Linux kernel, CFS Bandwidth Control](#source-endpoint_cpu_contention-2) |

### Traffic, Queueing & Resource

| Root cause | Failure ID | Scenarios | Parameters | Injection method | Trigger and signal | Verification evidence | Sources |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Undersized overlay EF class | `overlay_qos_ef_undersized` | `enterprise_branch` (FRR sizes `s`/`m`/`l`; RouterOS size `s`) | `host_name`, `intf_name`, `src_host`, `dst_host` (workload-loaded HQ video egress) | Lowers the EF guarantee and ceiling to one tenth of the EF reservation, so EF cannot borrow unused capacity. RouterOS uses the environment's CHR-scaled rates. Classification and DSCP remain correct. Sources: Cisco [interface-drop troubleshooting](https://www.cisco.com/c/en/us/support/docs/routers/asr-1000-series-ios-xe-sd-wan/224129-troubleshoot-interface-packet-drops-in.html) and [priority-queue drop troubleshooting](https://www.cisco.com/c/en/us/support/docs/asynchronous-transfer-mode-atm/atm-traffic-management/10105-priorityqueuedrops.html) (vendor troubleshooting documents), and [voice bandwidth-exceed drops](https://community.cisco.com/t5/routing/b-w-exceed-drops-for-voice-traffic/td-p/2156289) (operator discussion). HTB and queue trees reproduce bandwidth exhaustion with shaping and queue drops; Cisco LLQ uses a policer. | The declared HQ-to-br1 EF video loses packets while the EF class continues carrying traffic; best effort and pings remain healthy. | The live EF rate and ceiling both equal one tenth of the healthy reservation (minimum 1 kbit/s). Queue drops and softphone loss/jitter are separate symptom observations. Recovery restores the reservation and requires healthy media, best effort, and pings. Ground truth identifies the edge whose policy changed. | [RFC 3246, An Expedited Forwarding PHB](#source-overlay_qos_ef_undersized-1) |
| ECN marking disabled | `p4_ecn_marking_disabled` | `p4_dc_gateway` | `host_name`, `intf_name`, `bmv2_port`, `threshold=1024` | Sets the ECN threshold above the BMv2 queue-depth cap of 64 packets. | The affected egress never marks packets CE under congestion. | P4Runtime shows a threshold above the queue-depth cap; the Oracle compares it with other ports. | [Zhu et al., Packet-Level Telemetry in Large Datacenter Networks (Everflow), SIGCOMM 2015](#source-p4_ecn_marking_disabled-1) |
| Receiver TCP window limited | `tcp_receive_window_limited` | **HTTP** (`enterprise_branch` only for now) | `host_name` (HTTP client), optional `sender_host`/`sender_ip`/`large_url`, BDP calibration knobs | Disables `tcp_moderate_rcvbuf` and constrains receiver `tcp_rmem` (and `rmem_max` when writable) to a value well below measured path BDP (Dapper receiver-limited). Does not add delay, loss, or rate limits. | Bulk HTTP connections opened under the fault become slower, including new file-sync transfers. Ping, TCP connect, and small objects stay healthy; RTT and loss stay near baseline. Targets are limited to hosts that business traffic downloads into. | Sysctls match the injected `tcp_moderate_rcvbuf` / `tcp_rmem` values; interface remains up. Behavioral checks measure sender time limited by the receiver window and require a healthy control path. Recovery restores saved settings and reconnects post-injection sockets that retain the injected buffer cap, preserving pre-injection connections. | [Linux kernel, IP Sysctl](#source-tcp_receive_window_limited-1) |

### Security

| Root cause | Failure ID | Scenarios | Parameters | Injection method | Trigger and signal | Verification evidence | Sources |
| --- | --- | --- | --- | --- | --- | --- | --- |
| ARP cache poisoning | `arp_cache_poisoning` | **Host L2** | `host_name`, `fake_mac=00:11:22:33:44:55` | Clears existing neighbor entries (including `nud permanent` gateway mappings), installs a permanent neighbor that maps the default gateway to the fake MAC, and installs a netdev egress nftables drop for that forged MAC (required when P4 leaves ignore Ethernet dst on point-to-point host links; inet output cannot match L2 on locally generated packets). The extra drop is used only when the original neighbor was permanent and the environment's routers ignore Ethernet destination. Otherwise the wrong neighbor mapping acts alone; `sdn_l3_clos` leaves enforce destination MAC. | Off-subnet traffic from the target is sent toward the wrong L2 destination and does not leave the host usefully. | The gateway neighbor entry shows the fake MAC. | [RFC 5227, IPv4 Address Conflict Detection](#source-arp_cache_poisoning-1) |
| BGP hijacking | `bgp_hijacking` | **BGP** | `host_name`, `target_network` optional, `variant=prefix` (`prefix` or `rpki_invalid_origin`) | Makes an FRR, IOS XR, RouterOS, or SONiC router originate an unauthorized prefix and answer for it from a loopback. It runs on Kathara and on Containerlab SONiC and SR Linux routers with eligible BGP targets. On `enterprise_branch`, `hq_edge` originates another branch's CORP /24: hubs accept nothing longer than /24 from the overlay, and a branch's own /24 keeps winning at the hubs as the older path. On ISP labs with BGP, a router originates another router's business prefix inside its AS. Rows keep hijacks that win at a router whose stub holds sessions to that prefix's service. The `rpki_invalid_origin` variant on Kathara eBGP ISP labs exports an origin not authorized by the covering ROA. | BGP peers select the injected route when policy and prefix selection permit it. On `enterprise_branch`, HQ and every other branch send the victim branch's CORP traffic to `hq_edge`. On Kathara ISP labs, the observer's service sessions end at the hijacker and are refused; on Containerlab ISP labs, the observer stub's traffic to the prefix ends at the hijacker. ROV peers reject the invalid-origin variant while non-ROV peers learn it. | The router advertises the prefix; FRR also has the loopback address. | [FRRouting, BGP Networks](#source-bgp_hijacking-1); [RFC 6811, BGP Prefix Origin Validation](#source-bgp_hijacking-2) |
| TCP SYN flood | `tcp_syn_flood_attack` | `p4_dc_gateway` | `attacker_device`, `target_ip`, `target_port=80`, `rate_pps=1000`, `duration` (accepted, no timer), `flows=100`, `seed` | Starts deterministic SYN-only 5-tuples from one seeded external client without changing the Bloom-filter threshold. Based on [Tracking Network Flows with P4, Sections VII–VIII, paper pages 28–30](https://pure.uva.nl/ws/files/42163153/Tracking_Network_Flows_with_P4.pdf). | At the default settings, the attacker is configured to send about 1000 SYNs per second across 100 five-tuples for the whole trial; SYN/non-SYN imbalance and half-open pressure increase while legitimate target connections degrade. | At least one configured SYN-generator process runs, and the INT collector is alive with a fresh report file. Bloom-filter imbalance and independent-client health require separate symptom checks. | [RFC 4987, TCP SYN Flooding Attacks and Common Mitigations](#source-tcp_syn_flood_attack-1) |
| Service DoS | `web_dos_attack` | **HTTP** | `host_name`, `attacker_device`, optional `observer_device`/`probe_url`/`attack_url`; load and probe controls | Starts parallel ApacheBench download workers and a slow-header connection pool against the same service endpoint. The injector records an HTTP baseline from an independent observer and waits for worker, process, and target-connection evidence. | HTTP error rate, median latency, or p95 latency can rise from the healthy baseline while attack connections consume server capacity. | The configured worker loops, at least one `ab` process, and a slow-header client run. The target connection count meets `max(2, min(20, slow_connections / 2))` with integer division. HTTP degradation is checked separately against baseline; recovery stops failure-owned processes. | [cPanel, How to Mitigate Slowloris Attacks](#source-web_dos_attack-1) |

## Failure combinations

A multi-fault task injects two or more failure placements in one network. A task may place the same failure on two resources. Repeated placements use `failure[1]` and `failure[2]` keys in the injection map; the failure IDs stay unchanged.

`bgp_max_prefix_limit_low`, `bgp_prefix_surge`, and `bgp_rov_not_enforced` are combination members. Generation and selection reject single-fault tasks for these components. Multi-fault tasks form one category in reports, whatever their failures are.

A benchmark release contains only the tested multi-fault tasks in [`cases.yaml`](../../src/nika/problems/combinations/cases.yaml). Each task fixes its scenario, deployment, and every failure's parameters, and its failures have a public source for occurring together, recorded with the task and listed below. Every task passed the live test in [`test_failure_combinations_e2e.py`](../../tests/nika/problems/test_failure_combinations_e2e.py): each failure alone, both together, recovery of each, and an [Oracle](../benchmarks/oracle-solver.md) run that confirms every failure. `nika benchmark generate` writes these tasks to `pool/<scenario>/combinations/<name>.yaml`, and freezing a release rejects any other multi-fault task. Where the order matters, a task injects the first failure at `0s`, the others at `trigger_at`, and starts diagnosis after `settle`, once the network has converged.

Benchmark files outside a release may combine any registered failures. Before deploying such a task, NIKA checks that each failure is compatible with the deployment and that its parameters fit its schema and the topology, so the injections run. It does not check that the failures produce a joint symptom; test that separately.

The tasks of `policy_gap_exposed_by_uplink_loss` use size `s` of `dc_clos`. RouterOS removes policy on the leaf; IOS XR removes it on the spine side of the same session because its bridged leaves run FRR. Healthy IOS XR sessions use explicit pass policies with `bgp unsafe-ebgp-policy` disabled.

`pmtud_black_hole` requires small packets to remain reachable after routing settles. The SONiC `isp_abilene` IS-IS placement at `atlam5:eth1` is excluded because the reduced MTU breaks its sole adjacency and the peer's return route, so small packets also fail. Its padded IS-IS hellos exceed the reduced MTU; restoring the MTU restores the adjacency and return route.

<!-- combination-sources:start -->
| Combination | Failures | Tested deployments | Description | Sources |
| --- | --- | --- | --- | --- |
| `bgp_transport_masks_asn` | `bgp_asn_misconfig`, `bgp_acl_block` | `dc_clos` (default); `dc_clos` (routeros); `isp_dfn-bwin` (nokia_srlinux) | A router's BGP TCP filter keeps its sessions from reaching the OPEN exchange that would reveal its misconfigured local AS. | [RFC 4271, A Border Gateway Protocol 4 (BGP-4)](#combination-source-bgp_transport_masks_asn-1); [RFC 4271, A Border Gateway Protocol 4 (BGP-4)](#combination-source-bgp_transport_masks_asn-2) |
| `bottleneck_masks_receive_window` | `tcp_receive_window_limited`, `link_capacity_bottleneck` | `enterprise_branch` (default) | A bottleneck on the sender's link caps a transfer below the rate the receiver's small TCP window allows. | [RFC 6349, Framework for TCP Throughput Testing](#combination-source-bottleneck_masks_receive_window-1); [RFC 6349, Framework for TCP Throughput Testing](#combination-source-bottleneck_masks_receive_window-2) |
| `ingress_class_masks_target_port` | `k8s_service_target_port_mismatch`, `k8s_ingress_class_mismatch` | `k8s_lab` (default) | An Ingress claimed by no controller answers 404 before its Service's wrong target port can show as a gateway error. | [Kubernetes, Ingress](#combination-source-ingress_class_masks_target_port-1); [Kubernetes, Debug Services](#combination-source-ingress_class_masks_target_port-2) |
| `latent_adjacency_cut` | `ospf_hello_interval_mismatch`, `link_down` | `campus_lan` (default); `campus_lan` (iosxr); `campus_lan` (routeros) | An OSPF adjacency lost to a hello timer mismatch stays hidden until the other link of the same cut fails. | [Why Does the Cloud Stop Computing? Lessons from Hundreds of Service Outages (SoCC 2016)](#combination-source-latent_adjacency_cut-1); [Evolve or Die: High-Availability Design Principles Drawn from Google's Network Infrastructure (SIGCOMM 2016)](#combination-source-latent_adjacency_cut-2) |
| `latent_network_type_cut` | `ospf_network_type_mismatch`, `link_down` | `campus_lan` (default); `campus_lan` (routeros) | An OSPF link whose network types disagree drops out of SPF and stays hidden until the other link of the same cut fails. | [Why Does the Cloud Stop Computing? Lessons from Hundreds of Service Outages (SoCC 2016)](#combination-source-latent_network_type_cut-1); [Cisco, Troubleshoot Open Shortest Path First Route Database Issues](#combination-source-latent_network_type_cut-2) |
| `pmtud_black_hole` | `mtu_mismatch`, `icmp_frag_needed_filter_misconfiguration` | `dc_clos` (default); `dc_clos` (routeros) | A router's lowered MTU on a routed link and its filter on ICMP Fragmentation Needed together silently drop large DF packets. | [RFC 2923, TCP Problems with Path MTU Discovery](#combination-source-pmtud_black_hole-1); [Path MTU discovery in practice (Cloudflare)](#combination-source-pmtud_black_hole-2) |
| `policy_gap_exposed_by_uplink_loss` | `bgp_session_policy_missing`, `link_down` | `dc_clos` (iosxr); `dc_clos` (routeros) | A leaf's eBGP session without usable policy stays hidden until its other uplink fails. | [RFC 8212, Default External BGP Route Propagation Behavior without Policies](#combination-source-policy_gap_exposed_by_uplink_loss-1); [Evolve or Die: High-Availability Design Principles Drawn from Google's Network Infrastructure (SIGCOMM 2016)](#combination-source-policy_gap_exposed_by_uplink_loss-2); [Cisco IOS XR, Implementing BGP](#combination-source-policy_gap_exposed_by_uplink_loss-3) |
| `prefix_surge_trips_low_limit` | `bgp_max_prefix_limit_low`, `bgp_prefix_surge` | `isp_abilene` (frr) | A peer's prefix surge exceeds a session's latent low maximum-prefix limit and disconnects its customer route. | [Australian Senate, Chapter 2: The Optus network outage](#combination-source-prefix_surge_trips_low_limit-1); [RFC 7454, BGP Operations and Security](#combination-source-prefix_surge_trips_low_limit-2) |
| `redundant_links_both_lost` | `link_down`, `link_down` | `campus_lan` (default); `campus_lan` (iosxr); `campus_lan` (routeros); `isp_abilene` (sonic); `isp_dfn-gwin` (nokia_srlinux) | Two administratively disabled links that back each other up are lost together, partitioning the nodes behind them. | [Evolve or Die: High-Availability Design Principles Drawn from Google's Network Infrastructure (SIGCOMM 2016)](#combination-source-redundant_links_both_lost-1) |
| `rov_gap_admits_hijack` | `bgp_rov_not_enforced`, `bgp_hijacking` | `isp_abilene` (frr) | A router whose import policy no longer rejects RPKI-invalid routes installs routes a peer originates with an unauthorized origin. | [How Verizon and a BGP Optimizer Knocked Large Parts of the Internet Offline Today (Cloudflare)](#combination-source-rov_gap_admits_hijack-1); [RFC 6811, BGP Prefix Origin Validation](#combination-source-rov_gap_admits_hijack-2) |
| `selector_masks_readiness` | `k8s_readiness_probe_misconfig`, `k8s_service_selector_mismatch` | `k8s_lab` (default) | A Service whose selector matches no Pods hides that the Pods behind it fail their readiness probes. | [Kubernetes, Debug Services](#combination-source-selector_masks_readiness-1); [Kubernetes, Pod Lifecycle](#combination-source-selector_masks_readiness-2) |
| `standby_tunnel_allowed_ips_exposed` | `wireguard_allowed_ips_misconfiguration`, `link_down` | `enterprise_branch` (default); `enterprise_branch` (iosxr); `enterprise_branch` (routeros) | A standby tunnel's AllowedIPs omit a remote prefix, which stays hidden until the primary provider link fails. | [Why Does the Cloud Stop Computing? Lessons from Hundreds of Service Outages (SoCC 2016)](#combination-source-standby_tunnel_allowed_ips_exposed-1); [WireGuard, Cryptokey Routing](#combination-source-standby_tunnel_allowed_ips_exposed-2) |
| `standby_tunnel_key_exposed` | `wireguard_peer_key_misconfiguration`, `link_down` | `enterprise_branch` (default); `enterprise_branch` (iosxr); `enterprise_branch` (routeros) | A standby overlay tunnel's wrong peer key stays hidden until the primary provider link fails. | [Why Does the Cloud Stop Computing? Lessons from Hundreds of Service Outages (SoCC 2016)](#combination-source-standby_tunnel_key_exposed-1) |
| `undersized_backup_exposed` | `link_capacity_bottleneck`, `link_down` | `enterprise_branch` (default); `enterprise_branch` (iosxr) | A branch's standby provider link is provisioned far below the branch's traffic, which shows only after the primary link fails. | [Summary of the Amazon EC2 and Amazon RDS Service Disruption in the US East Region (AWS, April 2011)](#combination-source-undersized_backup_exposed-1); [Summary of the Amazon EC2 and Amazon RDS Service Disruption in the US East Region (AWS, April 2011)](#combination-source-undersized_backup_exposed-2) |

#### `bgp_transport_masks_asn`

<a id="combination-source-bgp_transport_masks_asn-1"></a>
<details>
<summary>RFC 4271, A Border Gateway Protocol 4 (BGP-4) (rfc; interaction)</summary>

Source: [RFC 4271, A Border Gateway Protocol 4 (BGP-4)](https://www.rfc-editor.org/rfc/rfc4271.html#section-8.2.2). Location: Section 8.2.2, Connect state. Accessed: 2026-11-12.

> If the TCP connection succeeds (Event 16 or Event 17), the local system checks the DelayOpen attribute prior to processing.

**Correspondence:** A speaker sends OPEN only after its TCP connection succeeds, so a filter on TCP port 179 keeps every session of the router before the OPEN exchange.

**Evidence limits:** The RFC describes the state machine, not a filter combined with an AS error.

</details>

<a id="combination-source-bgp_transport_masks_asn-2"></a>
<details>
<summary>RFC 4271, A Border Gateway Protocol 4 (BGP-4) (rfc; interaction)</summary>

Source: [RFC 4271, A Border Gateway Protocol 4 (BGP-4)](https://www.rfc-editor.org/rfc/rfc4271.html#section-6.2). Location: Section 6.2, OPEN Message Error Handling. Accessed: 2026-11-12.

> If the Autonomous System field of the OPEN message is unacceptable, then the Error Subcode MUST be set to Bad Peer AS.

**Correspondence:** The misconfigured local AS is reported only as an OPEN error, which the transport filter prevents.

**Evidence limits:** Same document as the state machine excerpt.

</details>

#### `bottleneck_masks_receive_window`

<a id="combination-source-bottleneck_masks_receive_window-1"></a>
<details>
<summary>RFC 6349, Framework for TCP Throughput Testing (rfc; interaction)</summary>

Source: [RFC 6349, Framework for TCP Throughput Testing](https://www.rfc-editor.org/rfc/rfc6349.html#section-3.3.1). Location: Section 3.3.1, Minimum TCP RWND. Accessed: 2026-11-12.

> If a smaller TCP RWND size is used, then the TCP Throughput cannot be optimal.

**Correspondence:** The tighter of the two limits sets throughput: the shaper on the sender's link keeps the transfer below what the receiver's window allows, so the window never fills until the shaper is removed.

**Evidence limits:** RFC 6349 is a test methodology; it does not report incidents with both conditions.

</details>

<a id="combination-source-bottleneck_masks_receive_window-2"></a>
<details>
<summary>RFC 6349, Framework for TCP Throughput Testing (rfc; interaction)</summary>

Source: [RFC 6349, Framework for TCP Throughput Testing](https://www.rfc-editor.org/rfc/rfc6349.html#section-3.2.2). Location: Section 3.2.2, Measuring the bottleneck bandwidth. Accessed: 2026-11-12.

> Before any TCP Throughput test can be conducted, bandwidth measurement tests SHOULD be run with stateless IP streams.

**Correspondence:** The method measures the bottleneck separately from TCP, which separates the two limits.

**Evidence limits:** Same methodology document as the window guidance.

</details>

#### `ingress_class_masks_target_port`

<a id="combination-source-ingress_class_masks_target_port-1"></a>
<details>
<summary>Kubernetes, Ingress (vendor_documentation; interaction)</summary>

Source: [Kubernetes, Ingress](https://kubernetes.io/docs/concepts/services-networking/ingress/). Location: Prerequisites. Accessed: 2026-11-12.

> You must have an Ingress controller to satisfy an Ingress. Only creating an Ingress resource has no effect.

**Correspondence:** An Ingress whose class no controller handles is not served, so clients never reach the Service whose target port is wrong.

**Evidence limits:** The page describes the missing controller, not an Ingress class that names no installed controller; both leave the Ingress unserved.

</details>

<a id="combination-source-ingress_class_masks_target_port-2"></a>
<details>
<summary>Kubernetes, Debug Services (vendor_documentation; co_occurrence)</summary>

Source: [Kubernetes, Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/). Location: Is the Service defined correctly?. Accessed: 2026-11-12.

> Is the `targetPort` correct for your Pods (some Pods use a different port than the Service)?

**Correspondence:** A wrong target port is a separate check from the Ingress path in the same debugging procedure.

**Evidence limits:** Checklist guidance, not an incident report.

</details>

#### `latent_adjacency_cut`

<a id="combination-source-latent_adjacency_cut-1"></a>
<details>
<summary>Why Does the Cloud Stop Computing? Lessons from Hundreds of Service Outages (SoCC 2016) (paper; co_occurrence)</summary>

Source: [Why Does the Cloud Stop Computing? Lessons from Hundreds of Service Outages (SoCC 2016)](https://www.ece.iastate.edu/~mai/docs/papers/2016_SoCC_COS.pdf). Location: Section 4.3, Backups that also Fail. Accessed: 2026-11-12.

> simultaneous multiple failures of primary and backup components such as double failures of power [5, 17], network [52, 87]

**Correspondence:** The OSPF adjacency on one link of a two-link cut is the failed backup; the later link loss is the primary failure that exposes it.

**Evidence limits:** The study classifies outages by category; it does not describe an IGP timer mismatch on a backup link.

</details>

<a id="combination-source-latent_adjacency_cut-2"></a>
<details>
<summary>Evolve or Die: High-Availability Design Principles Drawn from Google&#x27;s Network Infrastructure (SIGCOMM 2016) (paper; co_occurrence)</summary>

Source: [Evolve or Die: High-Availability Design Principles Drawn from Google's Network Infrastructure (SIGCOMM 2016)](https://homepages.dcc.ufmg.br/~cunha/teaching/20221/cloudcomp/readings/03-readings/govindan16evolve.pdf). Location: Section 5, root-cause categories. Accessed: 2026-11-12.

> A single failure event can have more than one root cause

**Correspondence:** Each fault is a counterfactual root cause: without either, the cut keeps one working link and the network stays connected.

**Evidence limits:** The paper does not count multi-root-cause events or name this pair.

</details>

#### `latent_network_type_cut`

<a id="combination-source-latent_network_type_cut-1"></a>
<details>
<summary>Why Does the Cloud Stop Computing? Lessons from Hundreds of Service Outages (SoCC 2016) (paper; co_occurrence)</summary>

Source: [Why Does the Cloud Stop Computing? Lessons from Hundreds of Service Outages (SoCC 2016)](https://www.ece.iastate.edu/~mai/docs/papers/2016_SoCC_COS.pdf). Location: Section 4.3, Backups that also Fail. Accessed: 2026-11-12.

> simultaneous multiple failures of primary and backup components such as double failures of power [5, 17], network [52, 87]

**Correspondence:** The link that the network-type mismatch removes from SPF is the failed backup; the loss of the other link of the cut exposes it.

**Evidence limits:** The study classifies outages by category; it does not describe an OSPF network-type mismatch.

</details>

<a id="combination-source-latent_network_type_cut-2"></a>
<details>
<summary>Cisco, Troubleshoot Open Shortest Path First Route Database Issues (vendor_documentation; interaction)</summary>

Source: [Cisco, Troubleshoot Open Shortest Path First Route Database Issues](https://www.cisco.com/c/en/us/support/docs/ip/open-shortest-path-first-ospf/7112-26.html). Location: Reason 1: Network Type Mismatch. Accessed: 2026-11-12.

> network type mismatch makes the advertising router unreachable

**Correspondence:** The mismatched link carries no routes, so its loss of service is visible only when no other path remains.

**Evidence limits:** Describes the single fault.

</details>

#### `pmtud_black_hole`

<a id="combination-source-pmtud_black_hole-1"></a>
<details>
<summary>RFC 2923, TCP Problems with Path MTU Discovery (rfc; interaction)</summary>

Source: [RFC 2923, TCP Problems with Path MTU Discovery](https://www.rfc-editor.org/rfc/rfc2923.html#section-2.1). Location: Section 2.1, Black Hole Detection. Accessed: 2026-11-12.

> Its packets are disappearing into a PMTUD black hole.

**Correspondence:** NIKA lowers a routed interface MTU and filters ICMP Fragmentation Needed on the same router, so large DF packets vanish without feedback.

**Evidence limits:** RFC 2923 describes the filter on any device on the path; NIKA places both faults on one router.

</details>

<a id="combination-source-pmtud_black_hole-2"></a>
<details>
<summary>Path MTU discovery in practice (Cloudflare) (incident_report; co_occurrence)</summary>

Source: [Path MTU discovery in practice (Cloudflare)](https://blog.cloudflare.com/path-mtu-discovery-in-practice/). Location: Section Do-not-fragment bit. Accessed: 2026-11-12.

> The most common problems are caused by misconfigured firewalls that drop ICMP packets.

**Correspondence:** An operator reports filtered ICMP combined with a smaller path MTU as the common cause of stalled transfers.

**Evidence limits:** The post describes production patterns rather than one incident.

</details>

#### `policy_gap_exposed_by_uplink_loss`

<a id="combination-source-policy_gap_exposed_by_uplink_loss-1"></a>
<details>
<summary>RFC 8212, Default External BGP Route Propagation Behavior without Policies (rfc; interaction)</summary>

Source: [RFC 8212, Default External BGP Route Propagation Behavior without Policies](https://datatracker.ietf.org/doc/html/rfc8212#section-3). Location: Section 3, Changes to RFC 4271. Accessed: 2026-11-12.

> Routes SHALL NOT be added to an Adj-RIB-Out associated with an EBGP peer if no explicit Export Policy has been applied.

**Correspondence:** A leaf's session without import and export policy carries no routes. Its other session carries rack traffic until that uplink fails.

**Evidence limits:** The RFC specifies per-session route rejection; the redundant uplink placement and latent outage are the NIKA reproduction. RouterOS reproduces rejection with undefined filter chains.

</details>

<a id="combination-source-policy_gap_exposed_by_uplink_loss-2"></a>
<details>
<summary>Evolve or Die: High-Availability Design Principles Drawn from Google&#x27;s Network Infrastructure (SIGCOMM 2016) (paper; co_occurrence)</summary>

Source: [Evolve or Die: High-Availability Design Principles Drawn from Google's Network Infrastructure (SIGCOMM 2016)](https://cs538.github.io/readings/govindan16.pdf). Location: Section 5, Analysis of Failure Events. Accessed: 2026-11-12.

> if a link failure coincided with planned network maintenance operation that should have been safe due to redundancy

**Correspondence:** A configuration error removes one usable routing path; a later link failure removes the remaining redundancy.

**Evidence limits:** The paper describes maintenance coinciding with link loss and congestive loss, not this eBGP policy fault or a complete rack partition.

</details>

<a id="combination-source-policy_gap_exposed_by_uplink_loss-3"></a>
<details>
<summary>Cisco IOS XR, Implementing BGP (vendor_documentation; interaction)</summary>

Source: [Cisco IOS XR, Implementing BGP](https://www.cisco.com/c/en/us/td/docs/iosxr/ncs5500/bgp/710x/b-bgp-cg-ncs5500-710x/implementing-bgp.html). Location: Enable BGP Routing, Before you begin. Accessed: 2026-11-12.

> allow all eBGP neighbors to pass routes using the bgp unsafe-ebgp-policy command.

**Correspondence:** The healthy IOS XR rendering uses explicit pass policies and disables the global unsafe bypass, so removing one session's policies invokes default rejection.

**Evidence limits:** This documents the policy prerequisite, not the later redundant-uplink outage.

</details>

#### `prefix_surge_trips_low_limit`

<a id="combination-source-prefix_surge_trips_low_limit-1"></a>
<details>
<summary>Australian Senate, Chapter 2: The Optus network outage (incident_report; co_occurrence)</summary>

Source: [Australian Senate, Chapter 2: The Optus network outage](https://www.aph.gov.au/Parliamentary_Business/Committees/Senate/Environment_and_Communications/OptusNetworkOutage/Report/Chapter_2_-_The_Optus_network_outage). Location: Paragraphs 2.21 and 2.27. Accessed: 2026-11-12.

> a preset safety limit on these routers was reached or exceeded

**Correspondence:** Routing changes from an interconnected network exceeded configured safety limits and disconnected routers. NIKA separates the extra advertisements from the low terminating limit.

**Evidence limits:** The report does not publish router configuration or prefix counts. NIKA models the mechanism with FRR sessions and an isolated additional-prefix block, not the national outage scale or the upgrade.

</details>

<a id="combination-source-prefix_surge_trips_low_limit-2"></a>
<details>
<summary>RFC 7454, BGP Operations and Security (rfc; interaction)</summary>

Source: [RFC 7454, BGP Operations and Security](https://www.rfc-editor.org/rfc/rfc7454.html#section-8). Location: Section 8, Maximum Prefixes on a Peering. Accessed: 2026-11-12.

> It is important to regularly review the limits that are configured

**Correspondence:** The terminating limit needs headroom for expected route growth. NIKA sets it one prefix above the normal received count.

**Evidence limits:** The RFC recommends limit management; it does not specify the Optus configuration or its actual headroom.

</details>

#### `redundant_links_both_lost`

<a id="combination-source-redundant_links_both_lost-1"></a>
<details>
<summary>Evolve or Die: High-Availability Design Principles Drawn from Google&#x27;s Network Infrastructure (SIGCOMM 2016) (paper; co_occurrence)</summary>

Source: [Evolve or Die: High-Availability Design Principles Drawn from Google's Network Infrastructure (SIGCOMM 2016)](https://homepages.dcc.ufmg.br/~cunha/teaching/20221/cloudcomp/readings/03-readings/govindan16evolve.pdf). Location: Section 5 and Figure 7, Competing drains/upgrades. Accessed: 2026-11-12.

> if a link failure coincided with planned network maintenance operation that should have been safe due to redundancy

**Correspondence:** NIKA disables both links of a redundant pair; each is safe alone and together they partition the nodes behind them.

**Evidence limits:** The paper's example ends in congestive loss; NIKA's pair removes all redundancy, so the outcome is a partition.

</details>

#### `rov_gap_admits_hijack`

<a id="combination-source-rov_gap_admits_hijack-1"></a>
<details>
<summary>How Verizon and a BGP Optimizer Knocked Large Parts of the Internet Offline Today (Cloudflare) (incident_report; co_occurrence)</summary>

Source: [How Verizon and a BGP Optimizer Knocked Large Parts of the Internet Offline Today (Cloudflare)](https://blog.cloudflare.com/how-verizon-and-a-bgp-optimizer-knocked-large-parts-of-the-internet-offline-today/). Location: Section What happened?. Accessed: 2026-11-12.

> Verizon’s lack of filtering turned this into a major incident

**Correspondence:** A peer leaks routes it should not originate, and an upstream that does not filter them installs and propagates them; NIKA leaks RPKI-invalid routes toward a router whose invalid-route rejection is missing.

**Evidence limits:** The 2019 leak consisted of more-specific prefixes and Verizon lacked several filters; NIKA reproduces the missing filter with RPKI origin validation and an unauthorized-origin leak.

</details>

<a id="combination-source-rov_gap_admits_hijack-2"></a>
<details>
<summary>RFC 6811, BGP Prefix Origin Validation (rfc; interaction)</summary>

Source: [RFC 6811, BGP Prefix Origin Validation](https://www.rfc-editor.org/rfc/rfc6811.html#section-2). Location: Section 2, validation states. Accessed: 2026-11-12.

> Invalid: At least one VRP Covers the Route Prefix, but no VRP Matches it.

**Correspondence:** The leaked routes are Invalid; only a router whose import policy acts on that state rejects them, so the missing rejection matters only once invalid routes arrive.

**Evidence limits:** RFC 6811 leaves the policy for Invalid routes to the operator.

</details>

#### `selector_masks_readiness`

<a id="combination-source-selector_masks_readiness-1"></a>
<details>
<summary>Kubernetes, Debug Services (vendor_documentation; interaction)</summary>

Source: [Kubernetes, Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/). Location: Does the Service have any EndpointSlices?. Accessed: 2026-11-12.

> you should check that the `spec.selector` field of your Service actually selects for `metadata.labels` values on your Pods

**Correspondence:** A selector that matches no Pods leaves the Service without endpoints regardless of the Pods' readiness.

**Evidence limits:** The guide presents the checks in sequence; it does not discuss both conditions at once.

</details>

<a id="combination-source-selector_masks_readiness-2"></a>
<details>
<summary>Kubernetes, Pod Lifecycle (vendor_documentation; interaction)</summary>

Source: [Kubernetes, Pod Lifecycle](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/). Location: Container probes, readinessProbe. Accessed: 2026-11-12.

> If the readiness probe returns a failed state, the EndpointSlice controller removes the Pod&#x27;s IP address from the EndpointSlices of all Services that match the Pod.

**Correspondence:** Failing readiness shows on a Service only through the endpoints of Pods it matches, so the selector mismatch hides it.

**Evidence limits:** Describes readiness alone.

</details>

#### `standby_tunnel_allowed_ips_exposed`

<a id="combination-source-standby_tunnel_allowed_ips_exposed-1"></a>
<details>
<summary>Why Does the Cloud Stop Computing? Lessons from Hundreds of Service Outages (SoCC 2016) (paper; co_occurrence)</summary>

Source: [Why Does the Cloud Stop Computing? Lessons from Hundreds of Service Outages (SoCC 2016)](https://www.ece.iastate.edu/~mai/docs/papers/2016_SoCC_COS.pdf). Location: Section 4.3, Backups that also Fail. Accessed: 2026-11-12.

> simultaneous multiple failures of primary and backup components such as double failures of power [5, 17], network [52, 87]

**Correspondence:** The standby tunnel's AllowedIPs omission is the failed backup; the primary provider link loss exposes it.

**Evidence limits:** The study classifies outages by category; it does not describe a WireGuard AllowedIPs error.

</details>

<a id="combination-source-standby_tunnel_allowed_ips_exposed-2"></a>
<details>
<summary>WireGuard, Cryptokey Routing (vendor_documentation; interaction)</summary>

Source: [WireGuard, Cryptokey Routing](https://www.wireguard.com/#cryptokey-routing). Location: Cryptokey Routing, outgoing peer selection. Accessed: 2026-11-12.

> compares it to each peer&#x27;s list of allowed IPs

**Correspondence:** Traffic to the omitted prefix is refused only once routing sends it into the standby tunnel.

**Evidence limits:** Describes peer selection, not standby tunnels.

</details>

#### `standby_tunnel_key_exposed`

<a id="combination-source-standby_tunnel_key_exposed-1"></a>
<details>
<summary>Why Does the Cloud Stop Computing? Lessons from Hundreds of Service Outages (SoCC 2016) (paper; co_occurrence)</summary>

Source: [Why Does the Cloud Stop Computing? Lessons from Hundreds of Service Outages (SoCC 2016)](https://www.ece.iastate.edu/~mai/docs/papers/2016_SoCC_COS.pdf). Location: Section 4.3, Backups that also Fail. Accessed: 2026-11-12.

> simultaneous multiple failures of primary and backup components such as double failures of power [5, 17], network [52, 87]

**Correspondence:** The standby tunnel's wrong peer key is the failed backup; the primary provider link loss exposes it.

**Evidence limits:** The study classifies outages by category; it does not describe a WireGuard key error.

</details>

#### `undersized_backup_exposed`

<a id="combination-source-undersized_backup_exposed-1"></a>
<details>
<summary>Summary of the Amazon EC2 and Amazon RDS Service Disruption in the US East Region (AWS, April 2011) (incident_report; co_occurrence)</summary>

Source: [Summary of the Amazon EC2 and Amazon RDS Service Disruption in the US East Region (AWS, April 2011)](https://aws.amazon.com/message/65648/). Location: Primary Outage. Accessed: 2026-11-12.

> the traffic was routed onto the lower capacity redundant EBS network

**Correspondence:** Traffic moved onto a backup network too small for it; NIKA shapes a branch's standby provider link and then fails the primary.

**Evidence limits:** In the AWS event a configuration change shifted the traffic; NIKA uses a primary link failure as the trigger.

</details>

<a id="combination-source-undersized_backup_exposed-2"></a>
<details>
<summary>Summary of the Amazon EC2 and Amazon RDS Service Disruption in the US East Region (AWS, April 2011) (incident_report; interaction)</summary>

Source: [Summary of the Amazon EC2 and Amazon RDS Service Disruption in the US East Region (AWS, April 2011)](https://aws.amazon.com/message/65648/). Location: Overview of EBS System. Accessed: 2026-11-12.

> This network is not designed to handle all traffic from the primary network

**Correspondence:** The backup's capacity is a standing condition that matters only when the primary's traffic moves onto it.

**Evidence limits:** Same report.

</details>

<!-- combination-sources:end -->

## Recover receiver-limited TCP transfers

Recovery can reset in-flight transfers opened while the fault was active. Clients must retry those transfers. NIKA preserves connections present before injection and sockets whose receive buffers have grown past the injected cap. It selects established sockets by their kernel identity and receive-buffer readback, then uses a complete endpoint filter with [`ss -K`](https://man7.org/linux/man-pages/man8/ss.8.html). Recovery succeeds only after the selected socket identities disappear. An unavailable optional `rmem_max` is reported as null; recovery still requires the original `tcp_rmem` and autotuning settings.

## Regenerate benchmark candidates

Failure IDs, `TAGS`, and `COMPATIBLE_COLUMNS` define compatibility. Regenerate the candidate catalog after a registry, compatibility, topology, or inject-target change. Then refresh the failure × scenario tables in [Benchmark configuration](../benchmarks/benchmark-configuration.md):

```shell
uv run nika benchmark generate
uv run python scripts/render_coverage_matrix.py --write-docs
```

[`registry.py`](../../src/nika/problems/registry.py) discovers failure classes. [`base.py`](../../src/nika/problems/base.py) validates taxonomy metadata. Domain directories contain registered failures; [`support/`](../../src/nika/problems/support/) holds cross-domain inject helpers (Kubernetes base, node filter, CPU quota, P4 gateway, probe paths, inject param derivation). See [Benchmark configuration](../benchmarks/benchmark-configuration.md), [Create benchmark tasks](../development/creating-benchmark-tasks.md), and [Root-cause ground truth and scoring](../benchmarks/root-cause-evaluation.md).

## Public sources

The evidence below supports each failure's mechanism, reference implementation, or expected symptom. Each entry distinguishes the source's claim from NIKA's modeling choices. Source review establishes the correspondence with code; executable tests establish behavior in the supported environments.

<!-- failure-sources:start -->
Reviewed: 97 of 97 registered failures.

| Source type | Citation records | Distinct failures |
| --- | --- | --- |
| incident_report | 5 | 5 |
| operator_mailing_list | 0 | 0 |
| vendor_documentation | 67 | 65 |
| rfc | 31 | 30 |
| paper | 11 | 8 |

Citation records count repeated uses of a source. Distinct-failure counts overlap when a failure cites multiple source types.

### `arp_acl_block`

Implementation: [`ARPAclBlock`](../../src/nika/problems/forwarding_encapsulation_policy/acl.py).

<a id="source-arp_acl_block-1"></a>
<details>
<summary>Netfilter, nft(8) (vendor_documentation; reference_implementation)</summary>

Source: [Netfilter, nft(8)](https://netfilter.org/projects/nftables/manpage.html). Location: ADDRESS FAMILIES (arp); VERDICT STATEMENTS (drop). Accessed: 2026-11-10.

> drop

**Correspondence:** NIKA adds a drop verdict in the arp family and flushes dynamic and permanent neighbors. The nft manual defines the ARP family and unconditional packet drop, matching blocked ARP resolution.

**Evidence limits:** The flush ensures existing mappings cannot mask resolution failure. Native fabrics with static neighbors need this extra state change.

</details>

### `arp_cache_poisoning`

Implementation: [`ArpCachePoisoning`](../../src/nika/problems/security/arp.py).

<a id="source-arp_cache_poisoning-1"></a>
<details>
<summary>RFC 5227, IPv4 Address Conflict Detection (rfc; mechanism)</summary>

Source: [RFC 5227, IPv4 Address Conflict Detection](https://www.rfc-editor.org/rfc/rfc5227.html#section-5). Location: Section 5, Security Considerations. Accessed: 2026-11-10.

> fraudulent ARP packets

**Correspondence:** RFC 5227 describes forged ARP claiming an address with the attacker hardware address. NIKA installs an equivalent wrong gateway-to-MAC neighbor mapping directly.

**Evidence limits:** NIKA changes the cache rather than sending forged ARP. On fabrics that ignore Ethernet destination it adds an egress drop, so this is a cache-state/symptom model rather than a complete ARP attack implementation.

</details>

### `bgp_acl_block`

Implementation: [`BGPAclBlock`](../../src/nika/problems/forwarding_encapsulation_policy/acl.py).

<a id="source-bgp_acl_block-1"></a>
<details>
<summary>Netfilter, nft(8) (vendor_documentation; reference_implementation)</summary>

Source: [Netfilter, nft(8)](https://netfilter.org/projects/nftables/manpage.html). Location: TCP HEADER EXPRESSION (sport/dport); VERDICT STATEMENTS (drop). Accessed: 2026-11-10.

> drop

**Correspondence:** NIKA drops TCP source and destination port 179 and resets BGP sessions. The manual documents port matching and drop verdicts, directly supporting the Linux filter implementation.

**Evidence limits:** The reference establishes the packet filter, not the native vendor ACL syntax or a measured route-convergence interval.

</details>

### `bgp_address_family_not_activated`

Implementation: [`BGPAddressFamilyNotActivated`](../../src/nika/problems/routing_control_plane/bgp_afi.py).

<a id="source-bgp_address_family_not_activated-1"></a>
<details>
<summary>MikroTik RouterOS, /routing/bgp (vendor_documentation; mechanism)</summary>

Source: [MikroTik RouterOS, /routing/bgp](https://help.mikrotik.com/docs/spaces/ROS/pages/331612228/routing%2Bbgp). Location: /routing/bgp/template, afi. Accessed: 2026-11-10.

> List of address families

**Correspondence:** NIKA removes ip from the selected RouterOS neighbor afi and restarts its connection. The vendor defines afi as the address families whose routing information the peer exchanges.

**Evidence limits:** The source supports family selection, not a guarantee that all implementations keep the connection Established when their last active family is removed. NIKA limits this fault to RouterOS.

</details>

### `bgp_asn_misconfig`

Implementation: [`BGPAsnMisconfig`](../../src/nika/problems/routing_control_plane/bgp.py).

<a id="source-bgp_asn_misconfig-1"></a>
<details>
<summary>RFC 4271, A Border Gateway Protocol 4 (BGP-4) (rfc; mechanism)</summary>

Source: [RFC 4271, A Border Gateway Protocol 4 (BGP-4)](https://www.rfc-editor.org/rfc/rfc4271.html#section-6.2). Location: Section 6.2, OPEN Message Error Handling. Accessed: 2026-11-10.

> the Error Subcode MUST be set to Bad Peer AS

**Correspondence:** NIKA changes the local BGP ASN while peers retain their configured expected ASN; an unacceptable AS in OPEN triggers the specified error.

**Evidence limits:** RFC 4271 leaves acceptable ASN selection to local policy. The link to rejection assumes peers enforce their expected remote ASN.

</details>

### `bgp_blackhole_community_leak`

Implementation: [`BGPBlackholeCommunityLeak`](../../src/nika/problems/routing_control_plane/bgp.py).

<a id="source-bgp_blackhole_community_leak-1"></a>
<details>
<summary>RFC 5635, Remote Triggered Black Hole Filtering with uRPF (rfc; mechanism)</summary>

Source: [RFC 5635, Remote Triggered Black Hole Filtering with uRPF](https://www.rfc-editor.org/rfc/rfc5635.html#section-3). Location: Section 3, Using the RTBH filtering, steps 1 and 2. Accessed: 2026-11-10.

> tagged with the chosen &quot;RTBH&quot; community

**Correspondence:** NIKA adds the provider RTBH community to a legitimately originated prefix. The receiver policy maps tagged routes to a discard next hop, matching packet loss with the origin ASN unchanged.

**Evidence limits:** The source describes intentional RTBH operation. NIKA models an erroneous export of a trigger community; it does not reproduce uRPF source filtering or a particular operator incident.

</details>

### `bgp_hijacking`

Implementation: [`BGPHijacking`](../../src/nika/problems/security/bgp.py).

<a id="source-bgp_hijacking-1"></a>
<details>
<summary>FRRouting, BGP Networks (vendor_documentation; reference_implementation)</summary>

Source: [FRRouting, BGP Networks](https://docs.frrouting.org/en/latest/bgp.html#networks). Location: Networks, network A.B.C.D/M. Accessed: 2026-11-10.

> adds the announcement network

**Correspondence:** NIKA assigns an address in the victim prefix to a local interface and originates that prefix through a BGP network statement and export policy. FRR documents that statement as announcing the prefix to neighbors.

**Evidence limits:** The source supports the reference origination configuration, not authorization of the originating AS or whether the forged route wins at every receiver. Native profiles use equivalent origination operations.

</details>

<a id="source-bgp_hijacking-2"></a>
<details>
<summary>RFC 6811, BGP Prefix Origin Validation (rfc; mechanism)</summary>

Source: [RFC 6811, BGP Prefix Origin Validation](https://www.rfc-editor.org/rfc/rfc6811.html#section-2). Location: Section 2, validation procedure; Section 5, deployment considerations. Accessed: 2026-11-10.

> rejecting all &quot;invalid&quot; routes

**Correspondence:** NIKA permits export of prefixes with the unauthorized origin origin ASN while ROAs authorize the legitimate origin. RFC 6811 marks a covered route invalid when no authorization matches and permits invalid-route rejection, matching the ROV/non-ROV observer difference.

**Evidence limits:** ROV rejection is local policy, not mandatory. This implementation changes origin and creates MOAS; it does not model every form of path-only route leak.

</details>

### `bgp_max_prefix_limit_low`

Implementation: [`BGPLowPrefixLimit`](../../src/nika/problems/routing_control_plane/bgp_prefix_surge.py).

<a id="source-bgp_max_prefix_limit_low-1"></a>
<details>
<summary>RFC 7454, BGP Operations and Security (rfc; mechanism)</summary>

Source: [RFC 7454, BGP Operations and Security](https://www.rfc-editor.org/rfc/rfc7454.html#section-8). Location: Section 8, Maximum Prefixes on a Peering. Accessed: 2026-11-12.

> It is important to regularly review the limits that are configured

**Correspondence:** The terminating limit needs headroom for expected route growth. NIKA sets it one prefix above the normal received count.

**Evidence limits:** The RFC recommends limit management; it does not specify the Optus configuration or its actual headroom.

</details>

### `bgp_md5_password_mismatch`

Implementation: [`BGPMd5PasswordMismatch`](../../src/nika/problems/routing_control_plane/bgp.py).

<a id="source-bgp_md5_password_mismatch-1"></a>
<details>
<summary>RFC 2385, Protection of BGP Sessions via the TCP MD5 Signature Option (rfc; mechanism)</summary>

Source: [RFC 2385, Protection of BGP Sessions via the TCP MD5 Signature Option](https://www.rfc-editor.org/rfc/rfc2385.html#section-2.0). Location: Section 2.0, Proposal. Accessed: 2026-11-10.

> segment being dropped

**Correspondence:** NIKA adds a TCP MD5 password on one endpoint of an unsigned BGP connection. RFC 2385 requires invalid digests to be discarded; an endpoint configured to require MD5 cannot establish the unsigned peer connection.

**Evidence limits:** This source specifies authentication behavior; platform-specific counters, reset timing and restoration are implementation details.

</details>

### `bgp_missing_route_advertisement`

Implementation: [`BGPMissingAdvertise`](../../src/nika/problems/routing_control_plane/bgp.py).

<a id="source-bgp_missing_route_advertisement-1"></a>
<details>
<summary>FRRouting, BGP Networks (vendor_documentation; reference_implementation)</summary>

Source: [FRRouting, BGP Networks](https://docs.frrouting.org/en/latest/bgp.html#networks). Location: Networks, network A.B.C.D/M. Accessed: 2026-11-10.

> This command adds the announcement network.

**Correspondence:** NIKA removes originating BGP network statements on ordinary FRR targets, disables connected redistribution in enterprise VRFs, or applies the native export withdrawal. The cited network command supports the network-statement branch: removing it removes that configured origin.

**Evidence limits:** The cited command alone does not document the redistribution or native export-policy branches. Other origin mechanisms can still advertise a prefix; withdrawal impact depends on the selected inventory and is checked separately.

</details>

### `bgp_prefix_list_length_mismatch`

Implementation: [`BGPPrefixListLengthMismatch`](../../src/nika/problems/routing_control_plane/bgp_prefix.py).

<a id="source-bgp_prefix_list_length_mismatch-1"></a>
<details>
<summary>FRRouting, Filtering (vendor_documentation; mechanism)</summary>

Source: [FRRouting, Filtering](https://docs.frrouting.org/en/latest/filter.html#ip-prefix-list). Location: IP Prefix List, omitted le/ge. Accessed: 2026-11-10.

> the prefix length must match exactly

**Correspondence:** NIKA changes an aggregate import filter from /8 le 24 to exact /8. The vendor specifies exact prefix-length matching when le/ge are omitted, which rejects the intended /24 specifics while the policy remains bound.

**Evidence limits:** This source directly supports FRR. RouterOS uses prefix equality instead of removing a length qualifier; its prefix-operator reference provides that correspondence.

</details>

<a id="source-bgp_prefix_list_length_mismatch-2"></a>
<details>
<summary>MikroTik RouterOS, Route Selection and Filters (vendor_documentation; mechanism)</summary>

Source: [MikroTik RouterOS, Route Selection and Filters](https://help.mikrotik.com/docs/spaces/ROS/pages/74678285/Route%2BSelection%2Band%2BFilters). Location: Prefix Operators, ==. Accessed: 2026-11-10.

> prefix is equal

**Correspondence:** NIKA replaces containment plus dst-len matching with dst == 10.0.0.0/8. The vendor prefix equality operator excludes the remote /24s.

**Evidence limits:** Deleting a dst-len qualifier alone would not establish this fault on RouterOS because the in operator still matches subnets.

</details>

### `bgp_prefix_surge`

Implementation: [`BGPPrefixSurge`](../../src/nika/problems/routing_control_plane/bgp_prefix_surge.py).

<a id="source-bgp_prefix_surge-1"></a>
<details>
<summary>Australian Senate, Chapter 2: The Optus network outage (incident_report; mechanism)</summary>

Source: [Australian Senate, Chapter 2: The Optus network outage](https://www.aph.gov.au/Parliamentary_Business/Committees/Senate/Environment_and_Communications/OptusNetworkOutage/Report/Chapter_2_-_The_Optus_network_outage). Location: Paragraphs 2.21 and 2.27. Accessed: 2026-11-12.

> a preset safety limit on these routers was reached or exceeded

**Correspondence:** Routing changes from an interconnected network exceeded configured safety limits and disconnected routers. NIKA separates the extra advertisements from the low terminating limit.

**Evidence limits:** The report does not publish router configuration or prefix counts. NIKA models the mechanism with FRR sessions and an isolated additional-prefix block, not the national outage scale or the upgrade.

</details>

### `bgp_route_leak`

Implementation: [`BGPRouteLeak`](../../src/nika/problems/routing_control_plane/route_leak.py).

<a id="source-bgp_route_leak-1"></a>
<details>
<summary>RFC 7908, Problem Definition and Classification of BGP Route Leaks (rfc; mechanism)</summary>

Source: [RFC 7908, Problem Definition and Classification of BGP Route Leaks](https://www.rfc-editor.org/rfc/rfc7908.html). Location: Sections 3.1–3.4: Type 1 hairpin; types 2–4 peer/provider export violations. Accessed: 2026-11-15.

> Neither the prefix nor the AS path in the update is altered.

**Correspondence:** NIKA bypasses one upstream export restriction on a multihomed customer. The learned full prefix and original origin AS remain intact; normal eBGP prepending adds the customer AS.

**Evidence limits:** Implements the type 1 provider-customer-provider hairpin. Types 2–4 describe related peer export violations; they are taxonomy references, not additional injected variants.

</details>

<a id="source-bgp_route_leak-2"></a>
<details>
<summary>RFC 9234, Route Leak Prevention and Detection Using Roles in UPDATE and OPEN Messages (rfc; mechanism)</summary>

Source: [RFC 9234, Route Leak Prevention and Detection Using Roles in UPDATE and OPEN Messages](https://www.rfc-editor.org/rfc/rfc9234.html). Location: Sections 3.1, 4 and 5: peering relationships, BGP Role and OTC. Accessed: 2026-11-15.

> It prevents ASes from creating leaks and detects leaks

**Correspondence:** The declared provider/customer/peer relationships define the healthy export contract. BGP roles and Only to Customer (OTC) are the protocol mitigation for this class of violation.

**Evidence limits:** NIKA uses ordinary AS-path export filters and customer route preference. It does not enable negotiated roles or OTC enforcement, which would prevent this injected leak.

</details>

<a id="source-bgp_route_leak-3"></a>
<details>
<summary>Cloudflare, How Verizon and a BGP Optimizer Knocked Large Parts of the Internet Offline Today (incident_report; mechanism)</summary>

Source: [Cloudflare, How Verizon and a BGP Optimizer Knocked Large Parts of the Internet Offline Today](https://blog.cloudflare.com/how-verizon-and-a-bgp-optimizer-knocked-large-parts-of-the-internet-offline-today/). Location: 24 June 2019, The leak section: DQE → Allegheny → Verizon. Accessed: 2026-11-15.

> sent to their other transit provider

**Correspondence:** The incident documents a customer forwarding routes learned from DQE to its other transit provider Verizon. NIKA reproduces the unauthorized upstream transit and resulting traffic detour.

**Evidence limits:** The incident also involved optimizer-generated more-specific prefixes and overload. NIKA uses an unchanged full prefix and does not reproduce those additional mechanisms.

</details>

### `bgp_rov_not_enforced`

Implementation: [`BGPROVNotEnforced`](../../src/nika/problems/routing_control_plane/bgp.py).

<a id="source-bgp_rov_not_enforced-1"></a>
<details>
<summary>RFC 6811, BGP Prefix Origin Validation (rfc; mechanism)</summary>

Source: [RFC 6811, BGP Prefix Origin Validation](https://www.rfc-editor.org/rfc/rfc6811.html#section-2). Location: Section 2, validation states; Section 5, invalid-route policy. Accessed: 2026-11-12.

> Invalid: At least one VRP Covers the Route Prefix, but no VRP Matches it.

**Correspondence:** The router retains origin validation data but loses its local policy to reject invalid routes. Section 5 describes rejecting all invalid routes as one possible policy.

**Evidence limits:** RFC 6811 defines validation states; it does not mandate invalid-route rejection. NIKA models a router whose intended import policy rejects them.

</details>

<a id="source-bgp_rov_not_enforced-2"></a>
<details>
<summary>FRR BGP documentation, Validating BGP Updates (vendor_documentation; reference_implementation)</summary>

Source: [FRR BGP documentation, Validating BGP Updates](https://docs.frrouting.org/en/latest/bgp.html#validating-bgp-updates). Location: Validating BGP Updates, match rpki notfound|invalid|valid. Accessed: 2026-11-12.

> Create a clause for a route map to match prefixes with the specified RPKI state.

**Correspondence:** The healthy BGP-IN deny sequence matches rpki invalid. Removing that sequence leaves the later business-prefix permit and RTR configuration intact.

**Evidence limits:** The example in the FRR documentation changes local preference. NIKA uses the documented matcher in a deny sequence.

</details>

<a id="source-bgp_rov_not_enforced-3"></a>
<details>
<summary>Cloudflare, How Verizon and a BGP Optimizer Knocked Large Parts of the Internet Offline Today (incident_report; mechanism)</summary>

Source: [Cloudflare, How Verizon and a BGP Optimizer Knocked Large Parts of the Internet Offline Today](https://blog.cloudflare.com/how-verizon-and-a-bgp-optimizer-knocked-large-parts-of-the-internet-offline-today/). Location: 2019-06-24 incident account, upstream filtering at Verizon. Accessed: 2026-11-12.

> Verizon’s lack of filtering turned this into a major incident

**Correspondence:** The Verizon/DQE incident motivates a separate upstream filtering failure that can combine with a route leak. Alone, the missing invalid-deny policy has no traffic effect without invalid announcements.

**Evidence limits:** The incident involved more-specific route leaks. NIKA retains the existing RPKI profile with an unauthorized origin; this is a mechanism-level reproduction.

</details>

### `bgp_session_policy_missing`

Implementation: [`BGPSessionPolicyMissing`](../../src/nika/problems/routing_control_plane/bgp.py).

<a id="source-bgp_session_policy_missing-1"></a>
<details>
<summary>RFC 8212, Default External BGP Route Propagation Behavior without Policies (rfc; mechanism)</summary>

Source: [RFC 8212, Default External BGP Route Propagation Behavior without Policies](https://www.rfc-editor.org/rfc/rfc8212.html#section-3). Location: Section 3, Changes to RFC 4271. Accessed: 2026-11-10.

> if no explicit Export Policy has been applied

**Correspondence:** NIKA removes IOS XR eBGP route policies. RFC 8212 excludes routes from import decision and export advertisement without explicit policy, matching Established sessions carrying no usable routes.

**Evidence limits:** This RFC directly supports IOS XR default-reject behavior. RouterOS uses named empty filter chains, whose default-reject rule needs its vendor reference.

</details>

<a id="source-bgp_session_policy_missing-2"></a>
<details>
<summary>MikroTik RouterOS, Route Selection and Filters (vendor_documentation; mechanism)</summary>

Source: [MikroTik RouterOS, Route Selection and Filters](https://help.mikrotik.com/docs/spaces/ROS/pages/74678285/Route%2BSelection%2Band%2BFilters). Location: Route Filtering, default action. Accessed: 2026-11-10.

> default action of the routing filter chain is &quot;reject&quot;

**Correspondence:** NIKA binds RouterOS connections to filter chains with no accepting rule, so the vendor default reject removes usable import and export routes while sessions remain up.

**Evidence limits:** An unspecified filter accepts everything in RouterOS. This correspondence requires the named empty-chain implementation, not merely an absent filter setting.

</details>

### `bgp_ttl_security_mismatch`

Implementation: [`BGPTtlSecurityMismatch`](../../src/nika/problems/routing_control_plane/bgp.py).

<a id="source-bgp_ttl_security_mismatch-1"></a>
<details>
<summary>AWS Direct Connect, Troubleshoot BGP TTL security (GTSM) issues (vendor_documentation; mechanism)</summary>

Source: [AWS Direct Connect, Troubleshoot BGP TTL security (GTSM) issues](https://docs.aws.amazon.com/directconnect/latest/UserGuide/ts-ttl-security.html). Location: Troubleshoot BGP TTL security (GTSM) issues. Accessed: 2026-11-10.

> sends BGP packets with an IP Time-to-Live (TTL) value of 1

**Correspondence:** AWS documents eBGP establishment failure when a router enables TTL security against a peer sending TTL 1. NIKA enables one-sided GTSM and resets sessions while peers retain ordinary TTL, reproducing that mismatch.

**Evidence limits:** The source directly supports the single-hop eBGP case. FRR multi-hop iBGP rejection and directly connected iBGP survival depend on the actual received TTL.

</details>

### `bmv2_switch_down`

Implementation: [`Bmv2SwitchDown`](../../src/nika/problems/forwarding_encapsulation_policy/switch.py).

<a id="source-bmv2_switch_down-1"></a>
<details>
<summary>P4lang, Behavioral Model targets (vendor_documentation; reference_implementation)</summary>

Source: [P4lang, Behavioral Model targets](https://raw.githubusercontent.com/p4lang/behavioral-model/main/targets/README.md). Location: simple_switch; simple_switch_grpc. Accessed: 2026-11-10.

> software switch

**Correspondence:** NIKA kills the simple_switch/simple_switch_grpc process. The target documentation identifies those binaries as the software switch executing the P4 dataplane, so killing that process removes forwarding through the emulated device.

**Evidence limits:** This models complete software dataplane process failure. It does not establish a physical chassis failure or a vendor hardware failure mode.

</details>

### `device_flow_blackhole`

Implementation: [`DeviceFlowBlackhole`](../../src/nika/problems/forwarding_encapsulation_policy/flow_blackhole.py).

<a id="source-device_flow_blackhole-1"></a>
<details>
<summary>Guo et al., Pingmesh: A Large-Scale System for Data Center Network Latency Measurement and Analysis, SIGCOMM 2015 (paper; mechanism)</summary>

Source: [Guo et al., Pingmesh: A Large-Scale System for Data Center Network Latency Measurement and Analysis, SIGCOMM 2015](https://conferences.sigcomm.org/sigcomm/2015/pdf/papers/p139.pdf). Location: Section 5.1, second packet black-hole type. Accessed: 2026-11-10.

> errors related to ECMP

**Correspondence:** Pingmesh reports deterministic loss selected by source/destination addresses and ports while other port choices succeed, and suspects ECMP selection. NIKA hashes forwarded TCP/UDP five-tuples and drops a selected subset, preserving ICMP.

**Evidence limits:** The paper suspects rather than proves an ECMP root cause. NIKA is a deterministic classifier model, not an implementation of the original switch defect; its selected fraction is lab-specific.

</details>

### `device_forwarding_packet_corruption`

Implementation: [`DeviceForwardingPacketCorruption`](../../src/nika/problems/forwarding_encapsulation_policy/switch_internal_corruption.py).

<a id="source-device_forwarding_packet_corruption-1"></a>
<details>
<summary>Linux BPF helper reference, bpf-helpers(7) (vendor_documentation; reference_implementation)</summary>

Source: [Linux BPF helper reference, bpf-helpers(7)](https://man7.org/linux/man-pages/man7/bpf-helpers.7.html). Location: bpf_skb_store_bytes, flags including BPF_F_RECOMPUTE_CSUM. Accessed: 2026-11-10.

> bpf_skb_store_bytes

**Correspondence:** NIKA switch_internal_corruption.bpf.c flips a selected TCP payload byte and writes it with bpf_skb_store_bytes flags=0. The helper reference defines packet-byte replacement and optional checksum recomputation, matching deliberate payload mutation without repairing its TCP checksum.

**Evidence limits:** This is an eBPF reference implementation of corruption after forwarding. It does not reproduce a particular ASIC defect or physical bit-error distribution; Containerlab attaches on the external host-side wire.

</details>

<a id="source-device_forwarding_packet_corruption-2"></a>
<details>
<summary>Stone and Partridge, When the CRC and TCP Checksum Disagree, SIGCOMM 2000 (paper; mechanism)</summary>

Source: [Stone and Partridge, When the CRC and TCP Checksum Disagree, SIGCOMM 2000](https://conferences.sigcomm.org/sigcomm/2000/conf/paper/sigcomm2000-9-1.pdf). Location: Section 1, introduction. Accessed: 2026-11-16.

> intermediate systems

**Correspondence:** The paper describes transport checksum errors that pass link CRC checks and identifies intermediate systems or hosts as possible sources. NIKA models payload corruption inside one forwarding device before egress FCS generation, then requires checksum errors on independent paths through that device.

**Evidence limits:** The paper does not establish NIKA's particular flow hash, bit position, corruption rate, or a specific switch ASIC defect. These remain model choices.

</details>

### `dhcp_missing_subnet`

Implementation: [`DHCPMissingSubnet`](../../src/nika/problems/addressing_neighbor_naming/dhcp.py).

<a id="source-dhcp_missing_subnet-1"></a>
<details>
<summary>ISC DHCP 4.4, dhcpd.conf (vendor_documentation; mechanism)</summary>

Source: [ISC DHCP 4.4, dhcpd.conf](https://kb.isc.org/docs/isc-dhcp-44-manual-pages-dhcpdconf). Location: DESCRIPTION, subnet declaration requirement. Accessed: 2026-11-10.

> there must be one subnet declaration

**Correspondence:** DHCPMissingSubnet removes the declaration for the client subnet and renews leases. ISC requires a subnet declaration for every served subnet, with ranges inside it for dynamic allocation.

**Evidence limits:** The manual establishes the required configuration. Server startup behavior after removing a declaration depends on its other configured subnets.

</details>

### `dhcp_option_dns`

Implementation: [`DHCPOptionDNS`](../../src/nika/problems/addressing_neighbor_naming/dhcp_options.py).

<a id="source-dhcp_option_dns-1"></a>
<details>
<summary>RFC 2132, DHCP Options and BOOTP Vendor Extensions (rfc; mechanism)</summary>

Source: [RFC 2132, DHCP Options and BOOTP Vendor Extensions](https://www.rfc-editor.org/rfc/rfc2132.html#section-3.8). Location: Section 3.8, Domain Name Server Option. Accessed: 2026-11-10.

> name servers available to the client

**Correspondence:** NIKA changes an option on the legitimate DHCP server and renews the affected client lease. The resulting client configuration follows the incorrect server option.

**Evidence limits:** NIKA models DHCP option misconfiguration; it does not introduce a rogue DHCP server.

</details>

### `dhcp_option_gateway`

Implementation: [`DHCPOptionGateway`](../../src/nika/problems/addressing_neighbor_naming/dhcp_options.py).

<a id="source-dhcp_option_gateway-1"></a>
<details>
<summary>RFC 2132, DHCP Options and BOOTP Vendor Extensions (rfc; mechanism)</summary>

Source: [RFC 2132, DHCP Options and BOOTP Vendor Extensions](https://www.rfc-editor.org/rfc/rfc2132.html#section-3.5). Location: Section 3.5, Router Option. Accessed: 2026-11-10.

> routers on the client&#x27;s subnet

**Correspondence:** NIKA changes an option on the legitimate DHCP server and renews the affected client lease. The resulting client configuration follows the incorrect server option.

**Evidence limits:** NIKA models DHCP option misconfiguration; it does not introduce a rogue DHCP server.

</details>

### `dhcp_option_subnet`

Implementation: [`DHCPOptionSubnet`](../../src/nika/problems/addressing_neighbor_naming/dhcp_options.py).

<a id="source-dhcp_option_subnet-1"></a>
<details>
<summary>RFC 2132, DHCP Options and BOOTP Vendor Extensions (rfc; mechanism)</summary>

Source: [RFC 2132, DHCP Options and BOOTP Vendor Extensions](https://www.rfc-editor.org/rfc/rfc2132.html#section-3.3). Location: Section 3.3, Subnet Mask. Accessed: 2026-11-10.

> specifies the client&#x27;s subnet mask

**Correspondence:** NIKA changes an option on the legitimate DHCP server and renews the affected client lease. The resulting client configuration follows the incorrect server option.

**Evidence limits:** NIKA models DHCP option misconfiguration; it does not introduce a rogue DHCP server.

</details>

### `dhcp_service_down`

Implementation: [`DHCPServiceDown`](../../src/nika/problems/addressing_neighbor_naming/service_failure.py).

<a id="source-dhcp_service_down-1"></a>
<details>
<summary>RFC 2131, Dynamic Host Configuration Protocol (rfc; symptom)</summary>

Source: [RFC 2131, Dynamic Host Configuration Protocol](https://www.rfc-editor.org/rfc/rfc2131.html#section-4.4.5). Location: Section 4.4.5, Reacquisition and expiration. Accessed: 2026-11-10.

> If the lease expires before the client receives a DHCPACK

**Correspondence:** The injector stops isc-dhcp-server, kills dhcpd and renews clients. RFC 2131 describes unanswered renewal requests and loss of the lease on expiration.

**Evidence limits:** This is a stopped DHCP service, not evidence of a host crash. NIKA forces renewal rather than waiting for natural expiration; existing leases can mask a real outage. NIKA stops the service on a running host and forces client renewal; it does not crash the host.

</details>

### `dns_port_blocked`

Implementation: [`DNSPortBlocked`](../../src/nika/problems/forwarding_encapsulation_policy/acl.py).

<a id="source-dns_port_blocked-1"></a>
<details>
<summary>RFC 1035, Domain Names: Implementation and Specification (rfc; mechanism)</summary>

Source: [RFC 1035, Domain Names: Implementation and Specification](https://www.rfc-editor.org/rfc/rfc1035.html#section-4.2). Location: Sections 4.2.1 and 4.2.2, UDP and TCP usage. Accessed: 2026-11-10.

> Server port 53

**Correspondence:** NIKA drops both UDP and TCP destination port 53. RFC 1035 assigns DNS server traffic to those transports and port, so both query paths are blocked.

**Evidence limits:** A dropped query times out; this does not model a DNS empty-answer response. The RFC defines the transport dependency, not the nftables implementation.

</details>

<a id="source-dns_port_blocked-2"></a>
<details>
<summary>Kubernetes, Debugging DNS Resolution (vendor_documentation; mechanism)</summary>

Source: [Kubernetes, Debugging DNS Resolution](https://kubernetes.io/docs/tasks/administer-cluster/dns-debugging-resolution/). Location: Is DNS service up?; Are DNS endpoints exposed?. Accessed: 2026-11-10.

> 53/UDP,53/TCP

**Correspondence:** NIKA filters TCP and UDP port 53 to the kube-dns Service and CoreDNS pod addresses. The guide identifies that Service and its DNS endpoints on those ports, matching name-resolution failure with live CoreDNS pods.

**Evidence limits:** The source supports the required DNS paths and diagnostic controls. It does not describe this particular node firewall rule as a real incident.

</details>

### `dns_record_error`

Implementation: [`DNSRecordError`](../../src/nika/problems/addressing_neighbor_naming/dns.py).

<a id="source-dns_record_error-1"></a>
<details>
<summary>RFC 1035, Domain Names: Implementation and Specification (rfc; mechanism)</summary>

Source: [RFC 1035, Domain Names: Implementation and Specification](https://www.rfc-editor.org/rfc/rfc1035.html#section-3.4.1). Location: Section 3.4.1, A RDATA format; Section 5.3, master file example. Accessed: 2026-11-10.

> A records cause no additional section processing.

**Correspondence:** NIKA rewrites an A record in the BIND master zone and reloads it. Section 3.4.1 defines its RDATA as a 32-bit Internet address; Section 5.3 shows address records in a zone.

**Evidence limits:** This supports an incorrect authoritative name-to-address mapping. It does not establish cache poisoning or a forged DNS response.

</details>

### `dns_service_down`

Implementation: [`DNSServiceDown`](../../src/nika/problems/addressing_neighbor_naming/service_failure.py).

<a id="source-dns_service_down-1"></a>
<details>
<summary>BIND 9, named manual (vendor_documentation; mechanism)</summary>

Source: [BIND 9, named manual](https://bind9.readthedocs.io/en/latest/manpages.html#named-internet-domain-name-server). Location: named: Description. Accessed: 2026-11-10.

> named is a Domain Name System (DNS) server

**Correspondence:** DNSServiceDown kills the named process, which the BIND manual identifies as the DNS server. Removing that process removes its query-serving function.

**Evidence limits:** This models a DNS process outage, not a host crash. Cached answers or alternative resolvers can preserve name-based access.

</details>

### `endpoint_cpu_contention`

Implementation: [`EndpointCPUContention`](../../src/nika/problems/endpoint_application/transport.py).

<a id="source-endpoint_cpu_contention-1"></a>
<details>
<summary>Dan Luu and David Mackey, The container throttling problem (Twitter operational analysis) (incident_report; mechanism)</summary>

Source: [Dan Luu and David Mackey, The container throttling problem (Twitter operational analysis)](https://danluu.com/cgroup-throttling/). Location: The problem, in theory; The problem, in practice [case study]. Accessed: 2026-11-10.

> thread pools that are much larger than their mesos core reservation

**Correspondence:** NIKA applies a CPU quota and starts competing CPU-bound batch workers on the selected endpoint. The endpoint node determines which side of the transfer suffers contention.

**Evidence limits:** This is an operational analysis with case studies, not a single-outage postmortem. It supports quota contention; it does not validate NIKA receiver-window behavior or download slowdown magnitude.

</details>

<a id="source-endpoint_cpu_contention-2"></a>
<details>
<summary>Linux kernel, CFS Bandwidth Control (vendor_documentation; mechanism)</summary>

Source: [Linux kernel, CFS Bandwidth Control](https://docs.kernel.org/scheduler/sched-bwc.html). Location: Management; quota exhaustion and throttling. Accessed: 2026-11-10.

> threads being throttled

**Correspondence:** NIKA applies a CPU quota and starts competing CPU-bound batch workers on the selected endpoint. The endpoint node determines which side of the transfer suffers contention.

**Evidence limits:** The source does not establish the NIKA HTTP throughput ratio or imply congestion in the network; the handler and background job are workload choices.

</details>

### `flow_rule_loop`

Implementation: [`FlowRuleLoop`](../../src/nika/problems/forwarding_encapsulation_policy/flow_rules.py).

<a id="source-flow_rule_loop-1"></a>
<details>
<summary>Open vSwitch, Using OpenFlow (vendor_documentation; reference_implementation)</summary>

Source: [Open vSwitch, Using OpenFlow](https://docs.openvswitch.org/en/latest/faq/openflow/). Location: Question: sending packets out the ingress port. Accessed: 2026-11-10.

> OFPP_IN_PORT

**Correspondence:** NIKA installs paired IPv4 rules outputting to OpenFlow IN_PORT with TTL decrement. The OVS FAQ explicitly documents OFPP_IN_PORT hairpin output and why ordinary same-port numeric output is ignored to avoid loops.

**Evidence limits:** The source supports hairpin forwarding; persistent looping requires the paired rules and topology. NIKA decrements TTL so traffic eventually expires.

</details>

### `flow_rule_shadowing`

Implementation: [`FlowRuleShadowing`](../../src/nika/problems/forwarding_encapsulation_policy/flow_rules.py).

<a id="source-flow_rule_shadowing-1"></a>
<details>
<summary>Open vSwitch, ovs-ofctl(8) (vendor_documentation; reference_implementation)</summary>

Source: [Open vSwitch, ovs-ofctl(8)](https://www.openvswitch.org/support/dist-docs/ovs-ofctl.8.html). Location: Flow Syntax, priority; actions, empty action list. Accessed: 2026-11-10.

> priority

**Correspondence:** NIKA installs a higher-priority IPv4 prefix rule with no output actions through ONOS. ovs-ofctl documents priority-based rule selection and dropping when an action list is empty, matching a prefix drop shadowing lower-priority forwarding.

**Evidence limits:** This supports OpenFlow match/action behavior, not an ONOS software defect. Other prefixes and non-IPv4 traffic do not match the injected selector.

</details>

### `host_acl_block`

Implementation: [`HostACLBlock`](../../src/nika/problems/forwarding_encapsulation_policy/acl.py).

<a id="source-host_acl_block-1"></a>
<details>
<summary>Netfilter, nft(8) (vendor_documentation; reference_implementation)</summary>

Source: [Netfilter, nft(8)](https://netfilter.org/projects/nftables/manpage.html). Location: TCP HEADER EXPRESSION (dport); VERDICT STATEMENTS (drop). Accessed: 2026-11-10.

> drop

**Correspondence:** NIKA installs tcp dport 80 drop, using the documented transport-port match and terminal drop verdict to block cleartext HTTP on the target.

**Evidence limits:** Only port 80 is selected. HTTPS, alternative HTTP ports and other protocols are outside this injected rule.

</details>

### `host_incorrect_dns`

Implementation: [`HostIncorrectDNS`](../../src/nika/problems/addressing_neighbor_naming/host_configuration.py).

<a id="source-host_incorrect_dns-1"></a>
<details>
<summary>Linux man-pages, resolv.conf(5) (vendor_documentation; mechanism)</summary>

Source: [Linux man-pages, resolv.conf(5)](https://man7.org/linux/man-pages/man5/resolv.conf.5.html). Location: DESCRIPTION, nameserver. Accessed: 2026-11-10.

> Name server IP address

**Correspondence:** NIKA replaces nameserver lines in resolv.conf and retains that choice through DHCP renewal. The documented nameserver setting tells the resolver which server to query.

**Evidence limits:** The source supports resolver misconfiguration; lookup failure depends on the injected resolver and caches. No DNS answer forgery is implied.

</details>

### `host_incorrect_gateway`

Implementation: [`HostIncorrectGateway`](../../src/nika/problems/addressing_neighbor_naming/host_configuration.py).

<a id="source-host_incorrect_gateway-1"></a>
<details>
<summary>RFC 1122, Requirements for Internet Hosts: Communication Layers (rfc; mechanism)</summary>

Source: [RFC 1122, Requirements for Internet Hosts: Communication Layers](https://www.rfc-editor.org/rfc/rfc1122.html#section-3.3.1.2). Location: Section 3.3.1.2, Gateway Selection. Accessed: 2026-11-10.

> default gateway

**Correspondence:** NIKA replaces the host default gateway. RFC 1122 directs remote traffic through a gateway and specifies default gateway selection when there is no cached route, explaining selective off-subnet failure.

**Evidence limits:** Specific routes can bypass the default gateway. The source does not establish that an arbitrary caller-selected gateway is invalid.

</details>

### `host_incorrect_ip`

Implementation: [`HostIncorrectIP`](../../src/nika/problems/addressing_neighbor_naming/host_configuration.py).

<a id="source-host_incorrect_ip-1"></a>
<details>
<summary>iproute2, ip-address(8) (vendor_documentation; reference_implementation)</summary>

Source: [iproute2, ip-address(8)](https://man7.org/linux/man-pages/man8/ip-address.8.html). Location: DESCRIPTION, local ADDRESS; ip address add/delete. Accessed: 2026-11-10.

> local ADDRESS

**Correspondence:** NIKA replaces eth0 IPv4 address through _inject_ip_change, so the intended address is no longer assigned to that device. ip-address documents address assignment and deletion.

**Evidence limits:** This supports address mutation, not a specific downstream outage. Reachability depends on routes, ARP and the injected address; DHCP persistence must be verified separately.

</details>

### `host_incorrect_netmask`

Implementation: [`HostIncorrectNetmask`](../../src/nika/problems/addressing_neighbor_naming/host_configuration.py).

<a id="source-host_incorrect_netmask-1"></a>
<details>
<summary>RFC 1122, Requirements for Internet Hosts: Communication Layers (rfc; mechanism)</summary>

Source: [RFC 1122, Requirements for Internet Hosts: Communication Layers](https://www.rfc-editor.org/rfc/rfc1122.html#section-3.3.1.1). Location: Section 3.3.1.1, Local/Remote Decision. Accessed: 2026-11-10.

> the destination is on the corresponding connected network

**Correspondence:** NIKA changes eth0 prefix length while preserving its IPv4 address. RFC 1122 uses the address mask to decide whether to send directly or through a gateway, matching the observed wrong on-link decision.

**Evidence limits:** The source establishes the routing decision, not the particular /8 parameter or connectivity outcome in every topology.

</details>

### `host_ip_conflict`

Implementation: [`HostIPConflict`](../../src/nika/problems/addressing_neighbor_naming/host_configuration.py).

<a id="source-host_ip_conflict-1"></a>
<details>
<summary>RFC 5227, IPv4 Address Conflict Detection (rfc; mechanism)</summary>

Source: [RFC 5227, IPv4 Address Conflict Detection](https://www.rfc-editor.org/rfc/rfc5227.html). Location: Abstract; Section 1. Accessed: 2026-11-10.

> two hosts on the same link attempt to use the same IPv4 address

**Correspondence:** HostIPConflict.inject_fault copies the first host's IPv4 address onto the second host, creating the same-link duplicate-address condition described by the RFC.

**Evidence limits:** The RFC supports the conflict mechanism, not a deterministic packet-loss rate. Actual behavior depends on ARP caches and host address-defense behavior.

</details>

### `host_missing_ip`

Implementation: [`HostMissingIP`](../../src/nika/problems/addressing_neighbor_naming/host_configuration.py).

<a id="source-host_missing_ip-1"></a>
<details>
<summary>iproute2, ip-address(8) (vendor_documentation; reference_implementation)</summary>

Source: [iproute2, ip-address(8)](https://man7.org/linux/man-pages/man8/ip-address.8.html). Location: DESCRIPTION; ip address delete. Accessed: 2026-11-10.

> Each device must have at least one address

**Correspondence:** NIKA deletes the recorded IPv4 address from the selected interface using ip addr del, matching the documented address-deletion operation and address requirement.

**Evidence limits:** The fault removes a specific IPv4 address. Other addresses and protocols can remain usable; it is not a physical interface outage.

</details>

### `host_static_blackhole`

Implementation: [`StaticBlackHole`](../../src/nika/problems/forwarding_encapsulation_policy/static_route.py).

<a id="source-host_static_blackhole-1"></a>
<details>
<summary>iproute2, ip-route(8) (vendor_documentation; reference_implementation)</summary>

Source: [iproute2, ip-route(8)](https://man7.org/linux/man-pages/man8/ip-route.8.html). Location: DESCRIPTION, Route types, blackhole. Accessed: 2026-11-10.

> These destinations are unreachable. Packets are discarded silently.

**Correspondence:** NIKA installs ip route replace blackhole for the selected prefix in the tables that route it. The documented blackhole route discards matching packets without ICMP.

**Evidence limits:** This source directly supports the Linux implementation. Native router backends implement equivalent discard routes through their own configuration APIs.

</details>

### `icmp_frag_needed_filter_misconfiguration`

Implementation: [`IcmpFragNeededFilterMisconfiguration`](../../src/nika/problems/forwarding_encapsulation_policy/p4_gateway.py).

<a id="source-icmp_frag_needed_filter_misconfiguration-1"></a>
<details>
<summary>RFC 2923, TCP Problems with Path MTU Discovery (rfc; mechanism)</summary>

Source: [RFC 2923, TCP Problems with Path MTU Discovery](https://www.rfc-editor.org/rfc/rfc2923.html#section-2.1). Location: Section 2.1, Black Hole Detection. Accessed: 2026-11-10.

> Firewalls are often misconfigured to suppress all ICMP messages.

**Correspondence:** NIKA drops ICMP type 3 code 4 on the lower-MTU path. RFC 2923 describes PMTUD failure when fragmentation-needed feedback is filtered: small probes work but large TCP transfers stall.

**Evidence limits:** NIKA targets only fragmentation-needed rather than all ICMP. Its symptom additionally requires a path MTU lower than endpoint packet size.

</details>

### `isis_authentication_mismatch`

Implementation: [`IsisAuthenticationMismatch`](../../src/nika/problems/routing_control_plane/isis.py).

<a id="source-isis_authentication_mismatch-1"></a>
<details>
<summary>RFC 5304, IS-IS Cryptographic Authentication (rfc; mechanism)</summary>

Source: [RFC 5304, IS-IS Cryptographic Authentication](https://www.rfc-editor.org/rfc/rfc5304.html#section-2). Location: Section 2, Authentication Procedures. Accessed: 2026-11-10.

> MUST discard the PDU

**Correspondence:** NIKA enables an MD5 Hello password on one router circuits while neighbors remain unauthenticated. RFC 5304 applies authentication to Hello PDUs and requires rejection of invalid authentication, explaining adjacency loss.

**Evidence limits:** Transition modes that skip verification are outside this correspondence. Password syntax and operational counters differ by platform.

</details>

### `isis_metric_style_mismatch`

Implementation: [`IsisMetricStyleMismatch`](../../src/nika/problems/routing_control_plane/isis.py).

<a id="source-isis_metric_style_mismatch-1"></a>
<details>
<summary>FRRouting 10.5, ISIS (vendor_documentation; mechanism)</summary>

Source: [FRRouting 10.5, ISIS](https://docs.frrouting.org/en/stable-10.5/isisd.html#isis-router). Location: ISIS router, metric-style. Accessed: 2026-11-10.

> Send and accept both styles of TLVs during transition

**Correspondence:** NIKA changes one router from wide to narrow while its neighbors remain wide. The manual distinguishes old/narrow and new/wide TLVs and documents transition as the mode accepting both.

**Evidence limits:** The source supports differing reachability encodings. It does not guarantee route loss on every vendor; SR Linux is excluded because it retains routes despite this change.

</details>

### `isis_router_level_mismatch`

Implementation: [`IsisRouterLevelMismatch`](../../src/nika/problems/routing_control_plane/isis.py).

<a id="source-isis_router_level_mismatch-1"></a>
<details>
<summary>FRRouting 10.5, ISIS (vendor_documentation; reference_implementation)</summary>

Source: [FRRouting 10.5, ISIS](https://docs.frrouting.org/en/stable-10.5/isisd.html). Location: ISIS router, is-type; ISIS interface, isis circuit-type. Accessed: 2026-11-10.

> is-type level-1

**Correspondence:** NIKA changes an IS-IS router from Level-2 to Level-1 while its neighbors remain Level-2. FRR documents router is-type and interface circuit-type as the supported level controls.

**Evidence limits:** The reference supports the configuration operation. The mismatch premise requires no common level with neighbors; changing only the interface type under a Level-2-only instance is not the implemented operation.

</details>

### `k8s_coredns_forward_loop`

Implementation: [`CoreDNSForwardLoop`](../../src/nika/problems/management_orchestration_plane/coredns.py).

<a id="source-k8s_coredns_forward_loop-1"></a>
<details>
<summary>CoreDNS, loop plugin (vendor_documentation; mechanism)</summary>

Source: [CoreDNS, loop plugin](https://coredns.io/plugins/loop/). Location: Examples; Troubleshooting Loops in Kubernetes Clusters. Accessed: 2026-11-10.

> forward . 127.0.0.1

**Correspondence:** The source shows exactly the forward-to-self directive that NIKA writes to the root zone. The loop plugin detects repeated probe queries, halts CoreDNS, and Kubernetes restarts it into CrashLoopBackOff.

**Evidence limits:** The detector only covers simple static loops present at startup and involving HINFO. NIKA restarts CoreDNS to make that premise hold.

</details>

### `k8s_deployment_replicas_zero`

Implementation: [`DeploymentReplicasZero`](../../src/nika/problems/management_orchestration_plane/kubernetes.py).

<a id="source-k8s_deployment_replicas_zero-1"></a>
<details>
<summary>Kubernetes, Deployments (vendor_documentation; mechanism)</summary>

Source: [Kubernetes, Deployments](https://kubernetes.io/docs/concepts/workloads/controllers/deployment/#replicas). Location: Replicas; Scaling a Deployment. Accessed: 2026-11-10.

> number of desired Pods

**Correspondence:** NIKA sets spec.replicas to zero on the Deployment backing an observable Service. Kubernetes defines this field as desired Pod count and reconciles excess Pods away, removing that application backend.

**Evidence limits:** This is a configuration-induced scale-down. Service failure assumes no other matching backend Pods; it is not a container crash.

</details>

### `k8s_ingress_class_mismatch`

Implementation: [`IngressClassMismatch`](../../src/nika/problems/service_networking/kubernetes.py).

<a id="source-k8s_ingress_class_mismatch-1"></a>
<details>
<summary>Kubernetes, Ingress (vendor_documentation; mechanism)</summary>

Source: [Kubernetes, Ingress](https://kubernetes.io/docs/concepts/services-networking/ingress/#ingress-class). Location: Ingress class; Deprecated annotation. Accessed: 2026-11-10.

> reference to an IngressClass resource

**Correspondence:** NIKA changes ingressClassName to a name absent from IngressClass objects. The reference defines that field as selecting the IngressClass and its controller, so the intended controller no longer implements that Ingress.

**Evidence limits:** The source establishes controller selection, not a universal HTTP response. The default 404 is specific to the NIKA ingress-nginx deployment.

</details>

### `k8s_ingress_rate_limit_misconfig`

Implementation: [`IngressRateLimitMisconfig`](../../src/nika/problems/service_networking/ingress.py).

<a id="source-k8s_ingress_rate_limit_misconfig-1"></a>
<details>
<summary>Ingress-Nginx Controller, Annotations (vendor_documentation; symptom)</summary>

Source: [Ingress-Nginx Controller, Annotations](https://kubernetes.github.io/ingress-nginx/user-guide/nginx-configuration/annotations/#rate-limiting). Location: Rate limiting: nginx.ingress.kubernetes.io/limit-rpm. Accessed: 2026-11-10.

> When clients exceed this limit, limit-req-status-code default: 503 is returned.

**Correspondence:** IngressRateLimitMisconfig.inject_fault sets limit-rpm below the declared request rate. The documented minute limit, burst multiplier and default 503 explain the burst rejection symptom.

**Evidence limits:** The source does not specify NIKA’s selected rate, burst test, or rejection threshold. Limits apply per client IP per controller replica.

</details>

### `k8s_networkpolicy_deny`

Implementation: [`NetworkPolicyDeny`](../../src/nika/problems/forwarding_encapsulation_policy/kubernetes_policy.py).

<a id="source-k8s_networkpolicy_deny-1"></a>
<details>
<summary>Kubernetes, Network Policies (vendor_documentation; mechanism)</summary>

Source: [Kubernetes, Network Policies](https://kubernetes.io/docs/concepts/services-networking/network-policies/#default-deny-all-ingress-traffic). Location: Default deny all ingress traffic; Behavior of to and from selectors. Accessed: 2026-11-10.

> does not allow any ingress traffic

**Correspondence:** NIKA applies a NetworkPolicy selecting the victim Pods with policyTypes Ingress and no allow rules. The documented deny-ingress pattern is the same manifest structure with a narrower Pod selector.

**Evidence limits:** Enforcement requires a NetworkPolicy-capable CNI. Policies are additive, so another matching allow policy can defeat a deny-only premise.

</details>

### `k8s_node_firewall_drop`

Implementation: [`NodeFirewallDrop`](../../src/nika/problems/service_networking/kubernetes.py).

<a id="source-k8s_node_firewall_drop-1"></a>
<details>
<summary>Kubernetes, Virtual IPs and Service Proxies (vendor_documentation; mechanism)</summary>

Source: [Kubernetes, Virtual IPs and Service Proxies](https://kubernetes.io/docs/reference/networking/virtual-ips/). Location: Proxy mode: iptables. Accessed: 2026-11-10.

> destination NAT

**Correspondence:** NIKA drops traffic to the ClusterIP before kube-proxy DNAT on one node. The reference explains iptables rules that redirect Service virtual IP traffic to backend endpoints; preempting those rules breaks the VIP path while direct endpoint traffic avoids the VIP match.

**Evidence limits:** NIKA models the failure by packet filtering rather than corrupting kube-proxy rules. The source supports the forwarding dependency, not the exact raw-table injection.

</details>

### `k8s_node_noexecute_taint`

Implementation: [`NodeNoExecuteTaint`](../../src/nika/problems/management_orchestration_plane/kubernetes.py).

<a id="source-k8s_node_noexecute_taint-1"></a>
<details>
<summary>Kubernetes, Taints and Tolerations (vendor_documentation; mechanism)</summary>

Source: [Kubernetes, Taints and Tolerations](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/). Location: NoExecute behavior; Taint based evictions. Accessed: 2026-11-10.

> pods that do not tolerate the taint will be evicted immediately

**Correspondence:** NIKA adds a NoExecute taint to a node hosting a business local volume. The documented effect evicts untolerating Pods and prevents their return, removing that application instance.

**Evidence limits:** The persistent Pending replacement additionally depends on local-volume node affinity; the taint alone could merely reschedule a stateless workload.

</details>

### `k8s_readiness_probe_misconfig`

Implementation: [`ReadinessProbeMisconfig`](../../src/nika/problems/management_orchestration_plane/kubernetes.py).

<a id="source-k8s_readiness_probe_misconfig-1"></a>
<details>
<summary>Kubernetes, Configure Liveness, Readiness and Startup Probes (vendor_documentation; mechanism)</summary>

Source: [Kubernetes, Configure Liveness, Readiness and Startup Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/). Location: Define readiness probes; Configure Probes, readinessProbe. Accessed: 2026-11-10.

> readiness probes

**Correspondence:** NIKA changes the readiness probe port to one with no application listener, drains old replicas, then deploys the changed template. The guide states failed readiness removes service eligibility while the container keeps running.

**Evidence limits:** The source supports readiness behavior. NIKA draining ensures old healthy replicas do not mask the symptom; specific HTTP responses depend on the frontend.

</details>

### `k8s_service_selector_mismatch`

Implementation: [`ServiceSelectorMismatch`](../../src/nika/problems/service_networking/kubernetes.py).

<a id="source-k8s_service_selector_mismatch-1"></a>
<details>
<summary>Kubernetes, Debug Services (vendor_documentation; symptom)</summary>

Source: [Kubernetes, Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/). Location: Does the Service have any EndpointSlices?. Accessed: 2026-11-10.

> If the ENDPOINTS column is &lt;none&gt;

**Correspondence:** The guide instructs checking spec.selector against Pod metadata.labels when endpoints are absent. ServiceSelectorMismatch.inject_fault changes a selector label so it matches no Pods.

**Evidence limits:** The guide supports the empty-endpoint symptom; NIKA-specific HTTP responses and Pod readiness require separate runtime checks.

</details>

### `k8s_service_target_port_mismatch`

Implementation: [`ServiceTargetPortMismatch`](../../src/nika/problems/service_networking/kubernetes.py).

<a id="source-k8s_service_target_port_mismatch-1"></a>
<details>
<summary>Kubernetes, Debug Services (vendor_documentation; mechanism)</summary>

Source: [Kubernetes, Debug Services](https://kubernetes.io/docs/tasks/debug/debug-application/debug-service/). Location: Is the Service defined correctly? (targetPort checklist). Accessed: 2026-11-10.

> Is the targetPort correct for your Pods

**Correspondence:** ServiceTargetPortMismatch.inject_fault changes spec.ports[0].targetPort to a port different from the application port, exactly the configuration checked in this checklist.

**Evidence limits:** The guide establishes the port mismatch diagnostic. NIKA-specific HTTP status codes depend on the deployed proxy and workload.

</details>

### `k8s_vxlan_neighbor_stale`

Implementation: [`VxlanNeighborStale`](../../src/nika/problems/forwarding_encapsulation_policy/kubernetes_overlay.py).

<a id="source-k8s_vxlan_neighbor_stale-1"></a>
<details>
<summary>flannel, VXLAN backend implementation (vendor_documentation; reference_implementation)</summary>

Source: [flannel, VXLAN backend implementation](https://raw.githubusercontent.com/flannel-io/flannel/master/pkg/backend/vxlan/vxlan_network.go). Location: handleSubnetEvents, neighbor and FDB programming. Accessed: 2026-11-10.

> addARP

**Correspondence:** NIKA replaces the overlay neighbor mapping from the peer VTEP address to its MAC on flannel.1 with a wrong MAC. Flannel programs that ARP mapping and a separate FDB mapping to the remote underlay, so an unmatched inner Ethernet destination cannot use the correct remote VTEP forwarding entry.

**Evidence limits:** This is a manually introduced stale mapping analogous to a recreated VTEP, not an experimentally observed flannel restart bug. Underlay and other overlay mappings remain intact.

</details>

### `k8s_worker_apiserver_partition`

Implementation: [`WorkerApiServerPartition`](../../src/nika/problems/management_orchestration_plane/kubernetes.py).

<a id="source-k8s_worker_apiserver_partition-1"></a>
<details>
<summary>Kubernetes, Nodes (vendor_documentation; mechanism)</summary>

Source: [Kubernetes, Nodes](https://kubernetes.io/docs/concepts/architecture/nodes/#node-controller). Location: Node controller; Heartbeats. Accessed: 2026-11-10.

> sets the Ready condition to Unknown

**Correspondence:** NIKA blocks worker TCP access to the API server. The kubelet cannot report status; the node controller marks an unreachable worker Ready=Unknown after its grace interval, matching the NotReady presentation.

**Evidence limits:** The source does not guarantee the NIKA timeout value or immediate Pod termination. Existing worker processes can survive a control-plane partition.

</details>

### `lb_advertisement_missing`

Implementation: [`LoadBalancerAdvertisementMissing`](../../src/nika/problems/service_networking/kubernetes.py).

<a id="source-lb_advertisement_missing-1"></a>
<details>
<summary>MetalLB, Configuration (vendor_documentation; mechanism)</summary>

Source: [MetalLB, Configuration](https://metallb.universe.tf/configuration/). Location: Announce the service IPs; Layer 2 configuration; BGP configuration. Accessed: 2026-11-10.

> must be associated to the IPAddressPool

**Correspondence:** NIKA rewrites ipAddressPools in every BGPAdvertisement and L2Advertisement to an undefined pool. MetalLB documents that allocation and announcement are separate and advertisements must select an address pool, so assigned VIPs lose their announcements.

**Evidence limits:** The source establishes advertisement association. NIKA-specific BGP convergence and ARP observations require live checks; it is not an address-allocation failure.

</details>

### `lb_connection_state_exhaustion`

Implementation: [`LbConnectionStateExhaustion`](../../src/nika/problems/service_networking/gateway.py).

<a id="source-lb_connection_state_exhaustion-1"></a>
<details>
<summary>Miao et al., SilkRoad: Making Stateful Layer-4 Load Balancing Fast and Cheap Using Switching ASICs, SIGCOMM 2017 (paper; mechanism)</summary>

Source: [Miao et al., SilkRoad: Making Stateful Layer-4 Load Balancing Fast and Cheap Using Switching ASICs, SIGCOMM 2017](https://www.cs.yale.edu/homes/yu-minlan/writeup/sigcomm17.pdf). Location: Sections 1 and 4.3, per-connection consistency and unpinned connections. Accessed: 2026-11-10.

> ConnTable is not ready

**Correspondence:** NIKA lowers the connection learner capacity so some live sessions cannot enter the pinning table. SilkRoad shows why unpinned connections following a changed DIP pool lose per-connection consistency, explaining the resulting resets during routine updates.

**Evidence limits:** The paper supports the unpinned-connection update hazard, not a four-entry capacity threshold or a reported connection-table-exhaustion incident. The capacity cap is NIKA reference-model configuration.

</details>

### `lb_pending_connection_update_race`

Implementation: [`LbPendingConnectionUpdateRace`](../../src/nika/problems/service_networking/gateway.py).

<a id="source-lb_pending_connection_update_race-1"></a>
<details>
<summary>Miao et al., SilkRoad: Making Stateful Layer-4 Load Balancing Fast and Cheap Using Switching ASICs, SIGCOMM 2017 (paper; mechanism)</summary>

Source: [Miao et al., SilkRoad: Making Stateful Layer-4 Load Balancing Fast and Cheap Using Switching ASICs, SIGCOMM 2017](https://www.cs.yale.edu/homes/yu-minlan/writeup/sigcomm17.pdf). Location: Section 4.3, pending connections and TransitTable, PDF page 8. Accessed: 2026-11-10.

> have not been inserted in ConnTable

**Correspondence:** SilkRoad explains that a pool update during software connection insertion can map subsequent packets to another DIP. NIKA lengthens the learning delay while ordinary pool updates proceed without TransitTable protection, reproducing that race.

**Evidence limits:** NIKA models the failure that SilkRoad prevents. It does not claim to implement the complete SilkRoad update algorithm or its measured performance.

</details>

### `link_capacity_bottleneck`

Implementation: [`LinkCapacityBottleneck`](../../src/nika/problems/link_interface/link.py).

<a id="source-link_capacity_bottleneck-1"></a>
<details>
<summary>Alizadeh et al., Data Center TCP (DCTCP), SIGCOMM 2010 (paper; mechanism)</summary>

Source: [Alizadeh et al., Data Center TCP (DCTCP), SIGCOMM 2010](https://conferences.sigcomm.org/sigcomm/2010/papers/sigcomm/p63.pdf). Location: Section 2.3.2 (Incast), Figure 6(a) and Figure 7. Accessed: 2026-11-10.

> the request for data synchronizes the workers’ responses

**Correspondence:** The paper describes congestion when synchronized fan-in exceeds available capacity. NIKA shapes the receiving circuit with a controller-side TBF below traffic demand.

**Evidence limits:** The paper studies incast and congestion. NIKA models limited circuit capacity rather than a separate switch-buffer failure.

</details>

<a id="source-link_capacity_bottleneck-2"></a>
<details>
<summary>Phanishayee et al., Measurement and Analysis of TCP Throughput Collapse in Cluster-based Storage Systems, FAST 2008 (paper; mechanism)</summary>

Source: [Phanishayee et al., Measurement and Analysis of TCP Throughput Collapse in Cluster-based Storage Systems, FAST 2008](https://www.cs.cmu.edu/~dga/papers/incast-fast2008.pdf). Location: Abstract; Section 1; Figure 1. Accessed: 2026-11-10.

> overload the switch buffers on the client’s link

**Correspondence:** The paper describes congestion when synchronized fan-in exceeds available capacity. NIKA shapes the receiving circuit with a controller-side TBF below traffic demand.

**Evidence limits:** The paper studies incast and congestion. NIKA models limited circuit capacity rather than a separate switch-buffer failure.

</details>

<a id="source-link_capacity_bottleneck-3"></a>
<details>
<summary>Vasudevan et al., Safe and Effective Fine-grained TCP Retransmissions for Datacenter Communication, SIGCOMM 2009 (paper; mechanism)</summary>

Source: [Vasudevan et al., Safe and Effective Fine-grained TCP Retransmissions for Datacenter Communication, SIGCOMM 2009](https://www.cs.cmu.edu/~dga/papers/incast-sigcomm2009.pdf). Location: Abstract; Section 1. Accessed: 2026-11-10.

> Inbound data overfills small switch buffers

**Correspondence:** The paper describes congestion when synchronized fan-in exceeds available capacity. NIKA shapes the receiving circuit with a controller-side TBF below traffic demand.

**Evidence limits:** The paper studies incast and congestion. NIKA models limited circuit capacity rather than a separate switch-buffer failure.

</details>

<a id="source-link_capacity_bottleneck-4"></a>
<details>
<summary>Chen et al., Understanding TCP Incast Throughput Collapse in Datacenter Networks, WREN 2009 (paper; mechanism)</summary>

Source: [Chen et al., Understanding TCP Incast Throughput Collapse in Datacenter Networks, WREN 2009](https://conferences.sigcomm.org/sigcomm/2009/workshops/wren/papers/p73.pdf). Location: Section 1; Section 3.2; discussion of randomized RTO. Accessed: 2026-11-10.

> switch buffer overflow events will be synchronized for all senders

**Correspondence:** The paper describes congestion when synchronized fan-in exceeds available capacity. NIKA shapes the receiving circuit with a controller-side TBF below traffic demand.

**Evidence limits:** The paper studies incast and congestion. NIKA models limited circuit capacity rather than a separate switch-buffer failure.

</details>

<a id="source-link_capacity_bottleneck-5"></a>
<details>
<summary>iproute2, tc-tbf(8) (vendor_documentation; reference_implementation)</summary>

Source: [iproute2, tc-tbf(8)](https://man7.org/linux/man-pages/man8/tc-tbf.8.html). Location: DESCRIPTION; ALGORITHM; PARAMETERS (rate, burst, limit). Accessed: 2026-11-10.

> ensure that the configured rate is not exceeded

**Correspondence:** NIKA shapes a link through a TBF in the controller-owned proxy or host-side wire. The documented token bucket limits sustained rate and queues packets up to its configured limit, matching low throughput and queue delay under load.

**Evidence limits:** This is a software rate-limit model of an undersized circuit, not evidence of faulty physical media. Queue delay and losses depend on offered load.

</details>

### `link_down`

Implementation: [`LinkFailure`](../../src/nika/problems/link_interface/link.py).

<a id="source-link_down-1"></a>
<details>
<summary>iproute2, ip-link(8) (vendor_documentation; reference_implementation)</summary>

Source: [iproute2, ip-link(8)](https://man7.org/linux/man-pages/man8/ip-link.8.html). Location: ip link set, netns. Accessed: 2026-11-10.

> move the device to the network namespace

**Correspondence:** NIKA moves the selected attachment into a controller-owned network namespace. ip-link documents netns as moving a device to another namespace, exactly the missing-interface operation observed in the node.

**Evidence limits:** The operation models interface removal from a node, not physical unplugging. Native router backends can expose port-down rather than disappearance.

</details>

<a id="source-link_down-2"></a>
<details>
<summary>Linux kernel, Operational States (vendor_documentation; mechanism)</summary>

Source: [Linux kernel, Operational States](https://docs.kernel.org/networking/operstates.html). Location: TLV IFLA_OPERSTATE, IF_OPER_DOWN; driver API. Accessed: 2026-11-10.

> Interface is unable to transfer data on L1

**Correspondence:** NIKA forces carrier/operational down on both logical-link attachments while preserving their administrative intent. The kernel defines operstate Down as inability to transfer L1 data, matching path failure and lost adjacencies.

**Evidence limits:** Emulated carrier loss is not evidence of a particular cable or transceiver defect; native interface and peer operations vary by backend.

</details>

### `link_flap`

Implementation: [`LinkFlap`](../../src/nika/problems/link_interface/link.py).

<a id="source-link_flap-1"></a>
<details>
<summary>Linux kernel, Operational States (vendor_documentation; mechanism)</summary>

Source: [Linux kernel, Operational States](https://docs.kernel.org/networking/operstates.html). Location: Querying from userspace; driver API netif_carrier_on/off. Accessed: 2026-11-10.

> notified of updates while the interface is admin up

**Correspondence:** NIKA Containerlab repeatedly toggles the host-side link so nodes see operational changes and periodic loss. The kernel documents carrier flags and link-state notifications independently of administrative state.

**Evidence limits:** Kathara uses a VDE proxy and models periodic packet loss without carrier events. This reference supports carrier transitions on Containerlab, not a claim that Kathara reproduces physical link flap logs.

</details>

### `link_latency`

Implementation: [`LinkLatency`](../../src/nika/problems/link_interface/latency.py).

<a id="source-link_latency-1"></a>
<details>
<summary>RIPE NCC, A Deep Dive Into the Baltic Sea Cable Cuts (incident_report; symptom)</summary>

Source: [RIPE NCC, A Deep Dive Into the Baltic Sea Cable Cuts](https://labs.ripe.net/author/emileaben/a-deep-dive-into-the-baltic-sea-cable-cuts/). Location: 19 December 2024, No inter-domain and intra-domain changes for 460 anchor pairs. Accessed: 2026-11-15.

> latency increases of 3 ms or more, which indicates circuit-level changes.

**Correspondence:** The report observes increased delay even with unchanged IP paths, consistent with circuit-level rerouting. NIKA adds propagation delay on the external link while preserving node interfaces and routes.

**Evidence limits:** NIKA models the slower circuit after a physical failure. It does not cut fiber, switch protection circuits, simulate optical impairment, or reproduce the incident delay distribution; its default 100 ms makes the link diagnosis measurable.

</details>

### `link_packet_corruption`

Implementation: [`LinkPacketCorruption`](../../src/nika/problems/link_interface/quality.py).

<a id="source-link_packet_corruption-1"></a>
<details>
<summary>iproute2, tc-netem(8) (vendor_documentation; reference_implementation)</summary>

Source: [iproute2, tc-netem(8)](https://man7.org/linux/man-pages/man8/tc-netem.8.html). Location: OPTIONS, corrupt. Accessed: 2026-11-10.

> corrupt

**Correspondence:** NIKA applies netem corrupt to IP frames on the controller-side wire, matching the documented random bit-corruption operation. Non-IP frames are dropped at the same rate to represent receiver rejection.

**Evidence limits:** Software bit corruption is a packet-impairment model. It does not reproduce physical cable BER, FCS counters or electrical faults.

</details>

### `mac_address_conflict`

Implementation: [`MacAddressConflict`](../../src/nika/problems/addressing_neighbor_naming/mac.py).

<a id="source-mac_address_conflict-1"></a>
<details>
<summary>Cisco, Troubleshoot MAC Flaps/Loop on Cisco Catalyst Switches (vendor_documentation; mechanism)</summary>

Source: [Cisco, Troubleshoot MAC Flaps/Loop on Cisco Catalyst Switches](https://www.cisco.com/c/en/us/support/docs/lan-switching/spanning-tree-protocol-stp-8021d/221722-troubleshoot-mac-flaps-loop-on-cisco-cat.html). Location: General Troubleshooting Guidelines, Check for Duplicate MAC Addresses. Accessed: 2026-11-10.

> two devices on your network have the same MAC address

**Correspondence:** NIKA assigns one host the other host eth0 MAC. Cisco identifies duplicate MACs as a cause of MAC flapping and directs inspection of the switching table, matching conflicting learning-bridge entries.

**Evidence limits:** FDB flapping requires a shared learning bridge; fixed P4/OpenFlow forwarding does not gain this behavior merely from duplicated endpoint addresses.

</details>

### `mtu_mismatch`

Implementation: [`MtuMismatch`](../../src/nika/problems/forwarding_encapsulation_policy/mtu.py).

<a id="source-mtu_mismatch-1"></a>
<details>
<summary>RFC 1191, Path MTU Discovery (rfc; mechanism)</summary>

Source: [RFC 1191, Path MTU Discovery](https://www.rfc-editor.org/rfc/rfc1191.html#section-2). Location: Section 2, Protocol overview. Accessed: 2026-11-10.

> too large to be forwarded

**Correspondence:** NIKA lowers one path-facing interface MTU while endpoints retain larger packet sizes. RFC 1191 describes a router unable to forward a DF datagram over a smaller-MTU next hop and sending fragmentation-needed feedback.

**Evidence limits:** With working PMTUD, TCP can adapt. A persistent PMTU black hole requires suppressed feedback, which is the separate ICMP filtering fault.

</details>

### `nat_mapping_removed_without_drain`

Implementation: [`NatMappingRemovedWithoutDrain`](../../src/nika/problems/service_networking/gateway.py).

<a id="source-nat_mapping_removed_without_drain-1"></a>
<details>
<summary>Netfilter, conntrack-tools user manual (vendor_documentation; reference_implementation)</summary>

Source: [Netfilter, conntrack-tools user manual](https://conntrack-tools.netfilter.org/manual.html). Location: Chapter 5, deleting existing flow entries; Chapter 6, state synchronization. Accessed: 2026-11-10.

> delete the existing flow entries

**Correspondence:** NIKA switches SNAT to a second address, removes the old address and deletes its live conntrack entries without waiting for sessions to drain. The tool manual documents deletion of established flow state and stateful traffic implications.

**Evidence limits:** This source supports the exact Linux state-removal operation. Address withdrawal plus lost mapping explains disruption; deleting state alone does not universally force a TCP reset without regard to firewall and NAT behavior.

</details>

### `ospf_acl_block`

Implementation: [`OSPFAclBlock`](../../src/nika/problems/forwarding_encapsulation_policy/acl.py).

<a id="source-ospf_acl_block-1"></a>
<details>
<summary>Netfilter, nft(8) (vendor_documentation; reference_implementation)</summary>

Source: [Netfilter, nft(8)](https://netfilter.org/projects/nftables/manpage.html). Location: IP HEADER EXPRESSIONS (protocol); VERDICT STATEMENTS (drop). Accessed: 2026-11-10.

> drop

**Correspondence:** NIKA installs ip protocol ospf drop on Linux. The manual defines protocol matching and the terminal drop action, which blocks OSPF packets without blocking unrelated IP protocols.

**Evidence limits:** The manual supports Linux packet filtering. The adjacency timeout and native router filter syntax require the protocol and platform references.

</details>

### `ospf_area_misconfiguration`

Implementation: [`OSPFAreaMisconfig`](../../src/nika/problems/routing_control_plane/ospf.py).

<a id="source-ospf_area_misconfiguration-1"></a>
<details>
<summary>RFC 2328, OSPF Version 2 (rfc; mechanism)</summary>

Source: [RFC 2328, OSPF Version 2](https://www.rfc-editor.org/rfc/rfc2328.html#section-8.2). Location: Section 8.2, Receiving protocol packets. Accessed: 2026-11-10.

> Match the Area ID of the receiving interface

**Correspondence:** NIKA moves OSPF network statements or interface bindings to a different area while adjacent routers keep their area. Section 8.2 rejects packets with an incompatible Area ID.

**Evidence limits:** The RFC also permits backbone packets through a matching virtual link; the correspondence concerns ordinary directly connected interfaces.

</details>

### `ospf_area_range_suppression`

Implementation: [`OspfAreaRangeSuppression`](../../src/nika/problems/routing_control_plane/ospf.py).

<a id="source-ospf_area_range_suppression-1"></a>
<details>
<summary>RFC 2328, OSPF Version 2 (rfc; mechanism)</summary>

Source: [RFC 2328, OSPF Version 2](https://www.rfc-editor.org/rfc/rfc2328.html#section-12.4.3). Location: Section 12.4.3, Originating summary-LSAs; Appendix C.2. Accessed: 2026-11-10.

> DoNotAdvertise

**Correspondence:** NIKA sets suppressing ranges on the ABR for a non-backbone area. RFC 2328 states that DoNotAdvertise suppresses Type-3 summary LSAs and hides component networks from other areas.

**Evidence limits:** The source defines intentional suppression. Whether the configuration is erroneous depends on the authored requirement that those remote subnets remain reachable.

</details>

### `ospf_duplicate_router_id`

Implementation: [`OSPFDuplicateRouterId`](../../src/nika/problems/routing_control_plane/ospf.py).

<a id="source-ospf_duplicate_router_id-1"></a>
<details>
<summary>Cisco, Troubleshooting Duplicate Router IDs with OSPF (vendor_documentation; mechanism)</summary>

Source: [Cisco, Troubleshooting Duplicate Router IDs with OSPF](https://www.cisco.com/c/en/us/support/docs/ip/open-shortest-path-first-ospf/23862-duplicate-router-id-ospf.html). Location: Known Issue; Troubleshooting, Single Area Network. Accessed: 2026-11-10.

> duplicate router IDs

**Correspondence:** NIKA copies another router ID in the same area. Cisco documents duplicate IDs, their conflicting LSA identities, and single-area troubleshooting, matching the injector premise and routing-database inconsistency.

**Evidence limits:** Cisco examples do not fix which duplicate router loses routes or require every adjacency to fail. NIKA prefers a nonadjacent duplicate and its timing-dependent outcome must be tested separately.

</details>

### `ospf_hello_interval_mismatch`

Implementation: [`OSPFHelloIntervalMismatch`](../../src/nika/problems/routing_control_plane/ospf.py).

<a id="source-ospf_hello_interval_mismatch-1"></a>
<details>
<summary>RFC 2328, OSPF Version 2 (rfc; mechanism)</summary>

Source: [RFC 2328, OSPF Version 2](https://www.rfc-editor.org/rfc/rfc2328.html#section-10.5). Location: Section 10.5, Receiving Hello Packets. Accessed: 2026-11-10.

> Any mismatch causes processing to stop

**Correspondence:** NIKA verifies matching Hello/dead timers on a Full adjacency, then changes both on one interface. Section 10.5 requires matching HelloInterval and RouterDeadInterval and discards mismatched Hellos.

**Evidence limits:** The source specifies Hello rejection. Loss of application connectivity depends on topology and route alternatives.

</details>

### `ospf_interface_not_enabled`

Implementation: [`OSPFInterfaceNotEnabled`](../../src/nika/problems/routing_control_plane/ospf.py).

<a id="source-ospf_interface_not_enabled-1"></a>
<details>
<summary>RFC 2328, OSPF Version 2 (rfc; mechanism)</summary>

Source: [RFC 2328, OSPF Version 2](https://www.rfc-editor.org/rfc/rfc2328.html#section-10.3). Location: Section 9.2, Interface events; Sections 10.2 and 10.3, InactivityTimer. Accessed: 2026-11-10.

> InactivityTimer

**Correspondence:** NIKA removes the OSPF binding for one previously Full adjacency. The interface stops protocol participation; the peer receives no Hellos and its inactivity event forces the neighbor Down.

**Evidence limits:** This models missing OSPF interface configuration rather than packet filtering. Remaining bindings and physical interfaces stay active.

</details>

### `ospf_network_type_mismatch`

Implementation: [`OSPFNetworkTypeMismatch`](../../src/nika/problems/routing_control_plane/ospf.py).

<a id="source-ospf_network_type_mismatch-1"></a>
<details>
<summary>Cisco, Troubleshoot Open Shortest Path First Route Database Issues (vendor_documentation; mechanism)</summary>

Source: [Cisco, Troubleshoot Open Shortest Path First Route Database Issues](https://www.cisco.com/c/en/us/support/docs/ip/open-shortest-path-first-ospf/7112-26.html). Location: Reason 1: Network Type Mismatch. Accessed: 2026-11-10.

> network type mismatch makes the advertising router unreachable

**Correspondence:** NIKA changes one OSPF interface between broadcast and point-to-point while the peer keeps its type. Cisco presents this exact mismatch with a Full adjacency and missing routing reachability.

**Evidence limits:** This requires matching timers and an implementation that retains Full adjacency. SR Linux is excluded because its type change drops the adjacency instead.

</details>

### `ospf_stub_area_mismatch`

Implementation: [`OspfStubAreaMismatch`](../../src/nika/problems/routing_control_plane/ospf.py).

<a id="source-ospf_stub_area_mismatch-1"></a>
<details>
<summary>RFC 2328, OSPF Version 2 (rfc; mechanism)</summary>

Source: [RFC 2328, OSPF Version 2](https://www.rfc-editor.org/rfc/rfc2328.html#section-10.5). Location: Section 10.5, Receiving Hello Packets; Appendix A.2, E-bit. Accessed: 2026-11-10.

> E-bit

**Correspondence:** NIKA changes one side of a healthy normal area-1 adjacency to stub. The peer keeps external routing capability, so the Hello E-bit differs; Section 10.5 requires matching capability and drops mismatched Hellos.

**Evidence limits:** This evidence applies to a stub/normal disagreement within the same area, not an area-number or Hello-timer mismatch.

</details>

### `overlay_qos_ef_undersized`

Implementation: [`OverlayQosEfUndersized`](../../src/nika/problems/traffic_queueing_resource/overlay_qos.py).

<a id="source-overlay_qos_ef_undersized-1"></a>
<details>
<summary>RFC 3246, An Expedited Forwarding PHB (rfc; mechanism)</summary>

Source: [RFC 3246, An Expedited Forwarding PHB](https://www.rfc-editor.org/rfc/rfc3246.html#section-2.1). Location: Section 2.1, Intuitive Description of EF. Accessed: 2026-11-10.

> service rate of EF packets

**Correspondence:** NIKA reduces the configured EF rate and ceiling to one tenth of the healthy class capacity. RFC 3246 requires EF service rate above EF arrivals for low-delay low-loss service, matching an undersized reserved class under unchanged media load.

**Evidence limits:** The source supports the rate/arrival mechanism, not NIKA packet-loss percentages or a specific vendor scheduler.

</details>

### `p4_action_selector_member_misconfig`

Implementation: [`P4ActionSelectorMemberMisconfig`](../../src/nika/problems/forwarding_encapsulation_policy/p4_runtime.py).

<a id="source-p4_action_selector_member_misconfig-1"></a>
<details>
<summary>P4.org API Working Group, P4Runtime Specification 1.4.1 (vendor_documentation; reference_implementation)</summary>

Source: [P4.org API Working Group, P4Runtime Specification 1.4.1](https://p4lang.github.io/p4runtime/spec/v1.4.1/P4Runtime-Spec.html). Location: Section 9.2.1, Action Profile Member Programming. Accessed: 2026-11-10.

> MODIFY

**Correspondence:** NIKA modifies an existing ActionSelector member action to a bogus output port. The specification binds each member ID to an action and defines MODIFY as replacing that action specification.

**Evidence limits:** The API permits member modification but does not define NIKA port encodings or claim a specific hardware defect.

</details>

### `p4_ecmp_group_member_missing`

Implementation: [`P4EcmpGroupMemberMissing`](../../src/nika/problems/forwarding_encapsulation_policy/p4_runtime.py).

<a id="source-p4_ecmp_group_member_missing-1"></a>
<details>
<summary>P4.org API Working Group, P4Runtime Specification 1.4.1 (vendor_documentation; reference_implementation)</summary>

Source: [P4.org API Working Group, P4Runtime Specification 1.4.1](https://p4lang.github.io/p4runtime/spec/v1.4.1/P4Runtime-Spec.html). Location: Section 9.2.2, Action Profile Group Programming. Accessed: 2026-11-10.

> members

**Correspondence:** NIKA modifies one selector group to omit a previously programmed member. The specification defines group members as the selection set and MODIFY as replacing that set, matching reduced ECMP membership.

**Evidence limits:** A missing member does not necessarily cause packet loss: remaining members can carry traffic. The evidence supports group shrinkage and changed distribution.

</details>

### `p4_ecn_marking_disabled`

Implementation: [`P4EcnMarkingDisabled`](../../src/nika/problems/traffic_queueing_resource/p4_gateway.py).

<a id="source-p4_ecn_marking_disabled-1"></a>
<details>
<summary>Zhu et al., Packet-Level Telemetry in Large Datacenter Networks (Everflow), SIGCOMM 2015 (paper; mechanism)</summary>

Source: [Zhu et al., Packet-Level Telemetry in Large Datacenter Networks (Everflow), SIGCOMM 2015](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/08/everflow-sigcomm15.pdf). Location: Section 7.1, high latency, PDF page 8. Accessed: 2026-11-10.

> misconfigured ECN

**Correspondence:** Everflow reports an ECN threshold misconfiguration allowing excessive queue buildup. NIKA raises the egress ECN marking threshold while preserving traffic and shaping, directly matching delayed marking and queue/latency growth.

**Evidence limits:** The source establishes the operational failure mechanism, not the NIKA threshold value or measured tail-latency increase.

</details>

### `p4_table_entry_misconfig`

Implementation: [`P4TableEntryMisconfig`](../../src/nika/problems/forwarding_encapsulation_policy/p4_pipeline.py).

<a id="source-p4_table_entry_misconfig-1"></a>
<details>
<summary>P4.org API Working Group, P4Runtime Specification 1.4.1 (vendor_documentation; reference_implementation)</summary>

Source: [P4.org API Working Group, P4Runtime Specification 1.4.1](https://p4lang.github.io/p4runtime/spec/v1.4.1/P4Runtime-Spec.html). Location: Sections 9.1 and 9.1.2, TableEntry and Action Specification. Accessed: 2026-11-10.

> action_profile_group_id

**Correspondence:** NIKA rewrites an ipv4_lpm entry to select the wrong next-hop group through P4Runtime. Device reads show the changed forwarding entry while the declared controller intent stays intact.

**Evidence limits:** This is a control-plane configuration error, not corruption beneath the P4Runtime read path.

</details>

<a id="source-p4_table_entry_misconfig-2"></a>
<details>
<summary>Zhu et al., Packet-Level Telemetry in Large Datacenter Networks (Everflow), SIGCOMM 2015 (paper; mechanism)</summary>

Source: [Zhu et al., Packet-Level Telemetry in Large Datacenter Networks (Everflow), SIGCOMM 2015](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/08/everflow-sigcomm15.pdf). Location: Section 7.2.1, Blackhole, PDF pages 8–9. Accessed: 2026-11-10.

> TCAM table was corrupted

**Correspondence:** NIKA rewrites an ipv4_lpm entry to select the wrong next-hop group through P4Runtime. Device reads show the changed forwarding entry while the declared controller intent stays intact.

**Evidence limits:** Everflow reports a hardware table fault. NIKA represents erroneous forwarding state through a P4Runtime configuration change.

</details>

### `p4_table_entry_missing`

Implementation: [`P4TableEntryMissing`](../../src/nika/problems/forwarding_encapsulation_policy/p4_pipeline.py).

<a id="source-p4_table_entry_missing-1"></a>
<details>
<summary>P4.org API Working Group, P4Runtime Specification 1.4.1 (vendor_documentation; reference_implementation)</summary>

Source: [P4.org API Working Group, P4Runtime Specification 1.4.1](https://p4lang.github.io/p4runtime/spec/v1.4.1/P4Runtime-Spec.html). Location: Section 9.1, TableEntry; Section 9.1.3, Default Entry. Accessed: 2026-11-10.

> DELETE

**Correspondence:** NIKA deletes one IPv4 prefix table entry through P4Runtime. The specification describes keyed DELETE and default entry behavior when no ordinary entry matches, matching loss of that forwarding rule.

**Evidence limits:** Deletion can expose another less-specific entry or a forwarding default. Packet loss requires the deployed pipeline behavior; absence alone does not prove a drop.

</details>

### `p4_table_resource_exhaustion`

Implementation: [`P4TableResourceExhaustion`](../../src/nika/problems/forwarding_encapsulation_policy/p4_runtime.py).

<a id="source-p4_table_resource_exhaustion-1"></a>
<details>
<summary>P4.org API Working Group, P4Runtime Specification 1.4.1 (vendor_documentation; reference_implementation)</summary>

Source: [P4.org API Working Group, P4Runtime Specification 1.4.1](https://p4lang.github.io/p4runtime/spec/v1.4.1/P4Runtime-Spec.html). Location: Section 9.1, TableEntry, table size limitation. Accessed: 2026-11-10.

> RESOURCE_EXHAUSTED

**Correspondence:** NIKA fills the prefix table with host routes and attempts to insert an unused diagnostic route. Admission and the Oracle require a device resource-exhaustion response (or BMv2's specific target-add failure), no residual diagnostic entry, and occupancy at the declared size. Recovery removes owned filler routes and verifies a new insertion succeeds.

**Evidence limits:** The specification defines RESOURCE_EXHAUSTED; BMv2 can instead return UNKNOWN with its target-add error. NIKA accepts that specific response only with the full-table and healthy-insertion controls. Existing forwarding can continue. The route population and diagnostic prefix are NIKA choices.

</details>

### `p4runtime_partial_write`

Implementation: [`P4RuntimePartialWrite`](../../src/nika/problems/forwarding_encapsulation_policy/p4_runtime.py).

<a id="source-p4runtime_partial_write-1"></a>
<details>
<summary>P4.org API Working Group, P4Runtime Specification 1.4.1 (vendor_documentation; reference_implementation)</summary>

Source: [P4.org API Working Group, P4Runtime Specification 1.4.1](https://p4lang.github.io/p4runtime/spec/v1.4.1/P4Runtime-Spec.html). Location: Sections 12.1 and 12.2, Batching and Ordering; Sections 12.3 and 9.1. Accessed: 2026-11-10.

> CONTINUE_ON_ERROR

**Correspondence:** NIKA submits successful group MODIFY operations together with erroneous INSERTs for routes that already exist. The specification requires attempting every update in CONTINUE_ON_ERROR and reports individual failures, allowing group updates to survive ALREADY_EXISTS route errors.

**Evidence limits:** The source supports non-atomic partial application; NIKA chooses the particular two-group/two-route batch and validates its observed status codes.

</details>

### `p4runtime_pipeline_mismatch`

Implementation: [`P4RuntimePipelineMismatch`](../../src/nika/problems/forwarding_encapsulation_policy/p4_runtime.py).

<a id="source-p4runtime_pipeline_mismatch-1"></a>
<details>
<summary>P4.org API Working Group, P4Runtime Specification 1.4.1 (vendor_documentation; reference_implementation)</summary>

Source: [P4.org API Working Group, P4Runtime Specification 1.4.1](https://p4lang.github.io/p4runtime/spec/v1.4.1/P4Runtime-Spec.html). Location: Section 14, SetForwardingPipelineConfig RPC. Accessed: 2026-11-10.

> SetForwardingPipelineConfig

**Correspondence:** NIKA compiles a drop-all P4 program and installs it on one switch using SetForwardingPipelineConfig. The specification defines that RPC as configuring a new forwarding pipeline, directly supporting this reference injection operation.

**Evidence limits:** The API does not diagnose the pipeline as wrong. Wrongness is relative to the intended NIKA program; drop-all behavior comes from the injected source.

</details>

### `rib_fib_inconsistency`

Implementation: [`RibFibInconsistency`](../../src/nika/problems/forwarding_encapsulation_policy/rib_fib.py).

<a id="source-rib_fib_inconsistency-1"></a>
<details>
<summary>FRRouting 10.5.3, zebra/rt_netlink.c (vendor_documentation; reference_implementation)</summary>

Source: [FRRouting 10.5.3, zebra/rt_netlink.c](https://raw.githubusercontent.com/FRRouting/frr/frr-10.5.3/zebra/rt_netlink.c). Location: is_selfroute; netlink_route_change_read_unicast, selfroute RTM_NEWROUTE guard. Accessed: 2026-11-10.

> Received that we think we have originated, ignoring

**Correspondence:** NIKA replaces a kernel route next hop while retaining the FRR protocol tag. FRR 10.5.3 classifies that protocol as selfroute and ignores non-startup RTM_NEWROUTE for self routes without ASIC offload, preserving its correct control-plane RIB.

**Evidence limits:** This source supports the exact software RIB/kernel FIB divergence model and its version-dependent persistence. It is not evidence of a physical router PFE installation bug.

</details>

### `routing_service_down`

Implementation: [`RoutingServiceDown`](../../src/nika/problems/routing_control_plane/node_services.py).

<a id="source-routing_service_down-1"></a>
<details>
<summary>FRRouting, WATCHFRR (vendor_documentation; reference_implementation)</summary>

Source: [FRRouting, WATCHFRR](https://docs.frrouting.org/en/latest/watchfrr.html). Location: WATCHFRR; Starting WATCHFRR. Accessed: 2026-11-10.

> handles failed daemon processes and intelligently restarts them

**Correspondence:** On FRR, NIKA kills watchfrr before the routing daemons to prevent automatic restarts. On SONiC, NIKA stops the running routing programs with supervisorctl stop. The FRR manual documents the watchdog restart behavior.

**Evidence limits:** This source supports FRR daemon failure handling and the need to stop watchfrr; it does not document SONiC supervisor control. Existing kernel forwarding entries may persist, so process failure alone does not prove all traffic stops.

</details>

### `sdn_controller_crash`

Implementation: [`SDNControllerCrash`](../../src/nika/problems/management_orchestration_plane/sdn.py).

<a id="source-sdn_controller_crash-1"></a>
<details>
<summary>Open vSwitch, ovs-vswitchd.conf.db(5) (vendor_documentation; mechanism)</summary>

Source: [Open vSwitch, ovs-vswitchd.conf.db(5)](https://www.openvswitch.org/support/dist-docs/ovs-vswitchd.conf.db.5.html). Location: Bridge, fail_mode. Accessed: 2026-11-10.

> no new network connections can be set up

**Correspondence:** NIKA kills the ONOS JVM/Karaf process, severing OpenFlow control. The OVS schema explains controller-managed flow setup and behavior when the connection fails, matching loss of new programming while installed forwarding can survive.

**Evidence limits:** Survival and fallback depend on fail_mode and flow timeouts. Controller failure does not necessarily stop existing traffic or imply a switch dataplane failure.

</details>

### `silent_egress_packet_loss`

Implementation: [`SilentEgressPacketLoss`](../../src/nika/problems/link_interface/p4_gateway.py).

<a id="source-silent_egress_packet_loss-1"></a>
<details>
<summary>Zhu et al., Packet-Level Telemetry in Large Datacenter Networks (Everflow), SIGCOMM 2015 (paper; mechanism)</summary>

Source: [Zhu et al., Packet-Level Telemetry in Large Datacenter Networks (Everflow), SIGCOMM 2015](https://www.microsoft.com/en-us/research/wp-content/uploads/2016/08/everflow-sigcomm15.pdf). Location: Section 7.2.2, Silent packet drop, PDF page 9. Accessed: 2026-11-10.

> faulty interface

**Correspondence:** Everflow identifies one interface silently dropping packets at random despite ordinary counters. NIKA drops a fraction of frames after the switch egress count on one transmit direction, preserving link and programmed state.

**Evidence limits:** The source establishes random interface loss and retransmissions. NIKA chooses a unidirectional external proxy model; the source does not identify an optics, SerDes or MAC root cause.

</details>

### `snat_port_pool_exhaustion`

Implementation: [`SnatPortPoolExhaustion`](../../src/nika/problems/service_networking/gateway.py).

<a id="source-snat_port_pool_exhaustion-1"></a>
<details>
<summary>Netfilter, NAT HOWTO (vendor_documentation; mechanism)</summary>

Source: [Netfilter, NAT HOWTO](https://netfilter.org/documentation/HOWTO/NAT-HOWTO-6.html). Location: Source NAT; What Happens When NAT Fails. Accessed: 2026-11-10.

> no way to uniquely map a connection

**Correspondence:** NIKA restricts SNAT to a small explicit source-port range used by normal client sessions. Netfilter documents port ranges and dropping a connection when no unique translation satisfying the rule is available, matching exhaustion and SYN retries.

**Evidence limits:** The HOWTO uses iptables syntax; NIKA uses equivalent nftables/native SNAT configuration. Capacity is per relevant NAT tuple constraints, not a universal count of all simultaneous connections.

</details>

### `southbound_port_block`

Implementation: [`SouthboundPortBlock`](../../src/nika/problems/management_orchestration_plane/sdn.py).

<a id="source-southbound_port_block-1"></a>
<details>
<summary>Netfilter, nft(8) (vendor_documentation; reference_implementation)</summary>

Source: [Netfilter, nft(8)](https://netfilter.org/projects/nftables/manpage.html). Location: TCP HEADER EXPRESSION (dport); VERDICT STATEMENTS (drop). Accessed: 2026-11-10.

> drop

**Correspondence:** NIKA drops the configured OpenFlow TCP destination port in hooked chains while ONOS still listens. The manual defines port selection and unconditional drop, directly supporting a transport black hole distinct from a stopped listener.

**Evidence limits:** The source supports Linux filtering, not a universal OpenFlow port number. Switch disconnection depends on the configured port and controller liveness timers.

</details>

### `southbound_port_mismatch`

Implementation: [`SouthboundPortMismatch`](../../src/nika/problems/management_orchestration_plane/sdn.py).

<a id="source-southbound_port_mismatch-1"></a>
<details>
<summary>ONOS 2.7.0, OpenFlowControllerImpl.java (vendor_documentation; reference_implementation)</summary>

Source: [ONOS 2.7.0, OpenFlowControllerImpl.java](https://raw.githubusercontent.com/opennetworkinglab/onos/2.7.0/protocols/openflow/ctl/src/main/java/org/onosproject/openflow/controller/impl/OpenFlowControllerImpl.java). Location: openflowPorts property; modified/readComponentConfiguration. Accessed: 2026-11-10.

> Port numbers (comma separated) used by OpenFlow protocol

**Correspondence:** NIKA changes the ONOS openflowPorts property to a different listening port while OVS keeps the original target. The public controller implementation parses that property and passes its port list to the controller listener.

**Evidence limits:** The source supports listener configuration, not an arbitrary firewall failure. Connection-refused behavior requires the original port to have no listener.

</details>

### `switch_datapath_id_conflict`

Implementation: [`SwitchDatapathIdConflict`](../../src/nika/problems/management_orchestration_plane/sdn.py).

<a id="source-switch_datapath_id_conflict-1"></a>
<details>
<summary>ONOS 2.7.0, OFChannelHandler.java (vendor_documentation; mechanism)</summary>

Source: [ONOS 2.7.0, OFChannelHandler.java](https://raw.githubusercontent.com/opennetworkinglab/onos/2.7.0/protocols/openflow/ctl/src/main/java/org/onosproject/openflow/controller/impl/OFChannelHandler.java). Location: ChannelState ACTIVE, duplicate datapath handling. Accessed: 2026-11-10.

> Duplicated dpid

**Correspondence:** NIKA assigns one OVS bridge the datapath ID of another. ONOS detects an already-connected datapath ID, logs Duplicated dpid and rejects the duplicate channel, matching repeated reconnection while existing local flows remain.

**Evidence limits:** This is ONOS-specific duplicate-identity handling. The source does not guarantee that every OpenFlow controller rejects the later channel in the same way.

</details>

### `tcp_receive_window_limited`

Implementation: [`TcpReceiveWindowLimited`](../../src/nika/problems/traffic_queueing_resource/tcp_receive_window.py).

<a id="source-tcp_receive_window_limited-1"></a>
<details>
<summary>Linux kernel, IP Sysctl (vendor_documentation; mechanism)</summary>

Source: [Linux kernel, IP Sysctl](https://docs.kernel.org/networking/ip-sysctl.html). Location: tcp_moderate_rcvbuf; tcp_rmem. Accessed: 2026-11-10.

> receive buffer auto-tuning

**Correspondence:** NIKA disables tcp_moderate_rcvbuf and limits tcp_rmem below measured path BDP. The kernel documents receive-buffer auto-tuning up to tcp_rmem limits, directly supporting receiver-buffer restriction as distinct from path rate shaping.

**Evidence limits:** This source supports the sysctl mechanism. Throughput reduction depends on RTT, receive-window advertisement, socket behavior and the selected buffer; it does not establish NIKA acceptance ratios.

</details>

### `tcp_syn_flood_attack`

Implementation: [`TcpSynFloodAttack`](../../src/nika/problems/security/tcp_syn_flood.py).

<a id="source-tcp_syn_flood_attack-1"></a>
<details>
<summary>RFC 4987, TCP SYN Flooding Attacks and Common Mitigations (rfc; mechanism)</summary>

Source: [RFC 4987, TCP SYN Flooding Attacks and Common Mitigations](https://www.rfc-editor.org/rfc/rfc4987.html#section-2.2). Location: Section 2.2, Theory of Operation. Accessed: 2026-11-10.

> backlog full of bogus half-opened connections

**Correspondence:** NIKA sends repeated SYN-only 5-tuples without completing legitimate handshakes. RFC 4987 describes SYN barrages retaining half-open state and excluding valid connection requests, matching the attack mechanism.

**Evidence limits:** The RFC does not validate the NIKA packet rate, number of tuples or Bloom-filter behavior. Success depends on backlog policy and SYN-cookie defenses.

</details>

### `vrf_dscp_remarking`

Implementation: [`VrfDscpRemarking`](../../src/nika/problems/forwarding_encapsulation_policy/vrf_dscp.py).

<a id="source-vrf_dscp_remarking-1"></a>
<details>
<summary>RFC 2474, Definition of the Differentiated Services Field (rfc; mechanism)</summary>

Source: [RFC 2474, Definition of the Differentiated Services Field](https://www.rfc-editor.org/rfc/rfc2474.html#section-3). Location: Section 3, DS field definition; Section 4.1, Default PHB. Accessed: 2026-11-10.

> 000000

**Correspondence:** NIKA rewrites EF-marked business packets from DSCP 46 to 0 before the overlay egress scheduler. RFC 2474 uses the codepoint to select forwarding behavior and defines 000000 as the default PHB, explaining reclassification into best effort.

**Evidence limits:** The RFC does not guarantee the configured queue mapping or resulting loss. NIKA intentionally places competing best-effort traffic on that egress.

</details>

### `vrf_route_target_mismatch`

Implementation: [`VrfRouteTargetMismatch`](../../src/nika/problems/routing_control_plane/vrf.py).

<a id="source-vrf_route_target_mismatch-1"></a>
<details>
<summary>RFC 4364, BGP/MPLS IP Virtual Private Networks (rfc; mechanism)</summary>

Source: [RFC 4364, BGP/MPLS IP Virtual Private Networks](https://www.rfc-editor.org/rfc/rfc4364.html#section-4.3.1). Location: Section 4.3.1, The Route Target Attribute; Section 4.3.6, local VRF distribution. Accessed: 2026-11-10.

> Import Target

**Correspondence:** NIKA replaces a business VRF import with a mistyped RT on RouterOS or a wrong source VRF on FRR. RFC 4364 ties route import eligibility to matching Route Target attributes, including distribution between VRFs on the same PE.

**Evidence limits:** FRR derives RTs from import vrf rather than editing an explicit RT string. The RFC supports import isolation but not the exact NIKA typo or any claimed operator incident.

</details>

<a id="source-vrf_route_target_mismatch-2"></a>
<details>
<summary>FRRouting, BGP Route Leaking (vendor_documentation; reference_implementation)</summary>

Source: [FRRouting, BGP Route Leaking](https://docs.frrouting.org/en/latest/bgp.html#clicmd-import-vrf-VRFNAME). Location: import vrf VRFNAME. Accessed: 2026-11-10.

> automatic leaking from vrf VRFNAME

**Correspondence:** NIKA replaces import vrf default with import vrf vrf_guest for the affected business VRF. The command imports routes from the named source VRF and derives RD/RT automatically, directly supporting the wrong-source implementation.

**Evidence limits:** This is a wrong source-VRF configuration on FRR. It should not be described as a literal explicit RT typo on that platform.

</details>

### `web_dos_attack`

Implementation: [`WebDoS`](../../src/nika/problems/security/web.py).

<a id="source-web_dos_attack-1"></a>
<details>
<summary>cPanel, How to Mitigate Slowloris Attacks (vendor_documentation; mechanism)</summary>

Source: [cPanel, How to Mitigate Slowloris Attacks](https://docs.cpanel.net/knowledge-base/security/how-to-mitigate-slowloris-attacks/). Location: Overview. Accessed: 2026-11-10.

> holds those connections open for as long as possible

**Correspondence:** NIKA creates many incomplete slow-header connections against the existing web service. The vendor describes holding open connections until finite server connection capacity prevents other clients from being served.

**Evidence limits:** NIKA also runs ApacheBench request workers; the source supports the slow-connection component, not a universal claim that each server or configuration becomes unavailable.

</details>

### `wireguard_allowed_ips_misconfiguration`

Implementation: [`WireGuardAllowedIpsMisconfiguration`](../../src/nika/problems/forwarding_encapsulation_policy/wireguard.py).

<a id="source-wireguard_allowed_ips_misconfiguration-1"></a>
<details>
<summary>WireGuard, Cryptokey Routing (vendor_documentation; mechanism)</summary>

Source: [WireGuard, Cryptokey Routing](https://www.wireguard.com/#cryptokey-routing). Location: Cryptokey Routing, outgoing peer selection. Accessed: 2026-11-10.

> compares it to each peer&#x27;s list of allowed IPs

**Correspondence:** NIKA removes one business prefix from the selected peer AllowedIPs while retaining the tunnel address and BGP route. WireGuard documents outgoing destination lookup in AllowedIPs, so the missing prefix cannot select that peer despite an ordinary route through the interface.

**Evidence limits:** AllowedIPs is separate from the Linux route table. The source supports peer selection, not a guarantee that a handshake or alternate route survives every configuration.

</details>

### `wireguard_peer_key_misconfiguration`

Implementation: [`WireGuardPeerKeyMisconfiguration`](../../src/nika/problems/forwarding_encapsulation_policy/wireguard.py).

<a id="source-wireguard_peer_key_misconfiguration-1"></a>
<details>
<summary>WireGuard, Conceptual Overview (vendor_documentation; mechanism)</summary>

Source: [WireGuard, Conceptual Overview](https://www.wireguard.com/#conceptual-overview). Location: Conceptual Overview; Simple Network Interface. Accessed: 2026-11-10.

> both parties have each other&#x27;s public keys

**Correspondence:** NIKA replaces the hub peer public key with a different key. WireGuard requires each party to use the other public key for encrypted and authenticated exchange, so the configured identity cannot authenticate the intended hub.

**Evidence limits:** The source supports the key identity requirement, not a full primary/backup failure scenario. A wrong key on a standby tunnel can remain latent while another tunnel carries traffic.

</details>

<!-- failure-sources:end -->

### Maintain and export sources

Edit [failure-sources.json](failure-sources.json), then update this reference or export a CSV for the paper appendix:

```shell
uv run python scripts/failure_sources.py --require-complete --write-docs
uv run python scripts/failure_sources.py --require-complete --format csv > /tmp/failure-sources.csv
```

Each record identifies the implementation path and class. Each citation includes its source type, direct URL, locator, short excerpt, supported mechanism/implementation/symptom, correspondence, evidence limits, and inspection date. Use `incident_report`, `operator_mailing_list`, `vendor_documentation`, `rfc`, or `paper`. Project documentation, reference code, and non-RFC implementation specifications use `vendor_documentation`. Operational case studies use `incident_report`; papers containing case studies remain `paper`.

Read the source and implementation together. A shared protocol name does not establish correspondence. Record differences in deployment and workload, and leave unsupported claims pending. The script checks registered IDs, class paths, duplicate URLs, and coverage; a reviewer checks whether the cited passage supports the recorded claim.

The source data stays outside learner-visible problem metadata.
