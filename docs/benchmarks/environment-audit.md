# Environment audit

This reference lists every case in benchmark release 0.2.0.
A case is the scenario, scale, backend, design options, fault, and inject parameters.

A running benchmark trial and a full audit use different checks.

The trial calls `startup_verify_lab` when the lab starts and `verify_fault` after inject.
While the agent runs, and again before NIKA removes the lab, `PresenceWatch` reads the fault artifact on the problem instance that injected the fault.
The trial writes those reads to `fault-presence.json`.
A `present` result means the artifact was still on the lab.
The full audit records the network effect.
When the artifact is absent or the read fails, the trial outcome is `environment_invalid`.
Leaderboard averages omit that outcome.
`nika benchmark run --resume` deletes the slot and runs it again.

Faults whose effect is a live worker, flap, queue, or quota are listed in `DYNAMIC_ARTIFACT_FAULTS`.
The recheck reads that worker, queue, or quota on the injected instance.

Run the full audit through `audit_case` in `experiment/audit/live.py`.
To audit all release cases, run `uv run python -m experiment.audit.matrix --jobs 2`. Add `--retry-failed` after fixing a failed check or fault.
The matrix writes one JSON record per case to `runtime/environment-audit-results/`. Those records stay local; this page is the committed summary.
Benchmark runs stay on `startup_verify_lab`, `verify_fault`, and `PresenceWatch`.
For one selected case, `audit_case` deploys a lab and runs `verify_lab` plus a healthy probe of the fault path before inject.
After inject it runs `verify_fault`, the symptom probe, and a control-path observation.
`window_for` chooses how long that fault stays in place before the next read.
After that wait, `audit_case` reads the artifact and the symptom probe again.
Those two reads run once more before `audit_case` undeploys the session it created.
A case with fault `healthy` runs `verify_lab` before the window and again before cleanup.
For faults without a targeted symptom probe, the full audit compares the scenario's health checks before and after injection and requires the same regression to persist.
Each record includes the audit method version, Git commit and dirty state, source and effective configuration hashes, observation timestamps, session id, and the image id and repository digests for each lab node.
The matrix and report reject records with missing provenance, changed source or configuration, changed installed images, or an older audit method. These cases stay `not_run` until audited again.
Use `--force` to rerun every selected case even when its stored result is current. The matrix exits nonzero if any selected case lacks a current passing result.
The persistence window is a short repeated observation, not a measurement across the full 2400-second trial budget. Dynamic injectors must keep their workers alive through that budget; `PresenceWatch` checks artifacts during the actual benchmark trial.
The P4 gateway ECN probe measures packet marks from a virtual queue with a drain rate of about 61 packets per second. It does not measure physical egress queue congestion. The `queue_occupancy` register in that scenario reports the modeled depth.

## Admission

A case is admitted only when `admission` is `pass`.
A separate control path is recorded when one exists. `no_control_path` is advisory; a failed control path still fails admission.

| Status | Meaning |
| --- | --- |
| `pass` | Every required stage observed the expected condition. An unavailable sibling control path is advisory. |
| `fail` | A stage ran and the observation failed. |
| `skipped` | The audit ran and skipped the stage. |
| `unsupported` | This fault has no behavioral check for the stage. `reason` names the scope. |
| `no_evidence` | The stage produced no observation. An `artifact_only` symptom result is `no_evidence`. |
| `not_run` | No current full-audit result is available for this case. |

`fail`, `skipped`, `unsupported`, `no_evidence`, and `not_run` do not admit a case.

## Regenerate this page

From the repository root:

```shell
uv run python -c "from experiment.audit.report_doc import write_environment_audit_doc; write_environment_audit_doc()"
```

## Coverage

Release 0.2.0 has 169 cases (dev 84, test 85).
Admitted cases: 0.
20 cases declare an `artifact_only` symptom probe.
Their full audit uses a scenario health-check delta; a check that stays healthy does not prove fault effect.

| Status | Cases |
| --- | --- |
| `pass` | 0 |
| `fail` | 0 |
| `skipped` | 0 |
| `unsupported` | 0 |
| `no_evidence` | 0 |
| `not_run` | 169 |

Symptom probes declared for these cases:

| Probe | Cases |
| --- | --- |
| `artifact_only` | 20 |
| `bgp_hijack_route` | 2 |
| `control_plane_bgp` | 7 |
| `control_plane_ospf` | 6 |
| `control_plane_routing` | 2 |
| `custom` | 56 |
| `dns_answer` | 2 |
| `healthy` | 18 |
| `http_by_name` | 4 |
| `isolation_http` | 4 |
| `path_http` | 14 |
| `path_mtu_frag_needed` | 2 |
| `path_ping` | 28 |
| `ping_old_ip` | 2 |
| `route_get_onlink` | 2 |

## Executed audits

Each row is one live `audit_case` run for a concrete case identity.
The release table below changes only when that run has the same scenario, scale, backend, design, fault, and inject parameters.

A diagnosis that starts with `verify` names the check or the host prerequisite.
A diagnosis that starts with `case` names the fault symptom on that lab.

No current live audit result is stored yet.


## Cases

### `enterprise_branch`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | arp_acl_block | s | scenario default | none | host_name=br1_corp_pc | path_ping | not_run |
| dev | host_incorrect_netmask | m | scenario default | none | host_name=br1_corp_pc, netmask_prefix=8 | route_get_onlink | not_run |
| dev | mtu_mismatch | l | scenario default | none | host_name=br1_edge, intf_name=eth2, mtu=500 | path_mtu_frag_needed | not_run |
| dev | nat_mapping_removed_without_drain | s | scenario default | none | host_name=br1_edge, nat_ip_a=198.18.1.10, nat_ip_b=198.18.1.11, source_prefix=10.1.40.0/24, wan_interface=eth2 | custom | not_run |
| dev | snat_port_pool_exhaustion | m | scenario default | none | host_name=br1_edge, port_end=40063, port_start=40000, public_ip=198.18.1.10, source_prefix=10.1.40.0/24 | custom | not_run |
| dev | tcp_receive_window_limited | l | scenario default | none | host_name=br1_corp_pc2, large_url=http://10.0.20.2/large.bin, sender_host=hq_srv, sender_ip=10.0.20.2, small_url=http://10.0.20.2/small.bin | custom | not_run |
| dev | vrf_dscp_remarking | s | scenario default | none | corp_prefix=10.0.10.0/24, direction=lan_to_overlay, dst_host=br1_corp_pc, host_name=hq_edge, intf_name=wg_br1, src_host=hq_corp_pc | custom | not_run |
| dev | wireguard_allowed_ips_misconfiguration | l | scenario default | none | host_name=br1_edge, intf_name=wg_hq, target_prefix=10.0.20.0/24 | path_ping | not_run |
| dev | wireguard_peer_key_misconfiguration | s | scenario default | none | host_name=br1_edge, intf_name=wg_hq | path_ping | not_run |
| test | bgp_hijacking | l | scenario default | none | host_name=br1_edge | bgp_hijack_route | not_run |
| test | bgp_missing_route_advertisement | m | scenario default | none | host_name=br1_edge | path_ping | not_run |
| test | host_static_blackhole | l | scenario default | none | host_name=br1_edge | path_ping | not_run |
| test | link_down | l | scenario default | none | host_name=br1_corp_pc, intf_name=eth0 | path_ping | not_run |
| test | link_packet_corruption | m | scenario default | none | corruption_percentage=10, host_name=br1_edge, intf_name=eth3, observer_device=br1_corp_pc, probe_dst_ip=10.0.20.2 | custom | not_run |
| test | nat_mapping_removed_without_drain | l | scenario default | none | host_name=br1_edge, nat_ip_a=198.18.1.10, nat_ip_b=198.18.1.11, source_prefix=10.1.40.0/24, wan_interface=eth3 | custom | not_run |
| test | snat_port_pool_exhaustion | s | scenario default | none | host_name=br1_edge, port_end=40063, port_start=40000, public_ip=198.18.1.10, source_prefix=10.1.40.0/24 | custom | not_run |
| test | tcp_receive_window_limited | s | scenario default | none | host_name=br1_corp_pc, large_url=http://10.0.20.2/large.bin, sender_host=hq_srv, sender_ip=10.0.20.2, small_url=http://10.0.20.2/small.bin | custom | not_run |
| test | vrf_dscp_remarking | m | scenario default | none | corp_prefix=10.0.10.0/24, direction=lan_to_overlay, dst_host=br1_corp_pc, host_name=hq_edge, intf_name=wg_br1, src_host=hq_corp_pc | custom | not_run |
| test | wireguard_allowed_ips_misconfiguration | s | scenario default | none | host_name=br1_edge, intf_name=wg_hq, target_prefix=10.0.20.0/24 | path_ping | not_run |
| test | wireguard_peer_key_misconfiguration | l | scenario default | none | host_name=br1_edge, intf_name=wg_hq | path_ping | not_run |
| test | healthy | m | scenario default | none | none | healthy | not_run |

### `k8s_lab`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | arp_cache_poisoning | none | scenario default | none | host_name=client | path_ping | not_run |
| dev | frr_service_down | none | scenario default | none | host_name=leaf_1_1 | control_plane_routing | not_run |
| dev | host_incorrect_gateway | none | scenario default | none | host_name=client | path_ping | not_run |
| dev | k8s_coredns_isolated | none | scenario default | none | control_node=controller, symptom_host=worker1 | isolation_http | not_run |
| test | bgp_asn_misconfig | none | scenario default | none | host_name=leaf_1_1 | control_plane_bgp | not_run |
| test | host_incorrect_netmask | none | scenario default | none | host_name=client, netmask_prefix=8 | route_get_onlink | not_run |
| test | k8s_clusterip_routing_broken | none | scenario default | none | control_node=controller, node_name=controller | custom | not_run |
| test | k8s_networkpolicy_deny | none | scenario default | none | control_node=controller, control_url=http://datacenter.com/weather?location=London, namespace=word-ns, pod_selector=app=word, symptom_host=client, symptom_url=http://datacenter.com/word | isolation_http | not_run |
| test | k8s_worker_apiserver_partition | none | scenario default | none | control_node=controller, node_name=worker1 | artifact_only | not_run |
| test | healthy | none | scenario default | none | none | healthy | not_run |

### `isp_nobel-eu`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_acl_block | m | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=amsterdam | control_plane_bgp | not_run |

### `isp_cost266`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_asn_misconfig | l | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=amsterdam | control_plane_bgp | not_run |

### `isp_abilene_ebgp_rtbh`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_blackhole_community_leak | s | kathara | device_profile=frr | host_name=kscyng, peer_host=pc_kscyng, probe_dst_ip=198.51.100.1, symptom_host=iplsng | path_ping | not_run |

### `isp_janos-us`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_hijacking | m | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=albany, peer_host=pc_albany, probe_dst_ip=198.18.0.1, symptom_host=atlanta, target_network=198.18.0.0/24 | bgp_hijack_route | not_run |

### `isp_geant`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_max_prefix_exceeded | m | kathara | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=frr | flood_count=120, neighbor_ip=10.0.0.35, peer_name=nl1_nl, receiver_name=de1_de | control_plane_bgp | not_run |

### `isp_pioro40`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_missing_route_advertisement | l | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=n7, peer_host=pc_n7, prefix=203.0.113.0/24, probe_dst_ip=203.0.113.1, symptom_host=n0 | path_ping | not_run |

### `isp_abilene_ebgp_rpki`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_rpki_invalid_route_leak | s | kathara | device_profile=frr | host_name=kscyng | path_ping | not_run |

### `p4_dc_fabric`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bmv2_switch_down | l | scenario default | none | host_name=leaf_1 | path_http | not_run |
| dev | host_missing_ip | s | scenario default | none | host_name=client_3_1, intf_name=eth0 | path_ping | not_run |
| dev | mac_address_conflict | m | scenario default | none | host_name=web_2, host_name_2=client_3_2 | custom | not_run |
| dev | p4_action_selector_member_misconfig | m | scenario default | none | host_name=leaf_1 | artifact_only | not_run |
| dev | p4_ecmp_group_member_missing | l | scenario default | none | host_name=leaf_1 | artifact_only | not_run |
| dev | p4_table_entry_misconfig | m | scenario default | none | host_name=leaf_1, observer_device=client_1_1, probe_dst_ip=10.0.2.11 | path_http | not_run |
| dev | p4_table_resource_exhaustion | s | scenario default | none | host_name=leaf_1 | artifact_only | not_run |
| dev | p4runtime_partial_write | l | scenario default | none | host_name=leaf_1 | path_http | not_run |
| dev | p4runtime_pipeline_mismatch | s | scenario default | none | host_name=leaf_1 | path_ping | not_run |
| test | incast_traffic_network_limitation | l | scenario default | none | duration=3600, host_name=web_2, observer_device=client_1_1, probe_dst_ip=10.0.2.11 | custom | not_run |
| test | p4_table_entry_missing | m | scenario default | none | host_name=leaf_1, observer_device=client_1_1, probe_dst_ip=10.0.2.11 | path_http | not_run |

### `sdn_l3_clos`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | device_forwarding_packet_corruption | s | scenario default | none | forwarding_device=leaf_2, intf_name=eth0, observer_device=client_1_1, probe_dst_ip=10.0.2.11, seed=42 | custom | not_run |
| dev | flow_rule_loop | m | scenario default | none | host_name=leaf_1, host_name_2=spine_2, port_name=eth5, port_name_2=eth5 | custom | not_run |
| dev | flow_rule_shadowing | l | scenario default | none | host_name=spine_1 | custom | not_run |
| dev | sdn_controller_crash | m | scenario default | none | host_name=onos | artifact_only | not_run |
| dev | sender_resource_contention | l | scenario default | none | client_host=client_2_1, cpu_quota=0.05, dst_ip=10.0.1.11, duration=3600, host_name=web_1, large_url=http://10.0.1.11/large.bin, small_url=http://10.0.1.11/small.bin, stress_cpus=16 | custom | not_run |
| dev | southbound_port_block | l | scenario default | none | host_name=onos, southbound_port=6653 | custom | not_run |
| dev | southbound_port_mismatch | s | scenario default | none | host_name=onos, mismatched_port=6633, original_port=6653 | custom | not_run |
| test | arp_cache_poisoning | l | scenario default | none | host_name=client_10_1 | path_ping | not_run |
| test | flow_rule_loop | s | scenario default | none | host_name=leaf_1, host_name_2=spine_1, port_name=eth2, port_name_2=eth2 | custom | not_run |
| test | flow_rule_shadowing | s | scenario default | none | host_name=spine_1 | custom | not_run |
| test | link_flap | s | scenario default | none | down_time=1, host_name=client_1_1, intf_name=eth0, observer_device=client_1_1, probe_dst_ip=10.0.2.11, up_time=1 | custom | not_run |
| test | sdn_controller_crash | l | scenario default | none | host_name=onos | artifact_only | not_run |
| test | southbound_port_block | m | scenario default | none | host_name=onos, southbound_port=6653 | custom | not_run |
| test | southbound_port_mismatch | l | scenario default | none | host_name=onos, mismatched_port=6633, original_port=6653 | custom | not_run |
| test | web_dos_attack | s | scenario default | none | attacker_device=client_4_1, host_name=web_2, observer_device=client_1_1, probe_url=http://10.0.2.11/ | custom | not_run |
| test | healthy | m | scenario default | none | none | healthy | not_run |
| test | healthy | l | scenario default | none | none | healthy | not_run |

### `campus_lan`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | dhcp_missing_subnet | m | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | artifact_only | not_run |
| dev | dhcp_service_down | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | artifact_only | not_run |
| dev | dhcp_spoofed_dns | s | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | artifact_only | not_run |
| dev | dhcp_spoofed_gateway | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | artifact_only | not_run |
| dev | dhcp_spoofed_subnet | m | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | artifact_only | not_run |
| dev | load_balancer_overload | s | scenario default | none | backend_cpu_host=backend_web_0, backend_probe_host=load_balancer, backend_url=http://20.200.0.2/small, client_host=pc_1_1_1_1, concurrency=200, control_url=http://web0.local/small, cpu_quota=0.2, duration_sec=300, host_name=load_balancer, load_client_hosts=pc_2_1_1_1, load_workers=4, probe_concurrency=4, probe_requests=60, vip_url=http://web99.local/small, warmup_sec=5 | custom | not_run |
| test | device_forwarding_packet_corruption | l | scenario default | none | forwarding_device=router_core_2, intf_name=eth5, observer_device=pc_1_1_1_1, probe_dst_ip=10.200.0.3, seed=42 | custom | not_run |
| test | dhcp_missing_subnet | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | artifact_only | not_run |
| test | dhcp_service_down | m | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | artifact_only | not_run |
| test | dhcp_spoofed_dns | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | artifact_only | not_run |
| test | dhcp_spoofed_gateway | s | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | artifact_only | not_run |
| test | dhcp_spoofed_subnet | s | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | artifact_only | not_run |
| test | dns_lookup_latency | m | scenario default | none | delay_ms=1000, host_name=dns_server, intf_name=eth0 | http_by_name | not_run |
| test | dns_port_blocked | l | scenario default | none | host_name=dns_server | path_http | not_run |
| test | dns_record_error | m | scenario default | none | host_name=dns_server, target_domain=local, target_website=web1 | dns_answer | not_run |
| test | dns_service_down | m | scenario default | none | host_name=dns_server | path_http | not_run |
| test | host_incorrect_dns | m | scenario default | none | host_name=pc_1_1_1_1 | http_by_name | not_run |
| test | load_balancer_overload | l | scenario default | none | backend_cpu_host=backend_web_0, backend_probe_host=load_balancer, backend_url=http://20.200.0.2/small, client_host=pc_1_1_1_1, concurrency=200, control_url=http://web0.local/small, cpu_quota=0.2, duration_sec=300, host_name=load_balancer, load_client_hosts=pc_2_1_1_1, load_workers=4, probe_concurrency=4, probe_requests=60, vip_url=http://web99.local/small, warmup_sec=5 | custom | not_run |
| test | ospf_acl_block | s | scenario default | none | host_name=router_core_1 | control_plane_ospf | not_run |
| test | ospf_area_misconfiguration | m | scenario default | none | host_name=router_core_1 | control_plane_ospf | not_run |
| test | ospf_neighbor_missing | l | scenario default | none | host_name=router_core_1 | control_plane_ospf | not_run |

### `dc_clos`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | dns_lookup_latency | s | scenario default | none | delay_ms=1000, host_name=dns_pod0, intf_name=eth0 | http_by_name | not_run |
| dev | dns_port_blocked | m | scenario default | none | host_name=dns_pod0 | path_http | not_run |
| dev | dns_record_error | l | scenario default | none | host_name=dns_pod0, target_domain=pod0, target_website=web0 | dns_answer | not_run |
| dev | dns_service_down | s | scenario default | none | host_name=dns_pod0 | path_http | not_run |
| dev | host_incorrect_dns | s | scenario default | none | host_name=client_0 | http_by_name | not_run |
| dev | web_dos_attack | m | scenario default | none | attacker_device=client_0, host_name=webserver0_pod0, observer_device=dns_pod0, probe_url=http://10.0.1.2/small.bin | custom | not_run |
| test | arp_acl_block | m | scenario default | none | host_name=client_0 | path_ping | not_run |
| test | host_incorrect_gateway | l | scenario default | none | host_name=client_0 | path_ping | not_run |
| test | link_detach | m | scenario default | none | host_name=client_0, intf_name=eth0 | path_ping | not_run |
| test | receiver_resource_contention | s | scenario default | none | duration=3600, host_name=client_0 | custom | not_run |
| test | healthy | m | scenario default | none | none | healthy | not_run |
| test | healthy | l | scenario default | none | none | healthy | not_run |
| test | healthy | s | scenario default | none | none | healthy | not_run |

### `llmd_lab`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | host_incorrect_ip | none | scenario default | none | host_name=client | ping_old_ip | not_run |
| dev | http_acl_block | none | scenario default | none | host_name=client | path_http | not_run |
| dev | k8s_clusterip_routing_broken | none | scenario default | none | control_node=controller, node_name=controller | custom | not_run |
| dev | k8s_networkpolicy_deny | none | scenario default | none | control_node=controller, control_url=http://200.0.0.8/, namespace=llm-d, pod_selector=gateway.networking.k8s.io/gateway-name=llm-d-gateway, symptom_host=client, symptom_url=http://llmd/v1/models | isolation_http | not_run |
| dev | k8s_worker_apiserver_partition | none | scenario default | none | control_node=controller, node_name=worker1 | artifact_only | not_run |
| dev | receiver_resource_contention | none | scenario default | none | duration=3600, host_name=client | custom | not_run |
| test | host_ip_conflict | none | scenario default | none | host_name=client, host_name_2=web | custom | not_run |
| test | host_missing_ip | none | scenario default | none | host_name=client, intf_name=eth0 | path_ping | not_run |
| test | k8s_coredns_isolated | none | scenario default | none | control_node=controller, symptom_host=worker1 | isolation_http | not_run |
| test | sender_resource_contention | none | scenario default | none | client_host=client, cpu_quota=0.05, dst_ip=200.0.0.8, duration=3600, host_name=web, large_url=http://200.0.0.8/large.bin, small_url=http://200.0.0.8/small.bin, stress_cpus=16 | custom | not_run |
| test | healthy | none | scenario default | none | none | healthy | not_run |

### `p4_dc_gateway`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | host_ip_conflict | l | scenario default | none | host_name=service_4_2, host_name_2=service_2_1 | custom | not_run |
| dev | incast_traffic_network_limitation | m | scenario default | none | duration=3600, host_name=service_1_1, observer_device=client_1, probe_dst_ip=10.0.1.11 | custom | not_run |
| dev | int_insufficient_mtu_headroom | s | scenario default | none | bmv2_port=2, host_name=gateway_1, int_mtu=1480, intf_name=eth1 | custom | not_run |
| dev | lb_connection_state_exhaustion | l | scenario default | none | attacker_device=client_8, backend_dip=10.0.1.11, capacity=256, client_host=client_1, host_name=gateway_1, seed=42, syn_timeout_sec=10, vip_url=http://20.0.0.1:80/ | custom | not_run |
| dev | lb_pending_connection_update_race | m | scenario default | none | host_name=gateway_1, learning_delay_ms=5, seed=42 | custom | not_run |
| dev | p4_ecn_threshold_misconfiguration | s | scenario default | none | bmv2_port=2, host_name=gateway_1, intf_name=eth1, threshold=1024 | custom | not_run |
| dev | p4_table_entry_missing | l | scenario default | none | host_name=gateway_1 | path_http | not_run |
| dev | p4_tcam_entry_corruption | m | scenario default | none | control_source=client_2, host_name=gateway_3, target_ip=10.0.4.11 | custom | not_run |
| dev | silent_egress_packet_loss | s | scenario default | none | bmv2_port=2, host_name=gateway_1, intf_name=eth1, loss_basis_points=200, seed=42 | custom | not_run |
| dev | tcp_syn_flood_attack | m | scenario default | none | attacker_device=client_1, duration=3600, flows=100, rate_pps=1000, seed=42, target_ip=10.0.1.12, target_port=80 | custom | not_run |
| test | bmv2_switch_down | s | scenario default | none | host_name=gateway_1 | path_http | not_run |
| test | host_incorrect_ip | s | scenario default | none | host_name=client_1 | ping_old_ip | not_run |
| test | http_acl_block | m | scenario default | none | host_name=client_1 | path_http | not_run |
| test | icmp_acl_block | s | scenario default | none | host_name=client_1 | path_ping | not_run |
| test | icmp_frag_needed_filter_misconfiguration | m | scenario default | none | host_name=gateway_1 | custom | not_run |
| test | int_insufficient_mtu_headroom | m | scenario default | none | bmv2_port=2, host_name=gateway_1, int_mtu=1480, intf_name=eth1 | custom | not_run |
| test | lb_connection_state_exhaustion | s | scenario default | none | attacker_device=client_2, backend_dip=10.0.1.11, capacity=256, client_host=client_1, host_name=gateway_1, seed=42, syn_timeout_sec=10, vip_url=http://20.0.0.1:80/ | custom | not_run |
| test | lb_pending_connection_update_race | l | scenario default | none | host_name=gateway_1, learning_delay_ms=5, seed=42 | custom | not_run |
| test | mac_address_conflict | s | scenario default | none | host_name=service_1_2, host_name_2=client_1 | custom | not_run |
| test | p4_action_selector_member_misconfig | s | scenario default | none | host_name=leaf_1 | artifact_only | not_run |
| test | p4_ecmp_group_member_missing | m | scenario default | none | host_name=leaf_1 | artifact_only | not_run |
| test | p4_ecn_threshold_misconfiguration | l | scenario default | none | bmv2_port=10, host_name=spine_1, intf_name=eth9, threshold=1024 | custom | not_run |
| test | p4_table_entry_misconfig | s | scenario default | none | host_name=leaf_1 | path_http | not_run |
| test | p4_table_resource_exhaustion | l | scenario default | none | host_name=leaf_1 | artifact_only | not_run |
| test | p4_tcam_entry_corruption | s | scenario default | none | control_source=client_2, host_name=spine_1, target_ip=10.0.1.12 | custom | not_run |
| test | p4runtime_partial_write | m | scenario default | none | host_name=leaf_1 | path_http | not_run |
| test | p4runtime_pipeline_mismatch | l | scenario default | none | host_name=gateway_1 | path_ping | not_run |
| test | silent_egress_packet_loss | m | scenario default | none | bmv2_port=2, host_name=gateway_1, intf_name=eth1, loss_basis_points=200, seed=42 | custom | not_run |
| test | tcp_syn_flood_attack | l | scenario default | none | attacker_device=client_1, duration=3600, flows=100, rate_pps=1000, seed=42, target_ip=10.0.4.12, target_port=80 | custom | not_run |
| test | healthy | m | scenario default | none | none | healthy | not_run |

### `isp_geant_ebgp_rpki`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | host_static_blackhole | m | kathara | device_profile=frr | host_name=at1_at | path_ping | not_run |
| test | bgp_rpki_invalid_route_leak | m | kathara | device_profile=frr | host_name=es1_es | path_ping | not_run |
| test | mtu_mismatch | m | kathara | device_profile=frr | host_name=at1_at, intf_name=eth0, mtu=500 | path_mtu_frag_needed | not_run |

### `isp_janos-us-ca`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | icmp_acl_block | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=pc_atlanta | path_ping | not_run |

### `isp_abilene`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | icmp_frag_needed_filter_misconfiguration | s | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=atlam5 | custom | not_run |
| test | bgp_max_prefix_exceeded | s | kathara | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=frr | flood_count=120, neighbor_ip=10.0.0.21, peer_name=losang, receiver_name=hstnng | control_plane_bgp | not_run |

### `min3clos`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | link_capacity_bottleneck | none | scenario default | none | burst=64kb, host_name=client1, intf_name=eth1, limit=500kb, rate=30kbit | custom | not_run |
| dev | healthy | none | scenario default | none | none | healthy | not_run |
| test | bgp_asn_misconfig | none | scenario default | none | host_name=leaf1 | control_plane_bgp | not_run |

### `isp_ta2`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | link_detach | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=n10, intf_name=eth0, peer_host=pc_n2, probe_dst_ip=10.254.0.46, symptom_host=pc_n10 | path_ping | not_run |

### `isp_dfn-bwin`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | link_down | s | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=berlin, intf_name=eth0, peer_host=pc_frankfurt, probe_dst_ip=10.254.0.6, symptom_host=pc_berlin | path_ping | not_run |
| dev | healthy | s | containerlab | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=nokia_srlinux | none | healthy | not_run |

### `isp_nobel-germany`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | link_flap | m | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | down_time=1, host_name=berlin, intf_name=eth0, peer_host=pc_hamburg, probe_dst_ip=10.254.0.26, symptom_host=pc_berlin, up_time=1 | custom | not_run |

### `isp_india35`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | link_packet_corruption | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | corruption_percentage=10, host_name=n_0, intf_name=eth0, peer_host=pc_n_24, probe_dst_ip=10.254.0.70, symptom_host=pc_n_0 | custom | not_run |

### `isp_ta1`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | ospf_acl_block | m | kathara | igp=ospf, bgp_mode=none, rpki=False, device_profile=frr | host_name=n1 | control_plane_ospf | not_run |

### `isp_germany50`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | ospf_area_misconfiguration | l | kathara | igp=ospf, bgp_mode=none, rpki=False, device_profile=frr | host_name=aachen | control_plane_ospf | not_run |
| test | frr_service_down | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=aachen | control_plane_routing | not_run |

### `isp_dfn-bwin_ebgp_rtbh`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | ospf_neighbor_missing | s | kathara | device_profile=frr | host_name=berlin | control_plane_ospf | not_run |
| test | bgp_blackhole_community_leak | s | kathara | device_profile=frr | host_name=frankfurt, peer_host=pc_frankfurt, probe_dst_ip=198.51.100.1, symptom_host=hamburg | path_ping | not_run |

### `isp_di-yuan`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | healthy | s | containerlab | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=nokia_srlinux | none | healthy | not_run |
| dev | healthy | s | containerlab | igp=isis, bgp_mode=none, rpki=False, device_profile=nokia_srlinux | none | healthy | not_run |
| dev | healthy | s | containerlab | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=nokia_srlinux | none | healthy | not_run |
| test | link_capacity_bottleneck | s | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | burst=64kb, host_name=n_10, intf_name=eth0, limit=500kb, peer_host=pc_n_9, probe_dst_ip=10.254.0.42, rate=30kbit, symptom_host=pc_n_10 | custom | not_run |

### `isp_pdh`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | healthy | s | containerlab | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=nokia_srlinux | none | healthy | not_run |
| dev | healthy | s | containerlab | igp=isis, bgp_mode=none, rpki=False, device_profile=nokia_srlinux | none | healthy | not_run |
| dev | healthy | s | containerlab | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=nokia_srlinux | none | healthy | not_run |
| test | bgp_acl_block | s | containerlab | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=nokia_srlinux | host_name=n1 | control_plane_bgp | not_run |

### `isp_dfn-gwin`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | healthy | s | containerlab | igp=isis, bgp_mode=none, rpki=False, device_profile=nokia_srlinux | none | healthy | not_run |

Rows with admission `not_run` are coverage gaps for the full audit.
