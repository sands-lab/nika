# Failure reference

This reference organizes every single-fault implementation registered by NIKA into six failure categories. It is for benchmark authors who need to choose a compatible scenario, supply injection parameters, and understand the evidence used to confirm an active fault.

NIKA discovers concrete `ProblemBase` subclasses under [`src/nika/problems/`](../src/nika/problems/) and keys them by `root_cause_name`. [`prob_pool.py`](../src/nika/problems/prob_pool.py) implements the registry. Each failure's `root_cause_resources()` method maps injection parameters to structured RCA labels. See [Root-cause ground truth and scoring](root-cause-evaluation.md). Inspect the installed checkout and an exact parameter schema with:

```shell
uv run nika failure list
uv run nika failure describe <failure_id>
```

## Failure categories and counts

NIKA currently registers 60 code-level failure IDs. The checked-in working matrix contains 580 cases for all 60 IDs. The totals below come from the current failure registry and [`benchmark/benchmark_full.yaml`](../benchmark/benchmark_full.yaml).

| Category | Registered failure IDs | Working-matrix cases |
| --- | ---: | ---: |
| Link failures | 6 | 142 |
| End-host failures | 10 | 128 |
| Network node errors | 9 | 45 |
| Misconfigurations (routing, ACL, and related configuration) | 18 | 128 |
| Resource contention | 6 | 67 |
| Network under attack | 11 | 70 |
| **Total** | **60** | **580** |

The registry and working matrix are the sources of truth for failure IDs and case counts. The headings provide the six-category organization used by this reference.

## How to read the compatibility sets

The **Scenarios and parameters** column names a compatibility set from the table below. These sets reproduce the tag-subset matching in [`benchmark/generate_benchmark.py`](../benchmark/generate_benchmark.py) and the current scenario registry. A tag match means the scenario exposes the needed device family. Injection still requires a suitable target device, interface, service, route, or workload inside that scenario.

| Set | Scenario IDs |
| --- | --- |
| **All** | All 14 registered scenarios |
| **Host L2** | `dc_clos`, `campus_lan`, `rip_small_internet_vpn`, `sdn_star`, `sdn_clos`, `p4_bloom_filter`, `p4_counter`, `p4_int`, `p4_mpls`, `simple_bgp`, `k8s_lab`, `llmd_lab` |
| **Routed host** | `dc_clos`, `campus_lan`, `rip_small_internet_vpn`, `simple_bgp`, `k8s_lab` |
| **FRR** | `dc_clos`, `campus_lan`, `rip_small_internet_vpn`, `simple_bgp`, `isp`, `k8s_lab` |
| **BGP** | `dc_clos`, `simple_bgp`, `isp`, `min3clos`, `k8s_lab` |
| **OSPF** | `campus_lan`, `isp` |
| **DNS** | `dc_clos` (service workload), `campus_lan` (dhcp workload) |
| **HTTP** | `dc_clos` (service workload), `campus_lan`, `rip_small_internet_vpn`, `llmd_lab` |
| **DHCP** | `campus_lan` (dhcp workload) |
| **SDN** | `sdn_star`, `sdn_clos` |
| **P4** | `p4_bloom_filter`, `p4_counter`, `p4_int`, `p4_mpls` |
| **Kubernetes** | `k8s_lab`, `llmd_lab` |
| **VPN** | `rip_small_internet_vpn` |

`isp` needs an extra protocol condition. OSPF faults require `--igp ospf`; BGP faults require `--bgp-mode ibgp_rr` or `ebgp`. The generic link faults work with either ISP backend. SR Linux support exists only where the failure implementation contains a Containerlab branch.

For `isp`, pass `target_network` to `bgp_hijacking` because the router-only topology has no web-host default. `bgp_blackhole_route_leak` and `host_static_blackhole` resolve a neighboring router as the victim. For link failures on Containerlab, the default `intf_name=eth0` maps to `e1-1`.

The **Trigger and signal** column describes the traffic or state that exposes the incident and the main symptom a troubleshooting agent can observe. The **Verification evidence** column describes what `verify_fault()` checks. NIKA stops the workflow when verification cannot prove the injected state. Third-level headings group failures by affected protocol or component; they are navigation labels, not runtime `RootCauseCategory` values.

## Link failures

### Interface state

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Link flap | `link_flap` | **All**. `host_name`, `intf_name=eth0`, `down_time=1`, `up_time=1` | Starts a loop that alternates the interface between down and up. | Sustained ping or protocol sessions show periodic loss and adjacency churn. | The recorded flap process and PID remain alive. |
| Link detached | `link_detach` | **All**. `host_name`, `intf_name=eth0` | Removes the interface from the running node through the backend runtime. | Traffic using the attachment fails and the interface disappears from inventory. | The interface no longer exists. |
| Link down | `link_down` | **All**. `host_name`, `intf_name=eth0` | Sets the selected interface down. | Traffic crossing the interface loses reachability; routing adjacencies on it drop. | Interface `operstate` is `down`. |

### Link quality and packet handling

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Faulty cable | `link_high_packet_corruption` | **All**. `host_name`, `corruption_percentage=60` | Applies `tc netem corrupt` to the target's last interface, simulating a cable that corrupts frames. | Packet streams across the interface show checksum failures, loss, and retransmissions. | `tc qdisc` reports the configured corruption rate. |
| Link fragmentation disabled | `link_fragmentation_disabled` | **All**. `host_name`, `mtu=10` | Adds an iptables OUTPUT length-based drop rule instead of changing path MTU discovery. | Packets at or above the limit drop while smaller packets can pass, producing size-dependent loss. | The exact length/drop rule appears in iptables. |

### Layer 2 identity

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| MAC address conflict | `mac_address_conflict` | **Host L2**. `host_name`, `host_name_2` | Copies one host's `eth0` MAC address to the other. | Switch learning moves the shared MAC between ports as both hosts send traffic. | Both interfaces report the same MAC address. |

## End-host failures

### VPN membership

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Conflicting VPN memberships | `host_vpn_membership_missing` | **VPN**. `host_name`, `host_name_2` | Comments the selected peer's WireGuard membership lines and reloads the configuration, simulating a membership conflict or omission. | Underlay IP traffic remains available while traffic requiring the affected WireGuard peer fails. | The target peer lines are commented in the VPN configuration. |

### Host and service availability

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Host crash | `host_crash` | **Host L2**. `host_name` | Pauses the target container through the runtime. | All services and traffic on the host stop without changing the surrounding topology. | Backend state reports the container as paused. |
| Host crash | `dns_service_down` | **DNS**. `host_name`, `service_name=named` | Kills the BIND `named` process to simulate a DNS host or service crash. | DNS-dependent web access fails while direct IP traffic can remain healthy. | `named` is absent from the process table. |
| Host crash | `dhcp_service_down` | **DHCP**. `host_name`, `service_name=isc-dhcp-server` | Kills `dhcpd` to simulate a DHCP host or service crash. | New leases and renewals fail; existing leases can hide the incident until renewal. | `dhcpd` is absent from the process table. |

### IP addressing

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Host IP conflict | `host_ip_conflict` | **Host L2**. `host_name`, `host_name_2` | Assigns the second host's IPv4 address to the first host. | ARP ownership can alternate, causing intermittent or misdelivered traffic when either address is used. | Both `eth0` interfaces report the same IPv4 address. |
| Host IP misconfig | `host_incorrect_gateway` | **Routed host**. `host_name`, `new_gateway` optional | Adds an invalid or caller-selected default gateway after reapplying the host address. | Same-subnet traffic can work while off-subnet traffic fails. | The default route output contains the injected gateway. |
| Host IP misconfig | `host_incorrect_ip` | **Host L2**. `host_name`, `incorrect_ip` optional | Replaces the target's `eth0` address; NIKA derives a wrong address when omitted. | The host leaves its intended subnet or conflicts with routing assumptions. | The current address differs from the address captured before injection. |
| Host IP misconfig | `host_missing_ip` | **Host L2**. `host_name`, `intf_name=eth0` | Flushes the interface's global IPv4 address. | Local and remote IP traffic fails while the link can remain up. | The interface has no global IPv4 address. |
| Incorrect netmask | `host_incorrect_netmask` | **Routed host**. `host_name`, `netmask_prefix=8` | Replaces the `eth0` prefix length. | The host treats remote destinations as on-link or routes local destinations through its gateway. | `eth0` has the injected non-`/24` prefix. |

### DNS reachability

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| DNS empty answer | `dns_port_blocked` | **DNS**. `host_name` | Adds nftables drops for TCP and UDP port 53, simulating a resolver that returns no usable answer from the client's perspective. | DNS queries time out while the BIND process and direct IP connectivity remain healthy. | Both DNS port rules appear in nftables. |

## Network node errors

### MPLS limits

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Number of MPLS labels hit limit | `mpls_label_limit_exceeded` | `p4_mpls`. `host_name` | Changes the program's `CONST_MAX_LABELS` limit to 2 and restarts compilation. | MPLS behavior needing more labels fails or compilation output disappears. | Source contains the limit 2; details also report whether JSON exists. |

### Router and switch availability

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Switch/router crash | `bmv2_switch_down` | **P4**. `host_name` | Kills `simple_switch` on a BMv2 node. | Every path through that switch fails and the P4 control CLI cannot reach the process. | No `simple_switch` process is running. |
| Switch/router crash | `frr_service_down` | **FRR**. `host_name`, `service_name=frr` | Stops FRR on a router. | Dynamic adjacencies and routes disappear while connected forwarding can remain. | Zebra is absent and FRR routing commands are unavailable. |

### P4 pipeline

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| P4 program reads invalid header field | `p4_header_definition_error` | **P4**. `host_name`, `p4_name` optional | Duplicates an Ethernet type field in the P4 header, removes JSON, and restarts BMv2 compilation. | Compilation fails or the switch cannot start; dependent paths fail. | Compiled JSON is missing or `simple_switch` is not running. |
| P4 program reads invalid header field | `p4_compilation_error_parser_state` | **P4**. `host_name`, `p4_name` optional | Rewrites `state` as invalid `states` syntax, removes JSON, and restarts. | The compiler or switch startup fails. | Compiled JSON is missing or `simple_switch` is not running. |

### SDN control plane

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| SDN controller crash | `sdn_controller_crash` | **SDN**. `host_name` | Kills the POX process. | Existing switch flows can survive until they expire; new flows cannot be programmed. | No POX process is running. |
| Southbound port unreachable | `southbound_port_block` | **SDN**. `host_name`, `southbound_port=6633` | Adds an nftables drop for the controller's OpenFlow port. | Switch-controller sessions fail while the controller process remains present. | The selected TCP port drop exists. |
| Southbound port unreachable | `southbound_port_mismatch` | **SDN**. `host_name`, `mismatched_port=6653`, `original_port=6633` | Restarts POX on a port different from the switches' configured port. | POX runs while switches cannot connect to it. | The POX command line contains the mismatched port. |

### Kubernetes service forwarding

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Kubernetes ClusterIP forwarding failure | `k8s_clusterip_routing_broken` | **Kubernetes**. `control_node`, `node_name`, `service_name`, `namespace`, and `service_cidr` optional | Adds raw-table destination drops before kube-proxy DNAT on one node, for one ClusterIP or the whole Service CIDR. | Pods on that node cannot reach ClusterIP services while direct endpoint traffic and Kubernetes objects can remain healthy. | Raw PREROUTING and OUTPUT drops exist and the Service object remains intact. |

## Misconfigurations (routing, ACL, and related configuration)

### BGP routing

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| BGP ASN mismatch | `bgp_asn_misconfig` | **BGP**. `host_name` | Changes the local ASN in FRR or SR Linux configuration. | Peers reject or reset sessions because the configured remote AS no longer matches. | Running configuration contains the changed ASN. |
| BGP blackhole route leak | `bgp_blackhole_route_leak` | **BGP**. `host_name` | Resolves a victim `/30`, installs a Null0 or blackhole route, and advertises it through BGP. | Traffic to the more-specific victim network follows the leaked route and drops. | The blackhole route or its advertisement exists. |
| Missing BGP advertisement | `bgp_missing_route_advertisement` | **BGP**. `host_name` | Removes an FRR network advertisement or applies an SR Linux export policy that withdraws it. | Peers stay reachable while they lose the selected prefix. | FRR configuration lacks the advertisement or SR Linux applies the withdrawal policy. |

### Static routing

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Host static blackhole | `host_static_blackhole` | **BGP**. `host_name` | Installs a static blackhole route for a resolved victim network without advertising the route. | Traffic matching the victim prefix drops at the target router. | The target running configuration contains the blackhole route. |

### OSPF routing

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| OSPF area misconfiguration | `ospf_area_misconfiguration` | **OSPF**. `host_name` | Changes an OSPF network statement to a mismatched area and restarts FRR. | Adjacency fails on links whose endpoints no longer agree on area. | Both file and running configuration show the changed area. |
| OSPF neighbor missing | `ospf_neighbor_missing` | **OSPF**. `host_name` | Comments OSPF network statements in `frr.conf` and removes them from the daemon. | The router stops forming expected OSPF adjacencies and loses learned routes. | File statements are commented and the daemon has no active network statements. |

### P4 forwarding tables

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Forwarding table entry misconfig | `p4_table_entry_misconfig` | **P4**. `host_name` | Modifies the action or data of the first usable table entry through `simple_switch_CLI`. | Matching traffic uses the wrong output or action while BMv2 stays up. | The dumped entry matches the recorded bad action data. |
| Forwarding table entry misconfig | `p4_table_entry_missing` | **P4**. `host_name` | Finds a populated table and clears its entries through `simple_switch_CLI`. | Packets needing that table miss or follow the default action. | The selected table has no match entries. |

### SDN flow rules

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Flow rule loop | `flow_rule_loop` | **SDN**. `host_name`, `host_name_2` | Configures each selected switch to emit matching packets through their ingress-facing port. | Traffic on the affected attachments reflects toward its incoming link and can loop with the adjacent path. | Both switches contain an `in_port` rule with an output action. |
| Flow rule shadowing | `flow_rule_shadowing` | **SDN**. `host_name` | Installs a high-priority OVS drop that takes precedence over normal forwarding. | Matching traffic fails even though lower-priority learning-switch flows exist. | OVS reports the high-priority drop flow. |

### ACL and protocol filtering

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| ARP ACL block | `arp_acl_block` | **Host L2**. `host_name` | Adds an nftables ARP drop and flushes the neighbor cache. | New ARP resolution fails; cached entries are removed so local-subnet traffic stops. | An ARP drop appears in nftables. |
| ICMP ACL block | `icmp_acl_block` | `dc_clos`, `campus_lan`, `rip_small_internet_vpn`, `sdn_star`, `sdn_clos`, **P4**, `simple_bgp`, `isp`, **Kubernetes**. `host_name` | Adds an nftables ICMP drop. | Ping and ICMP-based health checks fail while TCP or UDP services can remain reachable. | The ruleset contains an ICMP drop. |
| Routing control-plane ACL block | `bgp_acl_block` | **BGP**. `host_name` | Kathara adds nftables drops for TCP source and destination port 179; Containerlab installs the SR Linux ACL equivalent. | BGP sessions reset or cannot establish; routes learned through those sessions disappear. | The port-179 drop exists in nftables or the SR Linux ACL. |
| Routing control-plane ACL block | `ospf_acl_block` | **OSPF**. `host_name` | Adds an nftables rule that drops IP protocol 89. | OSPF neighbors time out and learned routes withdraw while unrelated protocols can pass. | The nftables ruleset contains the OSPF drop. |
| HTTP ACL block | `http_acl_block` | **HTTP**. `host_name` | Adds nftables drops for TCP port 80. | HTTP requests to or from the target time out while non-HTTP traffic can pass. | The ruleset contains the port-80 drop. |

### Kubernetes networking

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Kubernetes API-server partition | `k8s_worker_apiserver_partition` | **Kubernetes**. `control_node` optional, `node_name` optional, `apiserver_port=6443`, `apiserver_address` optional | Adds raw-table drops on one worker for TCP traffic to the k3s API server. | After the node-monitor grace period, the worker becomes NotReady and `kubectl logs` or `exec` fails while existing pods and ICMP reachability can survive. | Drop rules exist and the Kubernetes Node becomes NotReady; verification allows up to 240 seconds. |
| Kubernetes DNS path block | `k8s_coredns_isolated` | **Kubernetes**. `control_node` and `node_name` optional, `dns_service=kube-dns`, `dns_namespace=kube-system`, `dns_selector=k8s-app=kube-dns`, `dns_port=53`, `include_pod_ips=true` | Drops TCP and UDP DNS traffic to the CoreDNS Service IP and, by default, CoreDNS pod IPs on the selected nodes. | Cluster-name queries time out while CoreDNS pods remain Ready and direct IP traffic can work. | All drop rules exist and CoreDNS pods remain Ready; details also probe port 53 and metrics port 9153. |
| Kubernetes policy deny | `k8s_networkpolicy_deny` | **Kubernetes**. `control_node` optional, `namespace`, `pod_selector`, `policy_name`, `symptom_host`, `symptom_url`, `control_url` | Applies a deny-all-ingress `NetworkPolicy` to selected pods. | The selected route fails while a caller-supplied sibling route stays healthy; target pods remain Ready. | Policy exists, selected pods are Ready, the symptom URL fails, and the control URL succeeds. |

The generator sets workload-specific `namespace`, `pod_selector`, and probe URLs for `k8s_lab` (`word-ns` / `app=word`) and `llmd_lab` (`llm-d` / `app=llm-d-pd`). An empty `control_url` skips the sibling-route check. Both scenarios declare the `network_policy` tag; k3s kube-router enforces the policy.

## Resource contention

### Link capacity

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Microbursts on interface | `link_bandwidth_throttling` | **All**. `host_name`, `rate=30kbit`, `burst=64kb`, `limit=500kb` | Applies a token-bucket filter to the target's first interface. On Kathara, injection also starts 20 Mbps UDP flows from discovered peers to the target for 300 seconds. | Generated or caller-supplied load above the default 30 kbit rate causes lower throughput, queueing, or loss. | `tc qdisc` reports the TBF. |

### Endpoint and fan-in pressure

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Receiver saturated and slow | `receiver_resource_contention` | **HTTP**. `host_name`, `duration=600` | Runs `stress-ng` on the receiver. | Request generation or receive-side processing slows while the network remains configured. | `stress-ng` is running on the receiver. |
| Incast traffic | `incast_traffic_network_limitation` | **HTTP**. `host_name`, `rate=1mbit`, `burst=500kb`, `limit=500kb`, `delay_ms=20` | Adds delay and a child token-bucket filter on `eth0`, then starts 20 Mbps UDP flows from every other scenario host to the target for 300 seconds. | The generated many-to-one load exceeds the 1 Mbit receiver limit, exposing queueing, latency, and throughput collapse. | `tc qdisc` contains the netem or TBF discipline. |
| Sender saturated and slow | `sender_resource_contention` | **HTTP**. `host_name`, `duration=600` | Runs `stress-ng` on an HTTP sender. | Responses from that sender slow under CPU, memory, and I/O contention. | `stress-ng` is running on the sender. |
| Sender saturated and slow | `sender_application_delay` | **HTTP**. `host_name` | Replaces `/web_server.py` with a server implementation that sleeps before sending data, then restarts the service. | HTTP responses have application delay without a link or CPU fault. | The deployed server source contains `time.sleep`. |

### Middlebox overload

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Software middle-box overloads | `load_balancer_overload` | **DHCP** scenario. `host_name`, `duration=300` | Runs `stress-ng` on the NGINX load balancer. | Requests through the load balancer slow or fail under CPU and memory pressure; direct backend behavior can differ. | `stress-ng` is running. |

## Network under attack

### Service denial of service

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Service DoS | `web_dos_attack` | **HTTP**. `host_name`, `attacker_device` | Runs an unbounded ApacheBench loop with high request count and concurrency from the attacker. | HTTP latency and error rate rise while the attack process runs; impact depends on server capacity. | An `ab` process is active on the attacker. |
| Service DoS | `dns_lookup_latency` | **DNS**. `host_name`, `intf_name=eth0`, `delay_ms=1000` | Adds a `tc netem` delay to the DNS server interface, simulating the latency symptom of an overloaded or attacked DNS service without generating attack traffic. | Name-based requests pause on DNS while direct IP requests avoid the lookup delay. | `tc qdisc` reports the configured delay. |

### BGP hijacking

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| BGP hijacking | `bgp_hijacking` | **BGP**. `host_name`, `target_network` optional | Makes FRR or SR Linux originate an unauthorized prefix; FRR also installs it on loopback. | BGP peers select the injected route when policy and prefix selection permit it. | The router advertises the prefix; FRR also has the loopback address. |

### DHCP spoofing

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| DHCP spoofing | `dhcp_spoofed_gateway` | **DHCP**. `host_name`, `host_name_2` | Changes the target subnet's router option to an address ending in `.254`, then renews leases. | Renewed clients lose off-subnet traffic while local LAN traffic can work. | `dhcpd.conf` contains the spoofed router option. |
| DHCP spoofing | `dhcp_spoofed_dns` | **DHCP**. `host_name`, `host_name_2`, `wrong_dns=8.8.8.8` | Changes the subnet's DNS option and renews leases. | Renewed clients send queries to the wrong resolver while direct IP traffic can work. | `dhcpd.conf` contains the spoofed DNS option. |
| DHCP spoofing | `dhcp_missing_subnet` | **DHCP**. `host_name`, `host_name_2` | Removes the client's subnet block from `dhcpd.conf` and renews DHCP clients. | Clients in that subnet cannot obtain a valid lease while other subnets can continue. | The subnet declaration is absent from `dhcpd.conf`. |
| DHCP spoofing | `dhcp_spoofed_subnet` | **DHCP**. `host_name`, `host_name_2` | Removes the matching subnet declaration and renews leases. | Clients in the target subnet cannot renew a valid configuration. | The target subnet declaration is absent. |

### DNS spoofing

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| DNS spoofing | `dns_record_error` | **DNS**. `host_name`, `target_website`, `target_domain`, `wrong_ip` optional | Replaces the target A record in the BIND zone and reloads the service. | Queries for one name return the wrong address while other DNS records can remain correct. | The zone contains the wrong IP and the running DNS server returns it. |
| DNS spoofing | `host_incorrect_dns` | **DNS**. `host_name`, `fake_dns_ip=8.8.8.8` | Rewrites `/etc/resolv.conf` with the fake resolver address. | Name-based requests fail or resolve through the wrong server while direct IP requests still work. | `/etc/resolv.conf` contains the fake resolver. |

### ARP poisoning

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| ARP cache poisoning | `arp_cache_poisoning` | **Host L2**. `host_name`, `fake_mac=00:11:22:33:44:55` | Adds a static ARP entry that maps the default gateway to the fake MAC. | Off-subnet traffic from the target is sent to the wrong L2 destination. | The neighbor table contains the fake MAC. |

### P4 detection thresholds

| Root cause | Failure ID | Scenarios and parameters | Injection method | Trigger and signal | Verification evidence |
| --- | --- | --- | --- | --- | --- |
| Misaligned sketch thresholds | `p4_aggressive_detection_thresholds` | `p4_bloom_filter`. `host_name`, `p4_name` optional | Changes `PACKET_THRESHOLD` from 1000 to 100, removes JSON, and restarts the switch. | A TCP flow reaches the drop threshold ten times sooner than designed, causing false-positive detection and drops. | P4 source contains `PACKET_THRESHOLD 100`. |

## Inject and inspect a failure

Deploy a compatible scenario, inspect the schema, and pass each required value with `--set`:

```shell
uv run nika env run simple_bgp
uv run nika failure describe link_down
uv run nika failure inject link_down \
  --set host_name=router1 --set intf_name=eth0
uv run nika failure ps
```

Parameters identify device names inside the running scenario, not Docker container names. Use `uv run nika session inspect` and `uv run nika session containers` to resolve the active inventory.

The checked-in [`benchmark/benchmark_full.yaml`](../benchmark/benchmark_full.yaml) contains 580 cases for all 60 registered failures. Its scenario rows reflect the file's generation date; run `uv run python benchmark/generate_benchmark.py` after intentional registry changes. See [Create benchmark tasks](creating-benchmark-tasks.md) for the implementation contract and [Network scenario reference](network-scenarios.md) for topology details.
