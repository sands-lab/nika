# Failure taxonomy and reference

This reference serves benchmark authors and paper reviewers who analyze NIKA coverage. NIKA assigns each registered failure to one network subsystem and records cause, symptom, scope, temporal behavior, and impact as separate metadata. The registry keeps `root_cause_name` as the stable failure ID.

Run these commands against the installed checkout:

```shell
uv run nika failure list
uv run nika failure describe <failure_id>
```

## Taxonomy model

The `failure_domain` identifies the subsystem where the injected mechanism acts. Configuration and attack properties belong in `cause`, so an adversarial BGP injection remains in Routing & Control Plane and records `cause: adversarial`.

| Axis | Accepted values | Interpretation |
| --- | --- | --- |
| `failure_domain` | `link_interface`, `routing_control_plane`, `forwarding_encapsulation_policy`, `service_networking`, `management_orchestration_plane`, `addressing_neighbor_naming`, `endpoint_application`, `traffic_queueing_resource` | Network subsystem that contains the failed mechanism. |
| `cause` | `configuration`, `hardware`, `software`, `resource`, `operational`, `adversarial` | Root-cause mechanism. |
| `symptom` | `down`, `flap`, `loss`, `latency`, `blackhole`, `loop`, `corruption`, `degraded_throughput`, `misrouting` | Primary observable effect. |
| `scope` | `host`, `link`, `node`, `path`, `service`, `multi_node` | Smallest useful extent for coverage analysis. |
| `temporal` | `persistent`, `transient`, `intermittent` | Behavior after injection. |
| `impact` | `none`, `partial`, `complete` | Severity within the declared scope. |

`ground_truth.json` schema version 3 writes all six fields. `root_cause_category` remains a compatibility alias of `failure_domain`; NIKA excludes taxonomy metadata from the scored `(resource_id, fault_type)` key.

## Domain boundaries

| Domain | Boundary | Registered failures | Working-matrix cases |
| --- | --- | ---: | ---: |
| Link & Interface | Link attachment, interface state, flapping, and packet corruption. | 4 | 92 |
| Routing & Control Plane | BGP and OSPF route computation, advertisements, adjacency, and routing-daemon availability. | 9 | 62 |
| Forwarding, Encapsulation & Policy | ACLs, static forwarding, SDN rules, P4 pipelines, MTU behavior, WireGuard encapsulation, and Kubernetes NetworkPolicy. | 26 | 170 |
| Service Networking | Kubernetes ClusterIP forwarding and load-balancer operation. | 2 | 5 |
| Management & Orchestration Plane | Kubernetes API-server reachability, SDN controller availability, and controller southbound management channels. | 4 | 11 |
| Addressing, Neighbor & Naming | IP and MAC assignment, ARP, DHCP, DNS data, DNS reachability, and name resolution. | 17 | 168 |
| Endpoint & Application | Host availability, endpoint resource contention, application delay, and application-layer denial of service. | 5 | 85 |
| Traffic, Queueing & Resource | Offered load, queueing, and link-capacity constraints. | 2 | 39 |
| **Total** |  | **69** | **632** |

The domains follow the failed network mechanism. The orthogonal metadata captures configuration, resource exhaustion, and adversarial action without creating overlapping top-level groups. This split supports coverage comparisons across subsystem, cause, symptom, and scope.

## Compatibility sets

The scenario labels below reproduce tag-subset matching in [`benchmark/generate_benchmark.py`](../benchmark/generate_benchmark.py). A tag match means that the scenario exposes the required device family. Injection still requires a suitable target inside the selected scenario.

| Set | Scenario IDs |
| --- | --- |
| **All** | All registered scenarios |
| **Host L2** | `dc_clos`, `campus_lan`, `enterprise_branch`, `sdn_l3_clos`, `p4_bloom_filter`, `p4_int`, `p4_mpls`, `p4_dc_fabric`, `simple_bgp`, `k8s_lab`, `llmd_lab` |
| **Routed host** | `dc_clos`, `campus_lan`, `enterprise_branch`, `simple_bgp`, `k8s_lab` |
| **FRR** | `dc_clos`, `campus_lan`, `enterprise_branch`, `simple_bgp`, `isp`, `k8s_lab` |
| **BGP** | `dc_clos`, `enterprise_branch`, `simple_bgp`, `isp`, `min3clos`, `k8s_lab` |
| **RPKI** | `isp` (Abilene eBGP + Routinator) |
| **OSPF** | `campus_lan`, `isp` |
| **DNS** | `dc_clos` service workload and `campus_lan` DHCP workload |
| **HTTP** | `dc_clos` service workload, `campus_lan`, `enterprise_branch`, `sdn_l3_clos`, `p4_dc_fabric`, and `llmd_lab` |
| **DHCP** | `campus_lan` DHCP workload |
| **SDN** | `sdn_l3_clos` |
| **P4** | `p4_bloom_filter`, `p4_int`, `p4_mpls`, `p4_dc_fabric` |
| **Kubernetes** | `k8s_lab`, `llmd_lab` |
| **VPN** | `enterprise_branch` |

For `isp`, OSPF failures require `--igp ospf`; BGP failures require `--bgp-mode ibgp_rr` or `ebgp`. `bgp_rpki_invalid_route_leak` and `bgp_max_prefix_exceeded` additionally require `--topo abilene --bgp-mode ebgp` (Kathara + FRR; RPKI also needs Routinator). Generic link failures work with either ISP backend. SR Linux support exists only where the failure implementation contains a Containerlab branch.

## Registered failures

Each table preserves the injection and verification contract. The taxonomy column lists `cause / symptom / scope / temporal / impact`.

### Link & Interface

| Root cause | Failure ID | Taxonomy metadata | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- | --- |
| Link detached | `link_detach` | `operational` / `down` / `link` / `persistent` / `complete` | **All**. `host_name`, `intf_name=eth0` | Removes the interface from the running node through the backend runtime. | Traffic using the attachment fails and the interface disappears from inventory. | The interface no longer exists. |
| Link down | `link_down` | `hardware` / `down` / `link` / `persistent` / `complete` | **All**. `host_name`, `intf_name=eth0` | Sets the selected interface down. | Traffic crossing the interface loses reachability; routing adjacencies on it drop. | Interface `operstate` is `down`. |
| Link flap | `link_flap` | `hardware` / `flap` / `link` / `intermittent` / `partial` | **All**. `host_name`, `intf_name=eth0`, `down_time=1`, `up_time=1` | Starts a loop that alternates the interface between down and up. | Sustained ping or protocol sessions show periodic loss and adjacency churn. | The recorded flap process and PID remain alive. |
| Faulty cable | `link_high_packet_corruption` | `hardware` / `corruption` / `link` / `persistent` / `partial` | **All**. `host_name`, `corruption_percentage=60` | Applies `tc netem corrupt` to the target's last interface, simulating a cable that corrupts frames. | Packet streams across the interface show checksum failures, loss, and retransmissions. | `tc qdisc` reports the configured corruption rate. |

### Routing & Control Plane

| Root cause | Failure ID | Taxonomy metadata | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- | --- |
| BGP ASN mismatch | `bgp_asn_misconfig` | `configuration` / `loss` / `path` / `persistent` / `partial` | **BGP**. `host_name` | Changes the local ASN in FRR or SR Linux configuration. | Peers reject or reset sessions because the configured remote AS no longer matches. | Running configuration contains the changed ASN. |
| BGP maximum-prefix exceeded | `bgp_max_prefix_exceeded` | `operational` / `down` / `multi_node` / `persistent` / `partial` | **RPKI-style ISP eBGP** (`TAGS` require `bgp`+`isp`; `isp` Abilene eBGP). `receiver_name`, `peer_name`, optional `neighbor_ip` / `maximum_prefix` / `flood_count` | On the receiver, configures a low `maximum-prefix` for the chosen eBGP neighbor, temporarily permits a `198.19.0.0/16` flood space, then has the peer advertise many `/24` test prefixes so received count exceeds the limit. Does not shut the session directly. Inspired by the [Optus 2023 BGP maximum-prefix outage discussion](https://blog.apnic.net/2023/11/23/call-the-routing-police/). Kathara+FRR only. | FRR tears down the eBGP session; business routes from the peer may withdraw; reachability across the session drops. | Session not Established; neighbor/log evidence of maximum-prefix. GT labels both `node/{receiver}` and `node/{peer}`. |
| BGP blackhole route leak | `bgp_blackhole_route_leak` | `configuration` / `blackhole` / `multi_node` / `persistent` / `partial` | **BGP**. `host_name` | Resolves a victim `/30`, installs a Null0 or blackhole route, and advertises it through BGP. | Traffic to the more-specific victim network follows the leaked route and drops. | The blackhole route or its advertisement exists. |
| RPKI-invalid BGP route leak | `bgp_rpki_invalid_route_leak` | `configuration` / `misrouting` / `multi_node` / `persistent` / `partial` | **RPKI** (`TAGS` require `bgp`+`rpki`; `isp` Abilene eBGP). `host_name` (leaker) | On Abilene eBGP with the fixed inter-AS/RPKI profile, changes the leaker AS export policy so VRP-unauthorized prefixes are advertised with the leaker origin ASN. Requires Kathara+FRR+Routinator. Inspired by [APNIC analysis of a route leak](https://blog.apnic.net/2025/05/06/analysis-of-a-route-leak/). | Non-ROV observers learn Invalid-origin routes; ROV observers reject them; sessions stay Established. | Leaker advertises the leak prefixes; non-ROV RIB contains them with leaker origin; ROV RIB does not accept them as best. |
| Missing BGP advertisement | `bgp_missing_route_advertisement` | `configuration` / `blackhole` / `path` / `persistent` / `partial` | **BGP**. `host_name` | Removes an FRR network advertisement or applies an SR Linux export policy that withdraws it. | Peers stay reachable while they lose the selected prefix. | FRR configuration lacks the advertisement or SR Linux applies the withdrawal policy. |
| Switch/router crash | `frr_service_down` | `software` / `down` / `node` / `persistent` / `complete` | **FRR**. `host_name`, `service_name=frr` | Stops FRR on a router. | Dynamic adjacencies and routes disappear while connected forwarding can remain. | Zebra is absent and FRR routing commands are unavailable. |
| OSPF area misconfiguration | `ospf_area_misconfiguration` | `configuration` / `loss` / `multi_node` / `persistent` / `partial` | **OSPF**. `host_name` | Changes an OSPF network statement to a mismatched area and restarts FRR. | Adjacency fails on links whose endpoints no longer agree on area. | Both file and running configuration show the changed area. |
| OSPF neighbor missing | `ospf_neighbor_missing` | `configuration` / `down` / `link` / `persistent` / `complete` | **OSPF**. `host_name` | Comments OSPF network statements in `frr.conf` and removes them from the daemon. | The router stops forming expected OSPF adjacencies and loses learned routes. | File statements are commented and the daemon has no active network statements. |
| BGP hijacking | `bgp_hijacking` | `adversarial` / `misrouting` / `multi_node` / `persistent` / `partial` | **BGP**. `host_name`, `target_network` optional | Makes FRR or SR Linux originate an unauthorized prefix; FRR also installs it on loopback. | BGP peers select the injected route when policy and prefix selection permit it. | The router advertises the prefix; FRR also has the loopback address. |

### Forwarding, Encapsulation & Policy

| Root cause | Failure ID | Taxonomy metadata | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- | --- |
| Path MTU misconfiguration | `mtu_mismatch` | `configuration` / `loss` / `path` / `persistent` / `partial` | **All**. `host_name`, `mtu=100`. Legacy id `link_fragmentation_disabled` resolves to this failure. | Adds an iptables OUTPUT length-based DROP for packets with length >= `mtu` (lab stand-in for an undersized path MTU; does not rewrite interface MTU or PMTUD). | Large packets drop while smaller packets can pass, producing size-dependent loss. | The exact length/DROP rule appears in iptables. |
| ARP ACL block | `arp_acl_block` | `configuration` / `blackhole` / `link` / `persistent` / `partial` | **Host L2**. `host_name` | Adds an nftables ARP drop and flushes the neighbor cache. | New ARP resolution fails; cached entries are removed so local-subnet traffic stops. | An ARP drop appears in nftables. |
| Routing control-plane ACL block | `bgp_acl_block` | `configuration` / `blackhole` / `path` / `persistent` / `complete` | **BGP**. `host_name` | Kathara adds nftables drops for TCP source and destination port 179; Containerlab installs the SR Linux ACL equivalent. | BGP sessions reset or cannot establish; routes learned through those sessions disappear. | The port-179 drop exists in nftables or the SR Linux ACL. |
| Switch/router crash | `bmv2_switch_down` | `software` / `down` / `node` / `persistent` / `complete` | **P4**. `host_name` | Kills `simple_switch` on a BMv2 node. | Every path through that switch fails and the P4 control CLI cannot reach the process. | No `simple_switch` process is running. |
| DNS empty answer | `dns_port_blocked` | `configuration` / `blackhole` / `service` / `persistent` / `complete` | **DNS**. `host_name` | Adds nftables drops for TCP and UDP port 53, simulating a resolver that returns no usable answer from the client's perspective. | DNS queries time out while the BIND process and direct IP connectivity remain healthy. | Both DNS port rules appear in nftables. |
| Flow rule loop | `flow_rule_loop` | `configuration` / `loop` / `path` / `persistent` / `complete` | **SDN**. `host_name`, `host_name_2` | Configures each selected switch to emit matching packets through their ingress-facing port. | Traffic on the affected attachments reflects toward its incoming link and can loop with the adjacent path. | Both switches contain an `in_port` rule with an output action. |
| Flow rule shadowing | `flow_rule_shadowing` | `configuration` / `blackhole` / `path` / `persistent` / `partial` | **SDN**. `host_name` | Installs a high-priority OVS drop that takes precedence over normal forwarding. | Matching traffic fails even though lower-priority learning-switch flows exist. | OVS reports the high-priority drop flow. |
| Host static blackhole | `host_static_blackhole` | `configuration` / `blackhole` / `path` / `persistent` / `complete` | **BGP**. `host_name` | Installs a static blackhole route for a resolved victim network without advertising the route. | Traffic matching the victim prefix drops at the target router. | The target running configuration contains the blackhole route. |
| HTTP ACL block | `http_acl_block` | `configuration` / `blackhole` / `service` / `persistent` / `complete` | **HTTP**. `host_name` | Adds nftables drops for TCP port 80. | HTTP requests to or from the target time out while non-HTTP traffic can pass. | The ruleset contains the port-80 drop. |
| ICMP ACL block | `icmp_acl_block` | `configuration` / `blackhole` / `path` / `persistent` / `partial` | `dc_clos`, `campus_lan`, `enterprise_branch`, `sdn_l3_clos`, **P4**, `simple_bgp`, `isp`, **Kubernetes**. `host_name` | Adds an nftables ICMP drop. | Ping and ICMP-based health checks fail while TCP or UDP services can remain reachable. | The ruleset contains an ICMP drop. |
| Kubernetes policy deny | `k8s_networkpolicy_deny` | `configuration` / `blackhole` / `service` / `persistent` / `partial` | **Kubernetes**. `control_node` optional, `namespace`, `pod_selector`, `policy_name`, `symptom_host`, `symptom_url`, `control_url` | Applies a deny-all-ingress `NetworkPolicy` to selected pods. | The selected route fails while a caller-supplied sibling route stays healthy; target pods remain Ready. | Policy exists, selected pods are Ready, the symptom URL fails, and the control URL succeeds. |
| Number of MPLS labels hit limit | `mpls_label_limit_exceeded` | `resource` / `loss` / `path` / `persistent` / `partial` | `p4_mpls`. `host_name` | Changes the program's `CONST_MAX_LABELS` limit to 2 and restarts compilation. | MPLS behavior needing more labels fails or compilation output disappears. | Source contains the limit 2; details also report whether JSON exists. |
| Routing control-plane ACL block | `ospf_acl_block` | `configuration` / `blackhole` / `path` / `persistent` / `complete` | **OSPF**. `host_name` | Adds an nftables rule that drops IP protocol 89. | OSPF neighbors time out and learned routes withdraw while unrelated protocols can pass. | The nftables ruleset contains the OSPF drop. |
| Misaligned sketch thresholds | `p4_aggressive_detection_thresholds` | `configuration` / `loss` / `node` / `persistent` / `partial` | `p4_bloom_filter`. `host_name`, `p4_name` optional | Changes `PACKET_THRESHOLD` from 1000 to 100, removes JSON, and restarts the switch. | A TCP flow reaches the drop threshold ten times sooner than designed, causing false-positive detection and drops. | P4 source contains `PACKET_THRESHOLD 100`. |
| P4 program reads invalid header field | `p4_compilation_error_parser_state` | `software` / `down` / `node` / `persistent` / `complete` | `p4_bloom_filter`, `p4_int`, `p4_mpls`. `host_name`, `p4_name` optional | Rewrites `state` as invalid `states` syntax, removes JSON, and restarts. | The compiler or switch startup fails. | Compiled JSON is missing or `simple_switch` is not running. |
| P4 program reads invalid header field | `p4_header_definition_error` | `software` / `down` / `node` / `persistent` / `complete` | `p4_bloom_filter`, `p4_int`, `p4_mpls`. `host_name`, `p4_name` optional | Duplicates an Ethernet type field in the P4 header, removes JSON, and restarts BMv2 compilation. | Compilation fails or the switch cannot start; dependent paths fail. | Compiled JSON is missing or `simple_switch` is not running. |
| Forwarding table entry misconfig | `p4_table_entry_misconfig` | `configuration` / `misrouting` / `path` / `persistent` / `partial` | **P4**. `host_name` | On Thrift labs, modifies the first usable `simple_switch_CLI` entry. On `p4_dc_fabric`, points a concrete dest prefix at the wrong ActionSelector group through P4Runtime. | Matching traffic uses the wrong output or action while BMv2 stays up. | The dumped entry matches the recorded bad action or group. |
| Forwarding table entry missing | `p4_table_entry_missing` | `configuration` / `blackhole` / `path` / `persistent` / `partial` | **P4**. `host_name` | On Thrift labs, clears a populated table through `simple_switch_CLI`. On `p4_dc_fabric`, deletes a concrete dest prefix from `ipv4_lpm` through P4Runtime. | Packets needing that table or prefix miss or follow the default action. | The selected table has no match entries, or the prefix is absent from P4Runtime Read. |
| ActionSelector member misconfig | `p4_action_selector_member_misconfig` | `configuration` / `misrouting` / `path` / `persistent` / `partial` | `p4_dc_fabric`. `host_name` | Rewrites one ActionSelector member to a bogus egress port through P4Runtime. | Hashed flows that select that member fail; other members and other racks still forward. | P4Runtime member port differs from intent; same-rack and a control path stay up. |
| ECMP group member missing | `p4_ecmp_group_member_missing` | `configuration` / `misrouting` / `path` / `persistent` / `partial` | `p4_dc_fabric`. `host_name` | Deletes one spine member from a dest-rack ActionSelector group. | Group membership shrinks; remaining members and other prefixes still forward. | P4Runtime group no longer lists the member; same-rack and remaining cross-rack paths stay up. |
| P4Runtime pipeline mismatch | `p4runtime_pipeline_mismatch` | `configuration` / `blackhole` / `node` / `persistent` / `complete` | `p4_dc_fabric`. `host_name` | At inject time, compiles a drop-all P4 program and loads it with `SetForwardingPipelineConfig` on one switch. | Forwarding through that switch dies; other switches stay healthy. | Pipeline Read no longer matches the fabric program; an unaffected rack still pings. |
| P4Runtime partial write | `p4runtime_partial_write` | `configuration` / `blackhole` / `path` / `persistent` / `partial` | `p4_dc_fabric`. `host_name` | One Write mixes a valid LPM delete with an invalid update and `CONTINUE_ON_ERROR`. | The targeted prefix is missing; other intended entries remain. | P4Runtime Read lacks the prefix; same-rack traffic still works. |
| P4 table resource exhaustion | `p4_table_resource_exhaustion` | `resource` / `loss` / `node` / `persistent` / `partial` | `p4_dc_fabric`. `host_name` | Fills `ipv4_lpm` to its compiled size so later inserts fail. | Occupancy sits at cap; existing control prefixes still hit. | Write reports occupancy at size or a write error; same-rack and remaining paths stay up. |
| CORP VRF DSCP mis-remarking | `vrf_dscp_remarking` | `configuration` / `latency` / `path` / `persistent` / `partial` | **VPN** (`enterprise_branch` s/m/l). `host_name` (Site Edge: Branch, HQ, or DC2), `intf_name` (LAN→overlay WireGuard egress), `src_host` / `dst_host` (CORP EF path that exits that iface), `direction=lan_to_overlay`, optional `corp_prefix`. | Starts an ephemeral EF+CS0 compete workload on the target egress, records a healthy baseline, then installs nftables mangle on the Site Edge that rewrites CORP EF (DSCP 46) to CS0 (0) for packets leaving `intf_name`. Does not add extra netem/TBF congestion. | Under the same bulk load, the realtime flow shares the BE class after the boundary: destination DSCP is 0 and latency/jitter/loss degrade vs baseline while WG, eBGP, VRF RIB, SERVER HTTP, and other paths stay up. | nft rule present; source still EF; destination TOS is CS0; EF metrics degraded vs inject-time baseline; smoke connectivity/BGP/RIB checks pass. |
| WireGuard AllowedIPs omit of a remote business prefix | `wireguard_allowed_ips_misconfiguration` | `configuration` / `blackhole` / `path` / `persistent` / `partial` | **VPN** (`enterprise_branch` s/m/l). `host_name` (Branch Edge), `intf_name` (primary HQ peer, e.g. `wg_hq`), `target_prefix` (one remote advertised CORP/SERVER prefix, e.g. `10.0.20.0/24`). Dual-homed spokes are eligible because overlay BGP stays up and primary local-pref keeps the broken path. | Healthy labs still use catch-all `AllowedIPs = 0.0.0.0/0` with `Table = off`. Inject rewrites only the Branch→Hub peer AllowedIPs to an explicit allowlist that keeps the Hub tunnel `/32` and other remote enterprise prefixes, omitting `target_prefix`, then applies it with `wg syncconf`. Does not clear BGP or edit FRR. | Handshake continues; overlay BGP stays Established; the target prefix remains in FRR BGP/RIB and Linux routing via the WG iface; Edge can still reach the Hub tunnel address; LAN traffic to the omitted prefix fails while other business prefixes, other Branches, and underlay stay healthy. | Conf AllowedIPs omits `target_prefix` and retains Hub tunnel `/32`; latest handshake > 0; BGP neighbor Established; route to the target prefix still present via the WG iface. |
| Wrong WireGuard peer public key | `wireguard_peer_key_misconfiguration` | `configuration` / `down` / `path` / `persistent` / `complete` | **VPN** (`enterprise_branch` s/m/l). `host_name` (Branch Edge), `intf_name` (primary HQ peer, e.g. `wg_hq`). Every branch is dual-homed / dual-hub; inject corrupts **all** WireGuard peers on that Branch Edge so backup paths cannot mask the fault. Legacy id `host_vpn_membership_missing` rewrites to this failure (and a Site Edge inject target) when loading old benchmark YAML. | Replaces the Hub peer `PublicKey` on every WireGuard interface of the Branch Edge with a deterministic unused key and applies each with `wg syncconf` (interfaces stay up). Clears BGP neighbor sessions for those tunnels, then waits for settle. | Provider underlay and Hub WAN endpoints stay reachable; WG interfaces stay up; no tunnel completes a handshake; overlay BGP on that Branch fails; that Branch's cross-site business fails; other Branches stay healthy. | Conf `PublicKey` matches the injected wrong key on every WG iface; ifaces are up; `wg show` lists the wrong peer with no successful handshake. |

### Service Networking

| Root cause | Failure ID | Taxonomy metadata | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- | --- |
| Kubernetes ClusterIP forwarding failure | `k8s_clusterip_routing_broken` | `software` / `blackhole` / `node` / `persistent` / `complete` | **Kubernetes**. `control_node`, `node_name`, `service_name`, `namespace`, and `service_cidr` optional | Adds raw-table destination drops before kube-proxy DNAT on one node, for one ClusterIP or the whole Service CIDR. | Pods on that node cannot reach ClusterIP services while direct endpoint traffic and Kubernetes objects can remain healthy. | Raw PREROUTING and OUTPUT drops exist and the Service object remains intact. |
| Software middle-box overloads | `load_balancer_overload` | `resource` / `latency` / `service` / `persistent` / `partial` | **DHCP** scenario. `host_name`, `duration=300` | Runs `stress-ng` on the NGINX load balancer. | Requests through the load balancer slow or fail under CPU and memory pressure; direct backend behavior can differ. | `stress-ng` is running. |

### Management & Orchestration Plane

| Root cause | Failure ID | Taxonomy metadata | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- | --- |
| Kubernetes API-server partition | `k8s_worker_apiserver_partition` | `configuration` / `loss` / `node` / `persistent` / `partial` | **Kubernetes**. `control_node` optional, `node_name` optional, `apiserver_port=6443`, `apiserver_address` optional | Adds raw-table drops on one worker for TCP traffic to the k3s API server. | After the node-monitor grace period, the worker becomes NotReady and `kubectl logs` or `exec` fails while existing pods and ICMP reachability can survive. | Drop rules exist and the Kubernetes Node becomes NotReady; verification allows up to 240 seconds. |
| SDN controller crash | `sdn_controller_crash` | `software` / `down` / `service` / `persistent` / `complete` | **SDN**. `host_name` | Kills the POX process. | Existing switch flows can survive until they expire; new flows cannot be programmed. | No POX process is running. |
| Southbound port unreachable | `southbound_port_block` | `configuration` / `down` / `path` / `persistent` / `complete` | **SDN**. `host_name`, `southbound_port=6633` | Adds an nftables drop for the controller's OpenFlow port. | Switch-controller sessions fail while the controller process remains present. | The selected TCP port drop exists. |
| Southbound port unreachable | `southbound_port_mismatch` | `configuration` / `down` / `path` / `persistent` / `complete` | **SDN**. `host_name`, `mismatched_port=6653`, `original_port=6633` | Restarts POX on a port different from the switches' configured port. | POX runs while switches cannot connect to it. | The POX command line contains the mismatched port. |

### Addressing, Neighbor & Naming

| Root cause | Failure ID | Taxonomy metadata | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- | --- |
| MAC address conflict | `mac_address_conflict` | `configuration` / `loss` / `link` / `intermittent` / `partial` | **Host L2**. `host_name`, `host_name_2` | Copies one host's `eth0` MAC address to the other. | Switch learning moves the shared MAC between ports as both hosts send traffic. | Both interfaces report the same MAC address. |
| DHCP spoofing | `dhcp_missing_subnet` | `configuration` / `loss` / `service` / `persistent` / `complete` | **DHCP**. `host_name`, `host_name_2` | Removes the client's subnet block from `dhcpd.conf` and renews DHCP clients. | Clients in that subnet cannot obtain a valid lease while other subnets can continue. | The subnet declaration is absent from `dhcpd.conf`. |
| Host crash | `dhcp_service_down` | `software` / `down` / `service` / `persistent` / `complete` | **DHCP**. `host_name`, `service_name=isc-dhcp-server` | Kills `dhcpd` to simulate a DHCP host or service crash. | New leases and renewals fail; existing leases can hide the incident until renewal. | `dhcpd` is absent from the process table. |
| DNS spoofing | `dns_record_error` | `configuration` / `misrouting` / `service` / `persistent` / `partial` | **DNS**. `host_name`, `target_website`, `target_domain`, `wrong_ip` optional | Replaces the target A record in the BIND zone and reloads the service. | Queries for one name return the wrong address while other DNS records can remain correct. | The zone contains the wrong IP and the running DNS server returns it. |
| Host crash | `dns_service_down` | `software` / `down` / `service` / `persistent` / `complete` | **DNS**. `host_name`, `service_name=named` | Kills the BIND `named` process to simulate a DNS host or service crash. | DNS-dependent web access fails while direct IP traffic can remain healthy. | `named` is absent from the process table. |
| DNS spoofing | `host_incorrect_dns` | `configuration` / `blackhole` / `service` / `persistent` / `partial` | **DNS**. `host_name`, `fake_dns_ip=8.8.8.8` | Rewrites `/etc/resolv.conf` with the fake resolver address. | Name-based requests fail or resolve through the wrong server while direct IP requests still work. | `/etc/resolv.conf` contains the fake resolver. |
| Host IP misconfig | `host_incorrect_gateway` | `configuration` / `blackhole` / `host` / `persistent` / `partial` | **Routed host**. `host_name`, `new_gateway` optional | Adds an invalid or caller-selected default gateway after reapplying the host address. | Same-subnet traffic can work while off-subnet traffic fails. | The default route output contains the injected gateway. |
| Host IP misconfig | `host_incorrect_ip` | `configuration` / `blackhole` / `host` / `persistent` / `complete` | **Host L2**. `host_name`, `incorrect_ip` optional | Replaces the target's `eth0` address; NIKA derives a wrong address when omitted. | The host leaves its intended subnet or conflicts with routing assumptions. | The current address differs from the address captured before injection. |
| Incorrect netmask | `host_incorrect_netmask` | `configuration` / `loss` / `host` / `persistent` / `partial` | **Routed host**. `host_name`, `netmask_prefix=8` | Replaces the `eth0` prefix length. | The host treats remote destinations as on-link or routes local destinations through its gateway. | `eth0` has the injected non-`/24` prefix. |
| Host IP conflict | `host_ip_conflict` | `configuration` / `loss` / `host` / `intermittent` / `partial` | **Host L2**. `host_name`, `host_name_2` | Assigns the second host's IPv4 address to the first host. | ARP ownership can alternate, causing intermittent or misdelivered traffic when either address is used. | Both `eth0` interfaces report the same IPv4 address. |
| Host IP misconfig | `host_missing_ip` | `configuration` / `down` / `host` / `persistent` / `complete` | **Host L2**. `host_name`, `intf_name=eth0` | Flushes the interface's global IPv4 address. | Local and remote IP traffic fails while the link can remain up. | The interface has no global IPv4 address. |
| Kubernetes DNS path block | `k8s_coredns_isolated` | `configuration` / `blackhole` / `service` / `persistent` / `complete` | **Kubernetes**. `control_node` and `node_name` optional, `dns_service=kube-dns`, `dns_namespace=kube-system`, `dns_selector=k8s-app=kube-dns`, `dns_port=53`, `include_pod_ips=true` | Drops TCP and UDP DNS traffic to the CoreDNS Service IP and, by default, CoreDNS pod IPs on the selected nodes. | Cluster-name queries time out while CoreDNS pods remain Ready and direct IP traffic can work. | All drop rules exist and CoreDNS pods remain Ready; details also probe port 53 and metrics port 9153. |
| Service DoS | `dns_lookup_latency` | `resource` / `latency` / `service` / `persistent` / `partial` | **DNS**. `host_name`, `intf_name=eth0`, `delay_ms=1000` | Adds a `tc netem` delay to the DNS server interface, simulating the latency symptom of an overloaded or attacked DNS service without generating attack traffic. | Name-based requests pause on DNS while direct IP requests avoid the lookup delay. | `tc qdisc` reports the configured delay. |
| ARP cache poisoning | `arp_cache_poisoning` | `adversarial` / `misrouting` / `multi_node` / `persistent` / `partial` | **Host L2**. `host_name`, `fake_mac=00:11:22:33:44:55` | Adds a static ARP entry that maps the default gateway to the fake MAC. | Off-subnet traffic from the target is sent to the wrong L2 destination. | The neighbor table contains the fake MAC. |
| DHCP spoofing | `dhcp_spoofed_dns` | `adversarial` / `misrouting` / `multi_node` / `persistent` / `partial` | **DHCP**. `host_name`, `host_name_2`, `wrong_dns=8.8.8.8` | Changes the subnet's DNS option and renews leases. | Renewed clients send queries to the wrong resolver while direct IP traffic can work. | `dhcpd.conf` contains the spoofed DNS option. |
| DHCP spoofing | `dhcp_spoofed_gateway` | `adversarial` / `misrouting` / `multi_node` / `persistent` / `partial` | **DHCP**. `host_name`, `host_name_2` | Changes the target subnet's router option to an address ending in `.254`, then renews leases. | Renewed clients lose off-subnet traffic while local LAN traffic can work. | `dhcpd.conf` contains the spoofed router option. |
| DHCP spoofing | `dhcp_spoofed_subnet` | `adversarial` / `misrouting` / `multi_node` / `persistent` / `partial` | **DHCP**. `host_name`, `host_name_2` | Removes the matching subnet declaration and renews leases. | Clients in the target subnet cannot renew a valid configuration. | The target subnet declaration is absent. |

### Endpoint & Application

| Root cause | Failure ID | Taxonomy metadata | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- | --- |
| Host crash | `host_crash` | `software` / `down` / `host` / `persistent` / `complete` | **Host L2**. `host_name` | Pauses the target container through the runtime. | All services and traffic on the host stop without changing the surrounding topology. | Backend state reports the container as paused. |
| Receiver saturated and slow | `receiver_resource_contention` | `resource` / `degraded_throughput` / `host` / `persistent` / `partial` | **HTTP**. `host_name`, `duration=600` | Runs `stress-ng` on the receiver. | Request generation or receive-side processing slows while the network remains configured. | `stress-ng` is running on the receiver. |
| Sender saturated and slow | `sender_application_delay` | `software` / `latency` / `host` / `persistent` / `partial` | **HTTP**. `host_name` | Replaces `/web_server.py` with a server implementation that sleeps before sending data, then restarts the service. | HTTP responses have application delay without a link or CPU fault. | The deployed server source contains `time.sleep`. |
| Sender saturated and slow | `sender_resource_contention` | `resource` / `degraded_throughput` / `host` / `persistent` / `partial` | **HTTP**. `host_name`, `duration=600` | Runs `stress-ng` on an HTTP sender. | Responses from that sender slow under CPU, memory, and I/O contention. | `stress-ng` is running on the sender. |
| Service DoS | `web_dos_attack` | `adversarial` / `latency` / `service` / `persistent` / `partial` | **HTTP**. `host_name`, `attacker_device` | Runs an unbounded ApacheBench loop with high request count and concurrency from the attacker. | HTTP latency and error rate rise while the attack process runs; impact depends on server capacity. | An `ab` process is active on the attacker. |

### Traffic, Queueing & Resource

| Root cause | Failure ID | Taxonomy metadata | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- | --- |
| Incast traffic | `incast_traffic_network_limitation` | `resource` / `degraded_throughput` / `multi_node` / `persistent` / `partial` | **HTTP**. `host_name`, `rate=1mbit`, `burst=500kb`, `limit=500kb`, `delay_ms=20` | Adds delay and a child token-bucket filter on `eth0`, then starts 20 Mbps UDP flows from every other scenario host to the target for 300 seconds. | The generated many-to-one load exceeds the 1 Mbit receiver limit, exposing queueing, latency, and throughput collapse. | `tc qdisc` contains the netem or TBF discipline. |
| Microbursts on interface | `link_bandwidth_throttling` | `resource` / `degraded_throughput` / `link` / `persistent` / `partial` | **All**. `host_name`, `rate=30kbit`, `burst=64kb`, `limit=500kb` | Applies a token-bucket filter to the target's first interface. On Kathara, injection also starts 20 Mbps UDP flows from discovered peers to the target for 300 seconds. | Generated or caller-supplied load above the default 30 kbit rate causes lower throughput, queueing, or loss. | `tc qdisc` reports the TBF. |

## Metadata review items

The final domain mapping fixes the subsystem assignment for all 62 failures. Review these metadata choices before publishing aggregate cause or severity results:

- `link_detach` uses `cause: operational`; use `hardware` if the benchmark models cable or NIC removal.
- `sender_application_delay` uses `cause: software`; use `operational` if the benchmark models an operator-controlled workload.
- `impact` measures severity inside the declared `scope`. Validate complete-versus-partial labels against the paper's aggregation method.

## Regenerate benchmark coverage

Failure IDs and `TAGS` remain unchanged, so scenario compatibility remains unchanged. Regenerate the working matrices after a registry or tag change, then refresh the failure × scenario tables in [Benchmark configuration](benchmark-configuration.md):

```shell
uv run python benchmark/generate_benchmark.py
uv run python scripts/render_coverage_matrix.py --write-docs
```

[`prob_pool.py`](../src/nika/problems/prob_pool.py) discovers failure classes. [`problem_base.py`](../src/nika/problems/problem_base.py) validates taxonomy metadata. Domain directories contain registered failures; [`support/`](../src/nika/problems/support/) contains the shared Kubernetes failure base and node filter. See [Benchmark configuration](benchmark-configuration.md), [Create benchmark tasks](creating-benchmark-tasks.md), and [Root-cause ground truth and scoring](root-cause-evaluation.md).
