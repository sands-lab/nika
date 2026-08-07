# Failure types

NIKA's problem registry currently defines 60 injectable root causes. Of those, 59 are represented in the checked-in `benchmark/benchmark_full.yaml`, resulting in 708 cases across network topologies and sizes. The table below lists every registered problem and its current working-matrix case count; zero means the problem is registered but is not present in that YAML.


| Category | Problem ID | Description | # Cases |
| -------- | ---------- | ----------- | ------- |
| `end_host_failure` | `dns_record_error` | Some hosts cannot access external websites. | 6 |
| `end_host_failure` | `host_crash` | host_crash | 28 |
| `end_host_failure` | `host_incorrect_dns` | Some hosts are unable to access web services. | 6 |
| `end_host_failure` | `host_incorrect_gateway` | Some hosts seem to be unreachable in the network. | 17 |
| `end_host_failure` | `host_incorrect_ip` | Some hosts seem to be unreachable in the network. | 28 |
| `end_host_failure` | `host_incorrect_netmask` | Some hosts seem to be unreachable in the network. | 17 |
| `end_host_failure` | `host_ip_conflict` | Some hosts experience intermittent connectivity issues. | 28 |
| `end_host_failure` | `host_missing_ip` | Some hosts are unable to communicate with other devices in the network. | 28 |
| `end_host_failure` | `host_vpn_membership_missing` | host_vpn_membership_missing | 3 |
| `link_failure` | `bmv2_switch_down` | bmv2_switch_down | 4 |
| `link_failure` | `dhcp_service_down` | dhcp_service_down | 3 |
| `link_failure` | `dns_service_down` | Some hosts cannot access external websites. | 6 |
| `link_failure` | `link_detach` | Users report connectivity issues to other hosts. | 29 |
| `link_failure` | `link_down` | Users report connectivity issues to other hosts. | 29 |
| `link_failure` | `link_flap` | Users report connectivity issues to other hosts. | 29 |
| `link_failure` | `link_fragmentation_disabled` | Users report partial packet loss when communicating with other hosts. | 29 |
| `misconfiguration` | `arp_acl_block` | arp_acl_block | 28 |
| `misconfiguration` | `bgp_acl_block` | bgp_acl_block | 9 |
| `misconfiguration` | `bgp_asn_misconfig` | Some hosts are experiencing connectivity issues. | 9 |
| `misconfiguration` | `bgp_blackhole_route_leak` | bgp_blackhole_route_leak | 9 |
| `misconfiguration` | `bgp_missing_route_advertisement` | bgp_missing_route_advertisement | 9 |
| `misconfiguration` | `dhcp_missing_subnet` | dhcp_missing_subnet | 3 |
| `misconfiguration` | `dns_port_blocked` | dns_port_blocked | 6 |
| `misconfiguration` | `host_static_blackhole` | host_static_blackhole | 9 |
| `misconfiguration` | `http_acl_block` | http_acl_block | 13 |
| `misconfiguration` | `icmp_acl_block` | icmp_acl_block | 28 |
| `misconfiguration` | `k8s_coredns_isolated` | Applications cannot resolve Kubernetes service names such as *.svc.cluster.local and report DNS timeouts, while communication by IP address keeps working. The CoreDNS pods are Running and Ready and the DNS Service still lists its endpoints. | 2 |
| `misconfiguration` | `k8s_networkpolicy_deny` | Only the pods selected by a NetworkPolicy lose inbound connectivity while sibling routes and the rest of the cluster stay healthy. | 0 |
| `misconfiguration` | `k8s_worker_apiserver_partition` | One Kubernetes worker node reports NotReady and stops receiving new pods, and `kubectl exec` / `kubectl logs` time out for the pods it hosts, while those pods keep serving traffic and the node itself is still reachable over the network. | 2 |
| `misconfiguration` | `mac_address_conflict` | mac_address_conflict | 28 |
| `misconfiguration` | `ospf_acl_block` | ospf_acl_block | 6 |
| `misconfiguration` | `ospf_area_misconfiguration` | ospf_area_misconfiguration | 6 |
| `misconfiguration` | `ospf_neighbor_missing` | ospf_neighbor_missing | 6 |
| `network_node_error` | `flow_rule_loop` | flow_rule_loop | 6 |
| `network_node_error` | `flow_rule_shadowing` | flow_rule_shadowing | 6 |
| `network_node_error` | `frr_service_down` | Users report connectivity issues to other hosts in the network. | 17 |
| `network_node_error` | `k8s_clusterip_routing_broken` | Pods scheduled on one Kubernetes node cannot reach any ClusterIP Service, including in-cluster DNS, while direct pod-IP traffic from the same node still works. Services, endpoints and pods all report healthy, and the node stays Ready. | 2 |
| `network_node_error` | `mpls_label_limit_exceeded` | mpls_label_limit_exceeded | 1 |
| `network_node_error` | `p4_aggressive_detection_thresholds` | p4_aggressive_detection_thresholds | 1 |
| `network_node_error` | `p4_compilation_error_parser_state` | p4_compilation_error_parser_state | 4 |
| `network_node_error` | `p4_header_definition_error` | p4_header_definition_error | 4 |
| `network_node_error` | `p4_table_entry_misconfig` | p4_table_entry_misconfig | 4 |
| `network_node_error` | `p4_table_entry_missing` | p4_table_entry_missing | 4 |
| `network_node_error` | `sdn_controller_crash` | sdn_controller_crash | 6 |
| `network_node_error` | `southbound_port_block` | southbound_port_block | 6 |
| `network_node_error` | `southbound_port_mismatch` | southbound_port_mismatch | 6 |
| `network_under_attack` | `arp_cache_poisoning` | arp_cache_poisoning | 28 |
| `network_under_attack` | `bgp_hijacking` | bgp_hijacking | 9 |
| `network_under_attack` | `dhcp_spoofed_dns` | Some hosts can not access webservices. | 3 |
| `network_under_attack` | `dhcp_spoofed_gateway` | dhcp_spoofed_gateway | 3 |
| `network_under_attack` | `dhcp_spoofed_subnet` | dhcp_spoofed_subnet | 3 |
| `network_under_attack` | `web_dos_attack` | Users reports high latency when accessing some web services. | 13 |
| `resource_contention` | `dns_lookup_latency` | Users experience high latency when accessing web services. | 6 |
| `resource_contention` | `incast_traffic_network_limitation` | incast_traffic_network_limitation | 13 |
| `resource_contention` | `link_bandwidth_throttling` | link_bandwidth_throttling | 29 |
| `resource_contention` | `link_high_packet_corruption` | link_high_packet_corruption | 29 |
| `resource_contention` | `load_balancer_overload` | load_balancer_overload | 3 |
| `resource_contention` | `receiver_resource_contention` | receiver_resource_contention | 13 |
| `resource_contention` | `sender_application_delay` | sender_application_delay | 13 |
| `resource_contention` | `sender_resource_contention` | sender_resource_contention | 13 |
| **Total** | - | - | **708** |

See [Creating benchmark tasks](creating-benchmark-tasks.md) to add a new failure type, and `nika failure describe <problem_id>` for the required inject parameters of any entry above.
