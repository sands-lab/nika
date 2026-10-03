# Benchmark audit

The benchmark audit deploys every case in benchmark release 0.2.0 in a real NIKA lab, injects the fault, and checks that the fault produces its declared network symptom and stays in place.
Use this page to check whether a release case is valid before you score agents on it, and to rerun the audit after you change a scenario, fault, or symptom check.

A case is one scenario, scale, backend, design options, fault, and inject parameters.
Release cases come from `benchmark/releases/<version>/dev.yaml` and `test.yaml`.

## Result

Release 0.2.0 has 169 cases (dev 84, test 85).
Admitted cases: 169.

| Status | Cases |
| --- | --- |
| `pass` | 169 |
| `fail` | 0 |
| `skipped` | 0 |
| `unsupported` | 0 |
| `no_evidence` | 0 |
| `not_run` | 0 |

Each case declares one symptom probe. The probe decides what the audit measures after inject.
20 cases declare `artifact_only`. For those, the audit compares the scenario health checks before and after inject and requires the same checks to stay failed. A health check that stays green does not prove the fault had an effect.

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

## Test environment

The release 0.2.0 audit ran on one host. Other NIKA workloads shared that host, so the matrix admits a case only when enough memory is free (see [Launch parameters](#launch-parameters)).

| Component | Version |
| --- | --- |
| OS | Ubuntu 24.04.5 LTS, Linux 6.8.0-142-generic |
| CPU | 16 vCPU |
| Memory | 62 GiB |
| Docker Engine | 29.8.1 |
| Containerlab | 0.79.0 |
| Kathara | 3.8.3 |
| Python | 3.12.12, run through `uv` |

The stored records span 2026-10-02 11:06 to 2026-10-03 11:03 UTC.
Each case took 4.4 minutes at the median and 15.0 minutes at most, from lab deploy to undeploy.

Each record stores the NIKA commit it ran on. A case keeps its result until a change to that case requires a rerun, so records come from several commits:

| Commit | Cases |
| --- | --- |
| `6476b56` | 103 |
| `b4f9e8f` | 17 |
| `56bbc59` | 14 |
| `03b9251` | 9 |
| `3fc4adf` | 7 |
| `d84c44c` | 4 |
| `97f6bab` | 4 |
| `07ed124` | 3 |
| `0956260` | 2 |
| `273307e` | 2 |
| `5642b9c` | 2 |
| `33976a4` | 1 |
| `742306f` | 1 |

48 records ran with uncommitted changes in the working tree. Their `source_sha256` field identifies the exact source.

Lab nodes used these images. Every node that used an image reference had the same image ID.

| Image | Image ID |
| --- | --- |
| `ghcr.io/nokia/srlinux:24.10` | `20064faa8c2f` |
| `kathara/p4` | `62d451190853` |
| `kathara/sdn` | `19cb5b383367` |
| `nika/base` | `132582cff090` |
| `nika/fabric-controller` | `5d7df206543e` |
| `nika/frr` | `1251bceb0f1d` |
| `nika/nginx` | `45f3b48aa38c` |
| `nika/onos` | `26cd5a38aeef` |
| `nika/routinator:v0.14.2` | `3211e28488cd` |
| `rancher/k3s:v1.34.1-k3s1@sha256:5e0707cfd1239b358ef73f3254bc3eadc027dd30cd5ec6ca41e29e47652a1b8c` | `5e0707cfd123` |
| `wbitt/network-multitool` | `db2810fe2c8d` |

## What the audit checks for one case

`audit_case` in `experiment/audit/live.py` audits one case in this order:

1. Deploy the lab with `start_net_env`, using the case scenario, scale, backend, and design options.
2. Run the scenario health checks (`verify_lab`). This is stage `baseline_lab`.
3. Probe the fault path while the lab is healthy. This is stage `baseline_path`. It shows that the symptom probe sees a working path before inject.
4. Inject the fault with the case inject parameters and run `verify_fault`. This is stage `inject_artifact`.
5. Run the declared symptom probe (stage `symptom`) and a control path that avoids the root cause (stage `control_path`).
6. Wait for the persistence window, then read the fault artifact and the symptom again (stages `persistence_artifact` and `persistence_symptom`).
7. Read the artifact and the symptom once more (stages `final_artifact` and `final_symptom`).
8. Undeploy the lab with `close_session`, even when a stage fails.

A `healthy` case runs `verify_lab` before and after a 2-second window and skips steps 3 to 7.

The control path starts from a host that is not a root-cause node, a host behind a root-cause interface or link, or an attacker or load generator named in the inject parameters.
When no such path exists, the stage records `no_control_path`. That status does not block admission. A control path that exists and fails does block admission.

A stage with an empty observation is `no_evidence`. A stage whose observation reports an error, such as a command timeout, is `fail`. Neither status admits a case.

### Admission statuses

The audit admits a case only when its admission status is `pass`.

| Status | Meaning |
| --- | --- |
| `pass` | Every required stage observed the expected condition. A missing control path does not block `pass`. |
| `fail` | A stage ran and the observation failed. |
| `skipped` | The audit ran and skipped the stage. |
| `unsupported` | The fault has no behavioral check for the stage. `reason` names the scope. |
| `no_evidence` | The stage produced no observation. An `artifact_only` symptom result is `no_evidence`. |
| `not_run` | No complete stored result exists for this case. |

A stored result counts only when it has a finished run, a Git commit, image identities for every lab node, and the symptom probe that the fault declares today. Otherwise the case shows `not_run`.

## Launch parameters

Run every command from the repository root on a host with Docker and the NIKA images built.

The release 0.2.0 audit used these commands:

```shell
# Audit every release case and replace stored results
uv run python -m experiment.audit.matrix --jobs 8 --force

# Resume after an interruption. Cases with a stored result are skipped.
uv run python -m experiment.audit.matrix --jobs 8

# Rerun one scenario and fault after a fix. Other results stay valid.
uv run python -m experiment.audit.matrix --jobs 2 --scenario campus_lan --fault dns_lookup_latency --force
```

| Option | Default | Effect |
| --- | --- | --- |
| `--jobs N` | `2` | Number of worker processes. The scheduler below still limits how many labs run at once. |
| `--force` | off | Rerun every selected case, even when it has a stored result. |
| `--retry-failed` | off | Also rerun selected cases whose stored result is not `pass`. |
| `--scenario NAME` | all | Select cases of one scenario. |
| `--fault NAME` | all | Select cases of one fault. |
| `--resource-class CLASS` | all | Select `light`, `large`, `k8s`, or `clab` cases. |

The command exits with status 1 when any selected case lacks a passing result.

### Scheduler

Each case belongs to one resource class. `clab` is a Containerlab backend, `k8s` is a Kubernetes or llm-d scenario, `large` is topo size `l`, and `light` is everything else.
Before a case deploys, the matrix waits until available memory is at or above the class floor and the 1-minute load average is below twice the CPU count. It then waits the class spacing so the next check sees the memory the new lab claims.

| Class | Concurrent labs | Free memory floor | Spacing after admission |
| --- | --- | --- | --- |
| `light` | 8 | 8 GiB | 10 s |
| `large` | 3 | 16 GiB | 45 s |
| `k8s` | 2 | 16 GiB | 90 s |
| `clab` | 1 | 32 GiB | 180 s |

### Persistence window

Static faults wait 2 seconds between the symptom read and the persistence read. Dynamic faults wait longer:

| Fault | Window |
| --- | --- |
| `arp_cache_poisoning` | 3 s |
| `incast_traffic_network_limitation` | 3 s |
| `link_flap` | 5 s |
| `load_balancer_overload` | 3 s |
| `receiver_resource_contention` | 3 s |
| `sender_resource_contention` | 3 s |
| `tcp_syn_flood_attack` | 3 s |
| `web_dos_attack` | 3 s |

### Results and this page

The matrix writes one JSON record per case to `runtime/environment-audit-results/`. Each record holds the case identity, every stage with its evidence, the diagnosis, the elapsed time, and the provenance: Git commit and dirty flag, source and configuration hashes, start and finish timestamps, session ID, and image ID and repository digests for each lab node.

Regenerate this page from the stored records:

```shell
uv run python -c "from experiment.audit.report_doc import write_benchmark_audit_doc; write_benchmark_audit_doc()"
```

## Limits

- The persistence window lasts seconds. It does not cover the 2400-second trial budget. In this release, the SYN flood, incast, and sender and receiver contention cases set `duration=3600`, and the load balancer overload workers run until recovery. The benchmark trial also checks the artifact while the agent runs (see [Checks during a benchmark trial](#checks-during-a-benchmark-trial)).
- The P4 gateway ECN probe counts ECN marks from a virtual queue that drains about 61 packets per second. It does not measure congestion of a physical egress queue. The `queue_occupancy` register in that scenario reports the modeled queue depth.
- An `artifact_only` case shows that the scenario health checks regress and stay regressed. It does not measure a fault-specific symptom.

## Checks during a benchmark trial

`nika benchmark run` does not run this audit. Each trial runs lighter checks:

1. `startup_verify_lab` after the lab starts.
2. `verify_fault` after inject.
3. `PresenceWatch` reads the fault artifact on the problem instance that injected it: once 2 seconds after the agent starts, and once before NIKA removes the lab. For the faults in `DYNAMIC_ARTIFACT_FAULTS`, the read checks the live worker, flap, or quota.

The trial writes those reads to `fault-presence.json`. A `present` read means the artifact was still on the lab. It does not measure the network effect.
When any of these reads finds the artifact absent or fails, the trial outcome is `environment_invalid`. Leaderboard averages omit that outcome, and `nika benchmark run --resume` deletes the slot and runs it again.
When the agent run is interrupted (Ctrl+C or SIGTERM), NIKA skips the remaining reads and undeploys the lab.

## Executed audits

Each row is the stored result of one `audit_case` run.
The case tables in [Cases](#cases) count a run only when its scenario, scale, backend, design options, fault, and inject parameters all match a release case.

The Diagnosis column is `pass` for a passing run.
Otherwise it starts with `verify:` when the check or a host prerequisite failed, or with `case:` when the fault symptom did not appear on that lab.

| Scenario | Fault | Scale | Backend | Design | Inject | Admission | Diagnosis |
| --- | --- | --- | --- | --- | --- | --- | --- |
| campus_lan | device_forwarding_packet_corruption | l | scenario default | none | forwarding_device=router_core_2, intf_name=eth5, observer_device=pc_1_1_1_1, probe_dst_ip=10.200.0.3, seed=42 | pass | pass |
| campus_lan | dhcp_missing_subnet | m | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | pass | pass |
| campus_lan | dhcp_missing_subnet | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | pass | pass |
| campus_lan | dhcp_service_down | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | pass | pass |
| campus_lan | dhcp_service_down | m | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | pass | pass |
| campus_lan | dhcp_spoofed_dns | s | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | pass | pass |
| campus_lan | dhcp_spoofed_dns | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | pass | pass |
| campus_lan | dhcp_spoofed_gateway | s | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | pass | pass |
| campus_lan | dhcp_spoofed_gateway | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | pass | pass |
| campus_lan | dhcp_spoofed_subnet | s | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | pass | pass |
| campus_lan | dhcp_spoofed_subnet | m | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | pass | pass |
| campus_lan | dns_lookup_latency | m | scenario default | none | delay_ms=1000, host_name=dns_server, intf_name=eth0 | pass | pass |
| campus_lan | dns_port_blocked | l | scenario default | none | host_name=dns_server | pass | pass |
| campus_lan | dns_record_error | m | scenario default | none | host_name=dns_server, target_domain=local, target_website=web1 | pass | pass |
| campus_lan | dns_service_down | m | scenario default | none | host_name=dns_server | pass | pass |
| campus_lan | host_incorrect_dns | m | scenario default | none | host_name=pc_1_1_1_1 | pass | pass |
| campus_lan | load_balancer_overload | s | scenario default | none | backend_cpu_host=backend_web_0, backend_probe_host=load_balancer, backend_url=http://20.200.0.2/small, client_host=pc_1_1_1_1, concurrency=200, control_url=http://web0.local/small, cpu_quota=0.2, duration_sec=300, host_name=load_balancer, load_client_hosts=pc_2_1_1_1, load_workers=4, probe_concurrency=4, probe_requests=60, vip_url=http://web99.local/small, warmup_sec=5 | pass | pass |
| campus_lan | load_balancer_overload | l | scenario default | none | backend_cpu_host=backend_web_0, backend_probe_host=load_balancer, backend_url=http://20.200.0.2/small, client_host=pc_1_1_1_1, concurrency=200, control_url=http://web0.local/small, cpu_quota=0.2, duration_sec=300, host_name=load_balancer, load_client_hosts=pc_2_1_1_1, load_workers=4, probe_concurrency=4, probe_requests=60, vip_url=http://web99.local/small, warmup_sec=5 | pass | pass |
| campus_lan | ospf_acl_block | s | scenario default | none | host_name=router_core_1 | pass | pass |
| campus_lan | ospf_area_misconfiguration | m | scenario default | none | host_name=router_core_1 | pass | pass |
| campus_lan | ospf_neighbor_missing | l | scenario default | none | host_name=router_core_1 | pass | pass |
| dc_clos | arp_acl_block | m | scenario default | none | host_name=client_0 | pass | pass |
| dc_clos | dns_lookup_latency | s | scenario default | none | delay_ms=1000, host_name=dns_pod0, intf_name=eth0 | pass | pass |
| dc_clos | dns_port_blocked | m | scenario default | none | host_name=dns_pod0 | pass | pass |
| dc_clos | dns_record_error | l | scenario default | none | host_name=dns_pod0, target_domain=pod0, target_website=web0 | pass | pass |
| dc_clos | dns_service_down | s | scenario default | none | host_name=dns_pod0 | pass | pass |
| dc_clos | healthy | s | scenario default | none | none | pass | pass |
| dc_clos | healthy | l | scenario default | none | none | pass | pass |
| dc_clos | healthy | m | scenario default | none | none | pass | pass |
| dc_clos | host_incorrect_dns | s | scenario default | none | host_name=client_0 | pass | pass |
| dc_clos | host_incorrect_gateway | l | scenario default | none | host_name=client_0 | pass | pass |
| dc_clos | link_detach | m | scenario default | none | host_name=client_0, intf_name=eth0 | pass | pass |
| dc_clos | receiver_resource_contention | s | scenario default | none | duration=3600, host_name=client_0 | pass | pass |
| dc_clos | web_dos_attack | m | scenario default | none | attacker_device=client_0, host_name=webserver0_pod0, observer_device=dns_pod0, probe_url=http://10.0.1.2/small.bin | pass | pass |
| enterprise_branch | arp_acl_block | s | scenario default | none | host_name=br1_corp_pc | pass | pass |
| enterprise_branch | bgp_hijacking | l | scenario default | none | host_name=br1_edge | pass | pass |
| enterprise_branch | bgp_missing_route_advertisement | m | scenario default | none | host_name=br1_edge | pass | pass |
| enterprise_branch | healthy | m | scenario default | none | none | pass | pass |
| enterprise_branch | host_incorrect_netmask | m | scenario default | none | host_name=br1_corp_pc, netmask_prefix=8 | pass | pass |
| enterprise_branch | host_static_blackhole | l | scenario default | none | host_name=br1_edge | pass | pass |
| enterprise_branch | link_down | l | scenario default | none | host_name=br1_corp_pc, intf_name=eth0 | pass | pass |
| enterprise_branch | link_packet_corruption | m | scenario default | none | corruption_percentage=10, host_name=br1_edge, intf_name=eth3, observer_device=br1_corp_pc, probe_dst_ip=10.0.20.2 | pass | pass |
| enterprise_branch | mtu_mismatch | l | scenario default | none | host_name=br1_edge, intf_name=eth2, mtu=500 | pass | pass |
| enterprise_branch | nat_mapping_removed_without_drain | s | scenario default | none | host_name=br1_edge, nat_ip_a=198.18.1.10, nat_ip_b=198.18.1.11, source_prefix=10.1.40.0/24, wan_interface=eth2 | pass | pass |
| enterprise_branch | nat_mapping_removed_without_drain | l | scenario default | none | host_name=br1_edge, nat_ip_a=198.18.1.10, nat_ip_b=198.18.1.11, source_prefix=10.1.40.0/24, wan_interface=eth3 | pass | pass |
| enterprise_branch | snat_port_pool_exhaustion | s | scenario default | none | host_name=br1_edge, port_end=40063, port_start=40000, public_ip=198.18.1.10, source_prefix=10.1.40.0/24 | pass | pass |
| enterprise_branch | snat_port_pool_exhaustion | m | scenario default | none | host_name=br1_edge, port_end=40063, port_start=40000, public_ip=198.18.1.10, source_prefix=10.1.40.0/24 | pass | pass |
| enterprise_branch | tcp_receive_window_limited | s | scenario default | none | host_name=br1_corp_pc, large_url=http://10.0.20.2/large.bin, sender_host=hq_srv, sender_ip=10.0.20.2, small_url=http://10.0.20.2/small.bin | pass | pass |
| enterprise_branch | tcp_receive_window_limited | l | scenario default | none | host_name=br1_corp_pc2, large_url=http://10.0.20.2/large.bin, sender_host=hq_srv, sender_ip=10.0.20.2, small_url=http://10.0.20.2/small.bin | pass | pass |
| enterprise_branch | vrf_dscp_remarking | s | scenario default | none | corp_prefix=10.0.10.0/24, direction=lan_to_overlay, dst_host=br1_corp_pc, host_name=hq_edge, intf_name=wg_br1, src_host=hq_corp_pc | pass | pass |
| enterprise_branch | vrf_dscp_remarking | m | scenario default | none | corp_prefix=10.0.10.0/24, direction=lan_to_overlay, dst_host=br1_corp_pc, host_name=hq_edge, intf_name=wg_br1, src_host=hq_corp_pc | pass | pass |
| enterprise_branch | wireguard_allowed_ips_misconfiguration | l | scenario default | none | host_name=br1_edge, intf_name=wg_hq, target_prefix=10.0.20.0/24 | pass | pass |
| enterprise_branch | wireguard_allowed_ips_misconfiguration | s | scenario default | none | host_name=br1_edge, intf_name=wg_hq, target_prefix=10.0.20.0/24 | pass | pass |
| enterprise_branch | wireguard_peer_key_misconfiguration | l | scenario default | none | host_name=br1_edge, intf_name=wg_hq | pass | pass |
| enterprise_branch | wireguard_peer_key_misconfiguration | s | scenario default | none | host_name=br1_edge, intf_name=wg_hq | pass | pass |
| isp_abilene | bgp_max_prefix_exceeded | s | kathara | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=frr | flood_count=120, neighbor_ip=10.0.0.21, peer_name=losang, receiver_name=hstnng | pass | pass |
| isp_abilene | icmp_frag_needed_filter_misconfiguration | s | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=atlam5 | pass | pass |
| isp_abilene_ebgp_rpki | bgp_rpki_invalid_route_leak | s | kathara | device_profile=frr | host_name=kscyng | pass | pass |
| isp_abilene_ebgp_rtbh | bgp_blackhole_community_leak | s | kathara | device_profile=frr | host_name=kscyng, peer_host=pc_kscyng, probe_dst_ip=198.51.100.1, symptom_host=iplsng | pass | pass |
| isp_cost266 | bgp_asn_misconfig | l | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=amsterdam | pass | pass |
| isp_dfn-bwin | healthy | s | containerlab | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=nokia_srlinux | none | pass | pass |
| isp_dfn-bwin | link_down | s | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=berlin, intf_name=eth0, peer_host=pc_frankfurt, probe_dst_ip=10.254.0.6, symptom_host=pc_berlin | pass | pass |
| isp_dfn-bwin_ebgp_rtbh | bgp_blackhole_community_leak | s | kathara | device_profile=frr | host_name=frankfurt, peer_host=pc_frankfurt, probe_dst_ip=198.51.100.1, symptom_host=hamburg | pass | pass |
| isp_dfn-bwin_ebgp_rtbh | ospf_neighbor_missing | s | kathara | device_profile=frr | host_name=berlin | pass | pass |
| isp_dfn-gwin | healthy | s | containerlab | igp=isis, bgp_mode=none, rpki=False, device_profile=nokia_srlinux | none | pass | pass |
| isp_di-yuan | healthy | s | containerlab | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=nokia_srlinux | none | pass | pass |
| isp_di-yuan | healthy | s | containerlab | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=nokia_srlinux | none | pass | pass |
| isp_di-yuan | healthy | s | containerlab | igp=isis, bgp_mode=none, rpki=False, device_profile=nokia_srlinux | none | pass | pass |
| isp_di-yuan | link_capacity_bottleneck | s | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | burst=64kb, host_name=n_10, intf_name=eth0, limit=500kb, peer_host=pc_n_9, probe_dst_ip=10.254.0.42, rate=30kbit, symptom_host=pc_n_10 | pass | pass |
| isp_geant | bgp_max_prefix_exceeded | m | kathara | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=frr | flood_count=120, neighbor_ip=10.0.0.35, peer_name=nl1_nl, receiver_name=de1_de | pass | pass |
| isp_geant_ebgp_rpki | bgp_rpki_invalid_route_leak | m | kathara | device_profile=frr | host_name=es1_es | pass | pass |
| isp_geant_ebgp_rpki | host_static_blackhole | m | kathara | device_profile=frr | host_name=at1_at | pass | pass |
| isp_geant_ebgp_rpki | mtu_mismatch | m | kathara | device_profile=frr | host_name=at1_at, intf_name=eth0, mtu=500 | pass | pass |
| isp_germany50 | frr_service_down | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=aachen | pass | pass |
| isp_germany50 | ospf_area_misconfiguration | l | kathara | igp=ospf, bgp_mode=none, rpki=False, device_profile=frr | host_name=aachen | pass | pass |
| isp_india35 | link_packet_corruption | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | corruption_percentage=10, host_name=n_0, intf_name=eth0, peer_host=pc_n_24, probe_dst_ip=10.254.0.70, symptom_host=pc_n_0 | pass | pass |
| isp_janos-us | bgp_hijacking | m | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=albany, peer_host=pc_albany, probe_dst_ip=198.18.0.1, symptom_host=atlanta, target_network=198.18.0.0/24 | pass | pass |
| isp_janos-us-ca | icmp_acl_block | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=pc_atlanta | pass | pass |
| isp_nobel-eu | bgp_acl_block | m | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=amsterdam | pass | pass |
| isp_nobel-germany | link_flap | m | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | down_time=1, host_name=berlin, intf_name=eth0, peer_host=pc_hamburg, probe_dst_ip=10.254.0.26, symptom_host=pc_berlin, up_time=1 | pass | pass |
| isp_pdh | bgp_acl_block | s | containerlab | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=nokia_srlinux | host_name=n1 | pass | pass |
| isp_pdh | healthy | s | containerlab | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=nokia_srlinux | none | pass | pass |
| isp_pdh | healthy | s | containerlab | igp=isis, bgp_mode=none, rpki=False, device_profile=nokia_srlinux | none | pass | pass |
| isp_pdh | healthy | s | containerlab | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=nokia_srlinux | none | pass | pass |
| isp_pioro40 | bgp_missing_route_advertisement | l | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=n7, peer_host=pc_n7, prefix=203.0.113.0/24, probe_dst_ip=203.0.113.1, symptom_host=n0 | pass | pass |
| isp_ta1 | ospf_acl_block | m | kathara | igp=ospf, bgp_mode=none, rpki=False, device_profile=frr | host_name=n1 | pass | pass |
| isp_ta2 | link_detach | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=n10, intf_name=eth0, peer_host=pc_n2, probe_dst_ip=10.254.0.46, symptom_host=pc_n10 | pass | pass |
| k8s_lab | arp_cache_poisoning | none | scenario default | none | host_name=client | pass | pass |
| k8s_lab | bgp_asn_misconfig | none | scenario default | none | host_name=leaf_1_1 | pass | pass |
| k8s_lab | frr_service_down | none | scenario default | none | host_name=leaf_1_1 | pass | pass |
| k8s_lab | healthy | none | scenario default | none | none | pass | pass |
| k8s_lab | host_incorrect_gateway | none | scenario default | none | host_name=client | pass | pass |
| k8s_lab | host_incorrect_netmask | none | scenario default | none | host_name=client, netmask_prefix=8 | pass | pass |
| k8s_lab | k8s_clusterip_routing_broken | none | scenario default | none | control_node=controller, node_name=controller | pass | pass |
| k8s_lab | k8s_coredns_isolated | none | scenario default | none | control_node=controller, symptom_host=worker1 | pass | pass |
| k8s_lab | k8s_networkpolicy_deny | none | scenario default | none | control_node=controller, control_url=http://datacenter.com/weather?location=London, namespace=word-ns, pod_selector=app=word, symptom_host=client, symptom_url=http://datacenter.com/word | pass | pass |
| k8s_lab | k8s_worker_apiserver_partition | none | scenario default | none | control_node=controller, node_name=worker1 | pass | pass |
| llmd_lab | healthy | none | scenario default | none | none | pass | pass |
| llmd_lab | host_incorrect_ip | none | scenario default | none | host_name=client | pass | pass |
| llmd_lab | host_ip_conflict | none | scenario default | none | host_name=client, host_name_2=web | pass | pass |
| llmd_lab | host_missing_ip | none | scenario default | none | host_name=client, intf_name=eth0 | pass | pass |
| llmd_lab | http_acl_block | none | scenario default | none | host_name=client | pass | pass |
| llmd_lab | k8s_clusterip_routing_broken | none | scenario default | none | control_node=controller, node_name=controller | pass | pass |
| llmd_lab | k8s_coredns_isolated | none | scenario default | none | control_node=controller, symptom_host=worker1 | pass | pass |
| llmd_lab | k8s_networkpolicy_deny | none | scenario default | none | control_node=controller, control_url=http://200.0.0.8/, namespace=llm-d, pod_selector=gateway.networking.k8s.io/gateway-name=llm-d-gateway, symptom_host=client, symptom_url=http://llmd/v1/models | pass | pass |
| llmd_lab | k8s_worker_apiserver_partition | none | scenario default | none | control_node=controller, node_name=worker1 | pass | pass |
| llmd_lab | receiver_resource_contention | none | scenario default | none | duration=3600, host_name=client | pass | pass |
| llmd_lab | sender_resource_contention | none | scenario default | none | client_host=client, cpu_quota=0.05, dst_ip=200.0.0.8, duration=3600, host_name=web, large_url=http://200.0.0.8/large.bin, small_url=http://200.0.0.8/small.bin, stress_cpus=16 | pass | pass |
| min3clos | bgp_asn_misconfig | none | scenario default | none | host_name=leaf1 | pass | pass |
| min3clos | healthy | none | scenario default | none | none | pass | pass |
| min3clos | link_capacity_bottleneck | none | scenario default | none | burst=64kb, host_name=client1, intf_name=eth1, limit=500kb, rate=30kbit | pass | pass |
| p4_dc_fabric | bmv2_switch_down | l | scenario default | none | host_name=leaf_1 | pass | pass |
| p4_dc_fabric | host_missing_ip | s | scenario default | none | host_name=client_3_1, intf_name=eth0 | pass | pass |
| p4_dc_fabric | incast_traffic_network_limitation | l | scenario default | none | duration=3600, host_name=web_2, observer_device=client_1_1, probe_dst_ip=10.0.2.11 | pass | pass |
| p4_dc_fabric | mac_address_conflict | m | scenario default | none | host_name=web_2, host_name_2=client_3_2 | pass | pass |
| p4_dc_fabric | p4_action_selector_member_misconfig | m | scenario default | none | host_name=leaf_1 | pass | pass |
| p4_dc_fabric | p4_ecmp_group_member_missing | l | scenario default | none | host_name=leaf_1 | pass | pass |
| p4_dc_fabric | p4_table_entry_misconfig | m | scenario default | none | host_name=leaf_1, observer_device=client_1_1, probe_dst_ip=10.0.2.11 | pass | pass |
| p4_dc_fabric | p4_table_entry_missing | m | scenario default | none | host_name=leaf_1, observer_device=client_1_1, probe_dst_ip=10.0.2.11 | pass | pass |
| p4_dc_fabric | p4_table_resource_exhaustion | s | scenario default | none | host_name=leaf_1 | pass | pass |
| p4_dc_fabric | p4runtime_partial_write | l | scenario default | none | host_name=leaf_1 | pass | pass |
| p4_dc_fabric | p4runtime_pipeline_mismatch | s | scenario default | none | host_name=leaf_1 | pass | pass |
| p4_dc_gateway | bmv2_switch_down | s | scenario default | none | host_name=gateway_1 | pass | pass |
| p4_dc_gateway | healthy | m | scenario default | none | none | pass | pass |
| p4_dc_gateway | host_incorrect_ip | s | scenario default | none | host_name=client_1 | pass | pass |
| p4_dc_gateway | host_ip_conflict | l | scenario default | none | host_name=service_4_2, host_name_2=service_2_1 | pass | pass |
| p4_dc_gateway | http_acl_block | m | scenario default | none | host_name=client_1 | pass | pass |
| p4_dc_gateway | icmp_acl_block | s | scenario default | none | host_name=client_1 | pass | pass |
| p4_dc_gateway | icmp_frag_needed_filter_misconfiguration | m | scenario default | none | host_name=gateway_1 | pass | pass |
| p4_dc_gateway | incast_traffic_network_limitation | m | scenario default | none | duration=3600, host_name=service_1_1, observer_device=client_1, probe_dst_ip=10.0.1.11 | pass | pass |
| p4_dc_gateway | int_insufficient_mtu_headroom | s | scenario default | none | bmv2_port=2, host_name=gateway_1, int_mtu=1480, intf_name=eth1 | pass | pass |
| p4_dc_gateway | int_insufficient_mtu_headroom | m | scenario default | none | bmv2_port=2, host_name=gateway_1, int_mtu=1480, intf_name=eth1 | pass | pass |
| p4_dc_gateway | lb_connection_state_exhaustion | s | scenario default | none | attacker_device=client_2, backend_dip=10.0.1.11, capacity=256, client_host=client_1, host_name=gateway_1, seed=42, syn_timeout_sec=10, vip_url=http://20.0.0.1:80/ | pass | pass |
| p4_dc_gateway | lb_connection_state_exhaustion | l | scenario default | none | attacker_device=client_8, backend_dip=10.0.1.11, capacity=256, client_host=client_1, host_name=gateway_1, seed=42, syn_timeout_sec=10, vip_url=http://20.0.0.1:80/ | pass | pass |
| p4_dc_gateway | lb_pending_connection_update_race | l | scenario default | none | host_name=gateway_1, learning_delay_ms=5, seed=42 | pass | pass |
| p4_dc_gateway | lb_pending_connection_update_race | m | scenario default | none | host_name=gateway_1, learning_delay_ms=5, seed=42 | pass | pass |
| p4_dc_gateway | mac_address_conflict | s | scenario default | none | host_name=service_1_2, host_name_2=client_1 | pass | pass |
| p4_dc_gateway | p4_action_selector_member_misconfig | s | scenario default | none | host_name=leaf_1 | pass | pass |
| p4_dc_gateway | p4_ecmp_group_member_missing | m | scenario default | none | host_name=leaf_1 | pass | pass |
| p4_dc_gateway | p4_ecn_threshold_misconfiguration | l | scenario default | none | bmv2_port=10, host_name=spine_1, intf_name=eth9, threshold=1024 | pass | pass |
| p4_dc_gateway | p4_ecn_threshold_misconfiguration | s | scenario default | none | bmv2_port=2, host_name=gateway_1, intf_name=eth1, threshold=1024 | pass | pass |
| p4_dc_gateway | p4_table_entry_misconfig | s | scenario default | none | host_name=leaf_1 | pass | pass |
| p4_dc_gateway | p4_table_entry_missing | l | scenario default | none | host_name=gateway_1 | pass | pass |
| p4_dc_gateway | p4_table_resource_exhaustion | l | scenario default | none | host_name=leaf_1 | pass | pass |
| p4_dc_gateway | p4_tcam_entry_corruption | m | scenario default | none | control_source=client_2, host_name=gateway_3, target_ip=10.0.4.11 | pass | pass |
| p4_dc_gateway | p4_tcam_entry_corruption | s | scenario default | none | control_source=client_2, host_name=spine_1, target_ip=10.0.1.12 | pass | pass |
| p4_dc_gateway | p4runtime_partial_write | m | scenario default | none | host_name=leaf_1 | pass | pass |
| p4_dc_gateway | p4runtime_pipeline_mismatch | l | scenario default | none | host_name=gateway_1 | pass | pass |
| p4_dc_gateway | silent_egress_packet_loss | m | scenario default | none | bmv2_port=2, host_name=gateway_1, intf_name=eth1, loss_basis_points=200, seed=42 | pass | pass |
| p4_dc_gateway | silent_egress_packet_loss | s | scenario default | none | bmv2_port=2, host_name=gateway_1, intf_name=eth1, loss_basis_points=200, seed=42 | pass | pass |
| p4_dc_gateway | tcp_syn_flood_attack | m | scenario default | none | attacker_device=client_1, duration=3600, flows=100, rate_pps=1000, seed=42, target_ip=10.0.1.12, target_port=80 | pass | pass |
| p4_dc_gateway | tcp_syn_flood_attack | l | scenario default | none | attacker_device=client_1, duration=3600, flows=100, rate_pps=1000, seed=42, target_ip=10.0.4.12, target_port=80 | pass | pass |
| sdn_l3_clos | arp_cache_poisoning | l | scenario default | none | host_name=client_10_1 | pass | pass |
| sdn_l3_clos | device_forwarding_packet_corruption | s | scenario default | none | forwarding_device=leaf_2, intf_name=eth0, observer_device=client_1_1, probe_dst_ip=10.0.2.11, seed=42 | pass | pass |
| sdn_l3_clos | flow_rule_loop | m | scenario default | none | host_name=leaf_1, host_name_2=spine_2, port_name=eth5, port_name_2=eth5 | pass | pass |
| sdn_l3_clos | flow_rule_loop | s | scenario default | none | host_name=leaf_1, host_name_2=spine_1, port_name=eth2, port_name_2=eth2 | pass | pass |
| sdn_l3_clos | flow_rule_shadowing | s | scenario default | none | host_name=spine_1 | pass | pass |
| sdn_l3_clos | flow_rule_shadowing | l | scenario default | none | host_name=spine_1 | pass | pass |
| sdn_l3_clos | healthy | m | scenario default | none | none | pass | pass |
| sdn_l3_clos | healthy | l | scenario default | none | none | pass | pass |
| sdn_l3_clos | link_flap | s | scenario default | none | down_time=1, host_name=client_1_1, intf_name=eth0, observer_device=client_1_1, probe_dst_ip=10.0.2.11, up_time=1 | pass | pass |
| sdn_l3_clos | sdn_controller_crash | m | scenario default | none | host_name=onos | pass | pass |
| sdn_l3_clos | sdn_controller_crash | l | scenario default | none | host_name=onos | pass | pass |
| sdn_l3_clos | sender_resource_contention | l | scenario default | none | client_host=client_2_1, cpu_quota=0.05, dst_ip=10.0.1.11, duration=3600, host_name=web_1, large_url=http://10.0.1.11/large.bin, small_url=http://10.0.1.11/small.bin, stress_cpus=16 | pass | pass |
| sdn_l3_clos | southbound_port_block | m | scenario default | none | host_name=onos, southbound_port=6653 | pass | pass |
| sdn_l3_clos | southbound_port_block | l | scenario default | none | host_name=onos, southbound_port=6653 | pass | pass |
| sdn_l3_clos | southbound_port_mismatch | s | scenario default | none | host_name=onos, mismatched_port=6633, original_port=6653 | pass | pass |
| sdn_l3_clos | southbound_port_mismatch | l | scenario default | none | host_name=onos, mismatched_port=6633, original_port=6653 | pass | pass |
| sdn_l3_clos | web_dos_attack | s | scenario default | none | attacker_device=client_4_1, host_name=web_2, observer_device=client_1_1, probe_url=http://10.0.2.11/ | pass | pass |


## Cases

Every release case, grouped by scenario. The Admission column comes from the matching stored run.

### `enterprise_branch`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | arp_acl_block | s | scenario default | none | host_name=br1_corp_pc | path_ping | pass |
| dev | host_incorrect_netmask | m | scenario default | none | host_name=br1_corp_pc, netmask_prefix=8 | route_get_onlink | pass |
| dev | mtu_mismatch | l | scenario default | none | host_name=br1_edge, intf_name=eth2, mtu=500 | path_mtu_frag_needed | pass |
| dev | nat_mapping_removed_without_drain | s | scenario default | none | host_name=br1_edge, nat_ip_a=198.18.1.10, nat_ip_b=198.18.1.11, source_prefix=10.1.40.0/24, wan_interface=eth2 | custom | pass |
| dev | snat_port_pool_exhaustion | m | scenario default | none | host_name=br1_edge, port_end=40063, port_start=40000, public_ip=198.18.1.10, source_prefix=10.1.40.0/24 | custom | pass |
| dev | tcp_receive_window_limited | l | scenario default | none | host_name=br1_corp_pc2, large_url=http://10.0.20.2/large.bin, sender_host=hq_srv, sender_ip=10.0.20.2, small_url=http://10.0.20.2/small.bin | custom | pass |
| dev | vrf_dscp_remarking | s | scenario default | none | corp_prefix=10.0.10.0/24, direction=lan_to_overlay, dst_host=br1_corp_pc, host_name=hq_edge, intf_name=wg_br1, src_host=hq_corp_pc | custom | pass |
| dev | wireguard_allowed_ips_misconfiguration | l | scenario default | none | host_name=br1_edge, intf_name=wg_hq, target_prefix=10.0.20.0/24 | path_ping | pass |
| dev | wireguard_peer_key_misconfiguration | s | scenario default | none | host_name=br1_edge, intf_name=wg_hq | path_ping | pass |
| test | bgp_hijacking | l | scenario default | none | host_name=br1_edge | bgp_hijack_route | pass |
| test | bgp_missing_route_advertisement | m | scenario default | none | host_name=br1_edge | path_ping | pass |
| test | host_static_blackhole | l | scenario default | none | host_name=br1_edge | path_ping | pass |
| test | link_down | l | scenario default | none | host_name=br1_corp_pc, intf_name=eth0 | path_ping | pass |
| test | link_packet_corruption | m | scenario default | none | corruption_percentage=10, host_name=br1_edge, intf_name=eth3, observer_device=br1_corp_pc, probe_dst_ip=10.0.20.2 | custom | pass |
| test | nat_mapping_removed_without_drain | l | scenario default | none | host_name=br1_edge, nat_ip_a=198.18.1.10, nat_ip_b=198.18.1.11, source_prefix=10.1.40.0/24, wan_interface=eth3 | custom | pass |
| test | snat_port_pool_exhaustion | s | scenario default | none | host_name=br1_edge, port_end=40063, port_start=40000, public_ip=198.18.1.10, source_prefix=10.1.40.0/24 | custom | pass |
| test | tcp_receive_window_limited | s | scenario default | none | host_name=br1_corp_pc, large_url=http://10.0.20.2/large.bin, sender_host=hq_srv, sender_ip=10.0.20.2, small_url=http://10.0.20.2/small.bin | custom | pass |
| test | vrf_dscp_remarking | m | scenario default | none | corp_prefix=10.0.10.0/24, direction=lan_to_overlay, dst_host=br1_corp_pc, host_name=hq_edge, intf_name=wg_br1, src_host=hq_corp_pc | custom | pass |
| test | wireguard_allowed_ips_misconfiguration | s | scenario default | none | host_name=br1_edge, intf_name=wg_hq, target_prefix=10.0.20.0/24 | path_ping | pass |
| test | wireguard_peer_key_misconfiguration | l | scenario default | none | host_name=br1_edge, intf_name=wg_hq | path_ping | pass |
| test | healthy | m | scenario default | none | none | healthy | pass |

### `k8s_lab`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | arp_cache_poisoning | none | scenario default | none | host_name=client | path_ping | pass |
| dev | frr_service_down | none | scenario default | none | host_name=leaf_1_1 | control_plane_routing | pass |
| dev | host_incorrect_gateway | none | scenario default | none | host_name=client | path_ping | pass |
| dev | k8s_coredns_isolated | none | scenario default | none | control_node=controller, symptom_host=worker1 | isolation_http | pass |
| test | bgp_asn_misconfig | none | scenario default | none | host_name=leaf_1_1 | control_plane_bgp | pass |
| test | host_incorrect_netmask | none | scenario default | none | host_name=client, netmask_prefix=8 | route_get_onlink | pass |
| test | k8s_clusterip_routing_broken | none | scenario default | none | control_node=controller, node_name=controller | custom | pass |
| test | k8s_networkpolicy_deny | none | scenario default | none | control_node=controller, control_url=http://datacenter.com/weather?location=London, namespace=word-ns, pod_selector=app=word, symptom_host=client, symptom_url=http://datacenter.com/word | isolation_http | pass |
| test | k8s_worker_apiserver_partition | none | scenario default | none | control_node=controller, node_name=worker1 | artifact_only | pass |
| test | healthy | none | scenario default | none | none | healthy | pass |

### `isp_nobel-eu`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_acl_block | m | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=amsterdam | control_plane_bgp | pass |

### `isp_cost266`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_asn_misconfig | l | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=amsterdam | control_plane_bgp | pass |

### `isp_abilene_ebgp_rtbh`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_blackhole_community_leak | s | kathara | device_profile=frr | host_name=kscyng, peer_host=pc_kscyng, probe_dst_ip=198.51.100.1, symptom_host=iplsng | path_ping | pass |

### `isp_janos-us`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_hijacking | m | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=albany, peer_host=pc_albany, probe_dst_ip=198.18.0.1, symptom_host=atlanta, target_network=198.18.0.0/24 | bgp_hijack_route | pass |

### `isp_geant`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_max_prefix_exceeded | m | kathara | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=frr | flood_count=120, neighbor_ip=10.0.0.35, peer_name=nl1_nl, receiver_name=de1_de | control_plane_bgp | pass |

### `isp_pioro40`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_missing_route_advertisement | l | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=n7, peer_host=pc_n7, prefix=203.0.113.0/24, probe_dst_ip=203.0.113.1, symptom_host=n0 | path_ping | pass |

### `isp_abilene_ebgp_rpki`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_rpki_invalid_route_leak | s | kathara | device_profile=frr | host_name=kscyng | path_ping | pass |

### `p4_dc_fabric`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bmv2_switch_down | l | scenario default | none | host_name=leaf_1 | path_http | pass |
| dev | host_missing_ip | s | scenario default | none | host_name=client_3_1, intf_name=eth0 | path_ping | pass |
| dev | mac_address_conflict | m | scenario default | none | host_name=web_2, host_name_2=client_3_2 | custom | pass |
| dev | p4_action_selector_member_misconfig | m | scenario default | none | host_name=leaf_1 | artifact_only | pass |
| dev | p4_ecmp_group_member_missing | l | scenario default | none | host_name=leaf_1 | artifact_only | pass |
| dev | p4_table_entry_misconfig | m | scenario default | none | host_name=leaf_1, observer_device=client_1_1, probe_dst_ip=10.0.2.11 | path_http | pass |
| dev | p4_table_resource_exhaustion | s | scenario default | none | host_name=leaf_1 | artifact_only | pass |
| dev | p4runtime_partial_write | l | scenario default | none | host_name=leaf_1 | path_http | pass |
| dev | p4runtime_pipeline_mismatch | s | scenario default | none | host_name=leaf_1 | path_ping | pass |
| test | incast_traffic_network_limitation | l | scenario default | none | duration=3600, host_name=web_2, observer_device=client_1_1, probe_dst_ip=10.0.2.11 | custom | pass |
| test | p4_table_entry_missing | m | scenario default | none | host_name=leaf_1, observer_device=client_1_1, probe_dst_ip=10.0.2.11 | path_http | pass |

### `sdn_l3_clos`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | device_forwarding_packet_corruption | s | scenario default | none | forwarding_device=leaf_2, intf_name=eth0, observer_device=client_1_1, probe_dst_ip=10.0.2.11, seed=42 | custom | pass |
| dev | flow_rule_loop | m | scenario default | none | host_name=leaf_1, host_name_2=spine_2, port_name=eth5, port_name_2=eth5 | custom | pass |
| dev | flow_rule_shadowing | l | scenario default | none | host_name=spine_1 | custom | pass |
| dev | sdn_controller_crash | m | scenario default | none | host_name=onos | artifact_only | pass |
| dev | sender_resource_contention | l | scenario default | none | client_host=client_2_1, cpu_quota=0.05, dst_ip=10.0.1.11, duration=3600, host_name=web_1, large_url=http://10.0.1.11/large.bin, small_url=http://10.0.1.11/small.bin, stress_cpus=16 | custom | pass |
| dev | southbound_port_block | l | scenario default | none | host_name=onos, southbound_port=6653 | custom | pass |
| dev | southbound_port_mismatch | s | scenario default | none | host_name=onos, mismatched_port=6633, original_port=6653 | custom | pass |
| test | arp_cache_poisoning | l | scenario default | none | host_name=client_10_1 | path_ping | pass |
| test | flow_rule_loop | s | scenario default | none | host_name=leaf_1, host_name_2=spine_1, port_name=eth2, port_name_2=eth2 | custom | pass |
| test | flow_rule_shadowing | s | scenario default | none | host_name=spine_1 | custom | pass |
| test | link_flap | s | scenario default | none | down_time=1, host_name=client_1_1, intf_name=eth0, observer_device=client_1_1, probe_dst_ip=10.0.2.11, up_time=1 | custom | pass |
| test | sdn_controller_crash | l | scenario default | none | host_name=onos | artifact_only | pass |
| test | southbound_port_block | m | scenario default | none | host_name=onos, southbound_port=6653 | custom | pass |
| test | southbound_port_mismatch | l | scenario default | none | host_name=onos, mismatched_port=6633, original_port=6653 | custom | pass |
| test | web_dos_attack | s | scenario default | none | attacker_device=client_4_1, host_name=web_2, observer_device=client_1_1, probe_url=http://10.0.2.11/ | custom | pass |
| test | healthy | m | scenario default | none | none | healthy | pass |
| test | healthy | l | scenario default | none | none | healthy | pass |

### `campus_lan`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | dhcp_missing_subnet | m | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | artifact_only | pass |
| dev | dhcp_service_down | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | artifact_only | pass |
| dev | dhcp_spoofed_dns | s | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | artifact_only | pass |
| dev | dhcp_spoofed_gateway | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | artifact_only | pass |
| dev | dhcp_spoofed_subnet | m | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | artifact_only | pass |
| dev | load_balancer_overload | s | scenario default | none | backend_cpu_host=backend_web_0, backend_probe_host=load_balancer, backend_url=http://20.200.0.2/small, client_host=pc_1_1_1_1, concurrency=200, control_url=http://web0.local/small, cpu_quota=0.2, duration_sec=300, host_name=load_balancer, load_client_hosts=pc_2_1_1_1, load_workers=4, probe_concurrency=4, probe_requests=60, vip_url=http://web99.local/small, warmup_sec=5 | custom | pass |
| test | device_forwarding_packet_corruption | l | scenario default | none | forwarding_device=router_core_2, intf_name=eth5, observer_device=pc_1_1_1_1, probe_dst_ip=10.200.0.3, seed=42 | custom | pass |
| test | dhcp_missing_subnet | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | artifact_only | pass |
| test | dhcp_service_down | m | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | artifact_only | pass |
| test | dhcp_spoofed_dns | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | artifact_only | pass |
| test | dhcp_spoofed_gateway | s | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | artifact_only | pass |
| test | dhcp_spoofed_subnet | s | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | artifact_only | pass |
| test | dns_lookup_latency | m | scenario default | none | delay_ms=1000, host_name=dns_server, intf_name=eth0 | http_by_name | pass |
| test | dns_port_blocked | l | scenario default | none | host_name=dns_server | path_http | pass |
| test | dns_record_error | m | scenario default | none | host_name=dns_server, target_domain=local, target_website=web1 | dns_answer | pass |
| test | dns_service_down | m | scenario default | none | host_name=dns_server | path_http | pass |
| test | host_incorrect_dns | m | scenario default | none | host_name=pc_1_1_1_1 | http_by_name | pass |
| test | load_balancer_overload | l | scenario default | none | backend_cpu_host=backend_web_0, backend_probe_host=load_balancer, backend_url=http://20.200.0.2/small, client_host=pc_1_1_1_1, concurrency=200, control_url=http://web0.local/small, cpu_quota=0.2, duration_sec=300, host_name=load_balancer, load_client_hosts=pc_2_1_1_1, load_workers=4, probe_concurrency=4, probe_requests=60, vip_url=http://web99.local/small, warmup_sec=5 | custom | pass |
| test | ospf_acl_block | s | scenario default | none | host_name=router_core_1 | control_plane_ospf | pass |
| test | ospf_area_misconfiguration | m | scenario default | none | host_name=router_core_1 | control_plane_ospf | pass |
| test | ospf_neighbor_missing | l | scenario default | none | host_name=router_core_1 | control_plane_ospf | pass |

### `dc_clos`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | dns_lookup_latency | s | scenario default | none | delay_ms=1000, host_name=dns_pod0, intf_name=eth0 | http_by_name | pass |
| dev | dns_port_blocked | m | scenario default | none | host_name=dns_pod0 | path_http | pass |
| dev | dns_record_error | l | scenario default | none | host_name=dns_pod0, target_domain=pod0, target_website=web0 | dns_answer | pass |
| dev | dns_service_down | s | scenario default | none | host_name=dns_pod0 | path_http | pass |
| dev | host_incorrect_dns | s | scenario default | none | host_name=client_0 | http_by_name | pass |
| dev | web_dos_attack | m | scenario default | none | attacker_device=client_0, host_name=webserver0_pod0, observer_device=dns_pod0, probe_url=http://10.0.1.2/small.bin | custom | pass |
| test | arp_acl_block | m | scenario default | none | host_name=client_0 | path_ping | pass |
| test | host_incorrect_gateway | l | scenario default | none | host_name=client_0 | path_ping | pass |
| test | link_detach | m | scenario default | none | host_name=client_0, intf_name=eth0 | path_ping | pass |
| test | receiver_resource_contention | s | scenario default | none | duration=3600, host_name=client_0 | custom | pass |
| test | healthy | m | scenario default | none | none | healthy | pass |
| test | healthy | l | scenario default | none | none | healthy | pass |
| test | healthy | s | scenario default | none | none | healthy | pass |

### `llmd_lab`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | host_incorrect_ip | none | scenario default | none | host_name=client | ping_old_ip | pass |
| dev | http_acl_block | none | scenario default | none | host_name=client | path_http | pass |
| dev | k8s_clusterip_routing_broken | none | scenario default | none | control_node=controller, node_name=controller | custom | pass |
| dev | k8s_networkpolicy_deny | none | scenario default | none | control_node=controller, control_url=http://200.0.0.8/, namespace=llm-d, pod_selector=gateway.networking.k8s.io/gateway-name=llm-d-gateway, symptom_host=client, symptom_url=http://llmd/v1/models | isolation_http | pass |
| dev | k8s_worker_apiserver_partition | none | scenario default | none | control_node=controller, node_name=worker1 | artifact_only | pass |
| dev | receiver_resource_contention | none | scenario default | none | duration=3600, host_name=client | custom | pass |
| test | host_ip_conflict | none | scenario default | none | host_name=client, host_name_2=web | custom | pass |
| test | host_missing_ip | none | scenario default | none | host_name=client, intf_name=eth0 | path_ping | pass |
| test | k8s_coredns_isolated | none | scenario default | none | control_node=controller, symptom_host=worker1 | isolation_http | pass |
| test | sender_resource_contention | none | scenario default | none | client_host=client, cpu_quota=0.05, dst_ip=200.0.0.8, duration=3600, host_name=web, large_url=http://200.0.0.8/large.bin, small_url=http://200.0.0.8/small.bin, stress_cpus=16 | custom | pass |
| test | healthy | none | scenario default | none | none | healthy | pass |

### `p4_dc_gateway`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | host_ip_conflict | l | scenario default | none | host_name=service_4_2, host_name_2=service_2_1 | custom | pass |
| dev | incast_traffic_network_limitation | m | scenario default | none | duration=3600, host_name=service_1_1, observer_device=client_1, probe_dst_ip=10.0.1.11 | custom | pass |
| dev | int_insufficient_mtu_headroom | s | scenario default | none | bmv2_port=2, host_name=gateway_1, int_mtu=1480, intf_name=eth1 | custom | pass |
| dev | lb_connection_state_exhaustion | l | scenario default | none | attacker_device=client_8, backend_dip=10.0.1.11, capacity=256, client_host=client_1, host_name=gateway_1, seed=42, syn_timeout_sec=10, vip_url=http://20.0.0.1:80/ | custom | pass |
| dev | lb_pending_connection_update_race | m | scenario default | none | host_name=gateway_1, learning_delay_ms=5, seed=42 | custom | pass |
| dev | p4_ecn_threshold_misconfiguration | s | scenario default | none | bmv2_port=2, host_name=gateway_1, intf_name=eth1, threshold=1024 | custom | pass |
| dev | p4_table_entry_missing | l | scenario default | none | host_name=gateway_1 | path_http | pass |
| dev | p4_tcam_entry_corruption | m | scenario default | none | control_source=client_2, host_name=gateway_3, target_ip=10.0.4.11 | custom | pass |
| dev | silent_egress_packet_loss | s | scenario default | none | bmv2_port=2, host_name=gateway_1, intf_name=eth1, loss_basis_points=200, seed=42 | custom | pass |
| dev | tcp_syn_flood_attack | m | scenario default | none | attacker_device=client_1, duration=3600, flows=100, rate_pps=1000, seed=42, target_ip=10.0.1.12, target_port=80 | custom | pass |
| test | bmv2_switch_down | s | scenario default | none | host_name=gateway_1 | path_http | pass |
| test | host_incorrect_ip | s | scenario default | none | host_name=client_1 | ping_old_ip | pass |
| test | http_acl_block | m | scenario default | none | host_name=client_1 | path_http | pass |
| test | icmp_acl_block | s | scenario default | none | host_name=client_1 | path_ping | pass |
| test | icmp_frag_needed_filter_misconfiguration | m | scenario default | none | host_name=gateway_1 | custom | pass |
| test | int_insufficient_mtu_headroom | m | scenario default | none | bmv2_port=2, host_name=gateway_1, int_mtu=1480, intf_name=eth1 | custom | pass |
| test | lb_connection_state_exhaustion | s | scenario default | none | attacker_device=client_2, backend_dip=10.0.1.11, capacity=256, client_host=client_1, host_name=gateway_1, seed=42, syn_timeout_sec=10, vip_url=http://20.0.0.1:80/ | custom | pass |
| test | lb_pending_connection_update_race | l | scenario default | none | host_name=gateway_1, learning_delay_ms=5, seed=42 | custom | pass |
| test | mac_address_conflict | s | scenario default | none | host_name=service_1_2, host_name_2=client_1 | custom | pass |
| test | p4_action_selector_member_misconfig | s | scenario default | none | host_name=leaf_1 | artifact_only | pass |
| test | p4_ecmp_group_member_missing | m | scenario default | none | host_name=leaf_1 | artifact_only | pass |
| test | p4_ecn_threshold_misconfiguration | l | scenario default | none | bmv2_port=10, host_name=spine_1, intf_name=eth9, threshold=1024 | custom | pass |
| test | p4_table_entry_misconfig | s | scenario default | none | host_name=leaf_1 | path_http | pass |
| test | p4_table_resource_exhaustion | l | scenario default | none | host_name=leaf_1 | artifact_only | pass |
| test | p4_tcam_entry_corruption | s | scenario default | none | control_source=client_2, host_name=spine_1, target_ip=10.0.1.12 | custom | pass |
| test | p4runtime_partial_write | m | scenario default | none | host_name=leaf_1 | path_http | pass |
| test | p4runtime_pipeline_mismatch | l | scenario default | none | host_name=gateway_1 | path_ping | pass |
| test | silent_egress_packet_loss | m | scenario default | none | bmv2_port=2, host_name=gateway_1, intf_name=eth1, loss_basis_points=200, seed=42 | custom | pass |
| test | tcp_syn_flood_attack | l | scenario default | none | attacker_device=client_1, duration=3600, flows=100, rate_pps=1000, seed=42, target_ip=10.0.4.12, target_port=80 | custom | pass |
| test | healthy | m | scenario default | none | none | healthy | pass |

### `isp_geant_ebgp_rpki`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | host_static_blackhole | m | kathara | device_profile=frr | host_name=at1_at | path_ping | pass |
| test | bgp_rpki_invalid_route_leak | m | kathara | device_profile=frr | host_name=es1_es | path_ping | pass |
| test | mtu_mismatch | m | kathara | device_profile=frr | host_name=at1_at, intf_name=eth0, mtu=500 | path_mtu_frag_needed | pass |

### `isp_janos-us-ca`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | icmp_acl_block | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=pc_atlanta | path_ping | pass |

### `isp_abilene`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | icmp_frag_needed_filter_misconfiguration | s | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=atlam5 | custom | pass |
| test | bgp_max_prefix_exceeded | s | kathara | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=frr | flood_count=120, neighbor_ip=10.0.0.21, peer_name=losang, receiver_name=hstnng | control_plane_bgp | pass |

### `min3clos`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | link_capacity_bottleneck | none | scenario default | none | burst=64kb, host_name=client1, intf_name=eth1, limit=500kb, rate=30kbit | custom | pass |
| dev | healthy | none | scenario default | none | none | healthy | pass |
| test | bgp_asn_misconfig | none | scenario default | none | host_name=leaf1 | control_plane_bgp | pass |

### `isp_ta2`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | link_detach | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=n10, intf_name=eth0, peer_host=pc_n2, probe_dst_ip=10.254.0.46, symptom_host=pc_n10 | path_ping | pass |

### `isp_dfn-bwin`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | link_down | s | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=berlin, intf_name=eth0, peer_host=pc_frankfurt, probe_dst_ip=10.254.0.6, symptom_host=pc_berlin | path_ping | pass |
| dev | healthy | s | containerlab | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=nokia_srlinux | none | healthy | pass |

### `isp_nobel-germany`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | link_flap | m | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | down_time=1, host_name=berlin, intf_name=eth0, peer_host=pc_hamburg, probe_dst_ip=10.254.0.26, symptom_host=pc_berlin, up_time=1 | custom | pass |

### `isp_india35`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | link_packet_corruption | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | corruption_percentage=10, host_name=n_0, intf_name=eth0, peer_host=pc_n_24, probe_dst_ip=10.254.0.70, symptom_host=pc_n_0 | custom | pass |

### `isp_ta1`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | ospf_acl_block | m | kathara | igp=ospf, bgp_mode=none, rpki=False, device_profile=frr | host_name=n1 | control_plane_ospf | pass |

### `isp_germany50`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | ospf_area_misconfiguration | l | kathara | igp=ospf, bgp_mode=none, rpki=False, device_profile=frr | host_name=aachen | control_plane_ospf | pass |
| test | frr_service_down | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=aachen | control_plane_routing | pass |

### `isp_dfn-bwin_ebgp_rtbh`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | ospf_neighbor_missing | s | kathara | device_profile=frr | host_name=berlin | control_plane_ospf | pass |
| test | bgp_blackhole_community_leak | s | kathara | device_profile=frr | host_name=frankfurt, peer_host=pc_frankfurt, probe_dst_ip=198.51.100.1, symptom_host=hamburg | path_ping | pass |

### `isp_di-yuan`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | healthy | s | containerlab | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=nokia_srlinux | none | healthy | pass |
| dev | healthy | s | containerlab | igp=isis, bgp_mode=none, rpki=False, device_profile=nokia_srlinux | none | healthy | pass |
| dev | healthy | s | containerlab | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=nokia_srlinux | none | healthy | pass |
| test | link_capacity_bottleneck | s | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | burst=64kb, host_name=n_10, intf_name=eth0, limit=500kb, peer_host=pc_n_9, probe_dst_ip=10.254.0.42, rate=30kbit, symptom_host=pc_n_10 | custom | pass |

### `isp_pdh`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | healthy | s | containerlab | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=nokia_srlinux | none | healthy | pass |
| dev | healthy | s | containerlab | igp=isis, bgp_mode=none, rpki=False, device_profile=nokia_srlinux | none | healthy | pass |
| dev | healthy | s | containerlab | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=nokia_srlinux | none | healthy | pass |
| test | bgp_acl_block | s | containerlab | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=nokia_srlinux | host_name=n1 | control_plane_bgp | pass |

### `isp_dfn-gwin`

| Split | Fault | Scale | Backend | Design | Inject | Symptom probe | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- |
| dev | healthy | s | containerlab | igp=isis, bgp_mode=none, rpki=False, device_profile=nokia_srlinux | none | healthy | pass |

Rows with admission `not_run` are coverage gaps for the full audit.
