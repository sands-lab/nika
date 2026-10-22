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

Each fault declares the kind of symptom it must cause. The Symptom checked column in [Cases](#cases) says what the audit measures for each case.

| Symptom kind | Meaning | Cases |
| --- | --- | --- |
| `unreachable` | Traffic or a service that worked before inject stops working. | 76 |
| `control_plane` | A protocol session, route, or forwarding table changes; traffic may still flow. | 32 |
| `degradation` | Throughput, latency, or success rate gets far worse; traffic still flows. | 22 |
| `healthy` | No fault: the lab must pass its scenario checks. | 18 |
| `gray` | Part of the traffic is lost or corrupted. | 8 |
| `loss` | Packets are lost on the faulted path. | 7 |
| `isolation` | One path is cut off while a control path keeps working. | 4 |
| `latency` | Packets are delayed on the faulted path. | 2 |

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

The audit ran from 2026-10-24 20:10 to 2026-10-25 23:26 UTC.
Each case took 7.8 minutes at the median and 22.2 minutes at most, from lab deploy to undeploy.

Lab nodes used these images. Every node that used an image reference had the same image ID.

| Image | Image ID |
| --- | --- |
| `ghcr.io/nokia/srlinux:24.10@sha256:20064faa8c2fbb2fa6d8812496c28bcfdad40a965da8831da43e55a1eccda14d` | `57c4f3789309` |
| `kathara/p4:latest@sha256:62d4511908530028e275420b644d05e45ed9b4f5d786b56a63b5fb287dcce6ef` | `62d451190853`, `9102302f9fb3` |
| `kathara/sdn:latest@sha256:19cb5b383367e1d204e76f7e6e65753fbc947647747879f24883558a5c3fd8c5` | `19cb5b383367`, `1b6bac7a75d8` |
| `nika/base:0.2.0` | `708951f5b1a6`, `847c0f5fe882` |
| `nika/fabric-controller:0.2.0` | `8492ced23f37`, `bb30d4170e4d` |
| `nika/frr:0.2.0` | `754699e8256f`, `aca95475caee` |
| `nika/nginx:0.2.0` | `e2fe0d71fb71`, `e8655478f5c8` |
| `nika/onos:0.2.0` | `2a83abf39cab`, `790fa4ef806d` |
| `nika/routinator:0.2.0` | `020501984f49` |
| `rancher/k3s:v1.34.1-k3s1@sha256:5e0707cfd1239b358ef73f3254bc3eadc027dd30cd5ec6ca41e29e47652a1b8c` | `7a78342ef2af` |

## What the audit checks for one case

`audit_case` in `experiment/audit/live.py` audits one case in this order:

1. Deploy the lab with `start_net_env`, using the case scenario, scale, backend, and design options.
2. Run the scenario health checks (`verify_lab`). This is stage `baseline_lab`.
3. Measure the symptom signal while the lab is healthy. This is stage `baseline_path`. It reads the same metric, on the same path, that the symptom probe compares against after inject. A fault without such a baseline is `unsupported`.
4. Negative control: evaluate the symptom predicate on the healthy lab, before inject. This is stage `sham_symptom`, and it passes only when the predicate does not hold. A predicate that fires on a healthy lab cannot show that the fault caused the symptom.
5. Inject the fault with the case inject parameters and run `verify_fault`. This is stage `inject_artifact`.
6. Run the declared symptom probe (stage `symptom`) and a control path that avoids the root cause (stage `control_path`).
7. Wait 60 s, then read the fault artifact and the symptom again (stages `persistence_artifact` and `persistence_symptom`).
8. Wait 60 s more and read both once more (stages `final_artifact` and `final_symptom`).
9. Undeploy the lab with `close_session`, even when a stage fails.

A `healthy` case runs `verify_lab`, waits 120 s, and runs it again.

Every stage records its start time and duration. A symptom stage in which a probe command timed out or raised is `fail`, because a timed-out command reads the same as a broken path.

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
| `no_evidence` | The stage produced no observation. |

### Agent-view stages

These stages check what a diagnosing agent reads, using `experiment/audit/agent_view.py`. They run no LLM. They are required like the other stages: a stored record without them is stale and runs again.

| Stage | Runs on | Fails when |
| --- | --- | --- |
| `agent_view` | healthy cases, inside the 120 s wait | A check fails in both of two rounds 15 s apart: a PingMesh anomaly with the tool's default arguments and the scenario's `probe_intent`, the scenario probe path, an IPv4 or IPv6 endpoint pair that does not answer (or answers although `probe_intent` isolates it), a published DNS name that does not resolve from a resolver client, an FRR BGP peer that is not Established, or a Kubernetes `Warning` event. |
| `baseline_agent_view` | fault cases, after `sham_symptom` | The same checks on the healthy lab before inject. |
| `leak_scan` | fault cases, right after `inject_artifact` | A file under `/etc`, `/tmp`, `/var`, `/opt`, `/root`, `/usr/local` that inject added or changed names `nika`, an injection, or the fault id (in its path, or more times in its content than before), ends like a backup (`.sav`, `.bak`, `.orig`), or is a copy of a file's pre-inject content; or a new process, socket, link, route, rule, firewall, tc, FRR, OVS, SR Linux, Kubernetes, or P4Runtime line names one of those. |

The leak scan masks timestamps, PIDs, counters, and object versions before it compares the two snapshots. Inject parameter values such as the target host or prefix are not tokens: the fault lives in that configuration. New processes are listed for review without failing the stage, because some are the fault mechanism itself. An agent-view stage that cannot read the lab is `no_evidence`.

A stored result counts only when it has a finished run, image identities for every lab node, the symptom probe that the fault declares today, and a clean, committed working tree. Each record keeps its commit and source fingerprint. A case audited with `--repeats N` passes only when all N fresh-lab runs pass. A case without such a result is not admitted.

## Launch parameters

Run every command from the repository root on a host with Docker and the NIKA images built.

The release 0.2.0 audit used these commands:

```shell
# Audit every release case twice on fresh labs, replacing stored results
uv run python -m experiment.audit.matrix --jobs 8 --repeats 2 --force

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
| `--repeats N` | `1` | Run each case on N fresh labs. |
| `--allow-dirty` | off | Run on uncommitted edits. Those records never count on this page. |

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

### Results and this page

The matrix writes one JSON record per case to `runtime/environment-audit-results/`. Each record holds the case identity, every stage with its evidence, the diagnosis, the elapsed time, and the provenance: Git commit and dirty flag, source and configuration hashes, start and finish timestamps, session ID, and image ID and repository digests for each lab node.

Regenerate this page from the stored records:

```shell
uv run python -c "from experiment.audit.report_doc import write_benchmark_audit_doc; write_benchmark_audit_doc()"
```

## Limits

- The audit observes each case for about 2 minutes after inject. It does not cover the 2400-second trial budget. Labs run the scenario business workload during the audit, so symptoms are checked under realistic load. The agent-view stages check the PingMesh tool and the lab state an agent can read, not the prompt or the other MCP tools' output. The benchmark trial also checks the artifact while the agent runs (see [Checks during a benchmark trial](#checks-during-a-benchmark-trial)).

## Checks during a benchmark trial

`nika benchmark run` does not run this audit. Each trial runs lighter checks:

1. `startup_verify_lab` after the lab starts.
2. `verify_fault` after inject.
3. `PresenceWatch` reads the fault artifact on the problem instance that injected it: once 2 seconds after the agent starts, and once before NIKA removes the lab. For faults that declare `dynamic_artifact`, the read checks the live worker, flap, or quota.

The trial logs each read as a `fault_presence_recheck` event in the session log. A `present` read means the artifact was still on the lab. It does not measure the network effect.
An `absent` or `error` read is only logged. It does not change the trial outcome or score, and the trial is not retried.
When the agent run is interrupted (Ctrl+C or SIGTERM), NIKA skips the remaining reads and undeploys the lab.

## Cases

Every release case, grouped by scenario. The Admission column comes from the matching stored run.

### `enterprise_branch`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | arp_acl_block | s | scenario default | none | host_name=br1_corp_pc | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| dev | host_incorrect_netmask | m | scenario default | none | host_name=br1_corp_pc, netmask_prefix=8 | A remote host inside the wrong prefix loses the host; a sibling does not. | 2/2 | pass |
| dev | host_ip_conflict | l | scenario default | none | host_name=br1_corp_pc, host_name_2=br1_corp_pc2 | Two hosts answer ARP for the same address, as a third node sees it. | 2/2 | pass |
| dev | mtu_mismatch | l | scenario default | none | host_name=br1_edge, intf_name=eth2, mtu=500 | Small packets still cross, but the largest packet that does is far smaller. | 2/2 | pass |
| dev | nat_mapping_removed_without_drain | s | scenario default | none | host_name=br1_edge, nat_ip_a=198.18.1.10, nat_ip_b=198.18.1.11, source_prefix=10.1.40.0/24, wan_interface=eth2 | The pre-inject flow breaks and new flows leave through ``nat_ip_b``. | 2/2 | pass |
| dev | snat_port_pool_exhaustion | m | scenario default | none | host_name=br1_edge, port_end=40063, port_start=40000, public_ip=198.18.1.10, source_prefix=10.1.40.0/24 | After inject, concurrent flows stop at the SNAT pool size. | 2/2 | pass |
| dev | tcp_receive_window_limited | l | scenario default | none | host_name=br1_corp_pc2, large_url=http://10.0.20.2/large.bin, sender_host=hq_srv, sender_ip=10.0.20.2, small_url=http://10.0.20.2/small.bin | Bulk TCP into the receiver is window-bound; another receiver is not. | 2/2 | pass |
| dev | vrf_dscp_remarking | s | scenario default | none | corp_prefix=10.0.10.0/24, direction=lan_to_overlay, dst_host=br1_corp_pc, host_name=hq_edge, intf_name=wg_br1, src_host=hq_corp_pc | Demoted media competes with bulk in best effort and loses packets. | 2/2 | pass |
| dev | wireguard_allowed_ips_misconfiguration | l | scenario default | none | host_name=br1_edge, intf_name=wg_hq, target_prefix=10.0.20.0/24 | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| dev | wireguard_peer_key_misconfiguration | s | scenario default | none | host_name=br1_edge, intf_name=wg_hq | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| test | bgp_hijacking | l | scenario default | none | host_name=hq_edge, observer_host=br1_corp_pc, probe_dst_ip=10.2.10.2, symptom_host=br1_edge, target_network=10.2.10.0/24 | The remote observer's best path points at the hijacker and its host's traffic follows. | 2/2 | pass |
| test | bgp_missing_route_advertisement | m | scenario default | none | host_name=br1_edge | Every observer loses the originator's route for the baseline prefixes. | 2/2 | pass |
| test | host_static_blackhole | l | scenario default | none | host_name=br1_edge | Hosts behind the router lose the prefix; other destinations stay up. | 2/2 | pass |
| test | link_down | l | scenario default | none | host_name=br1_corp_pc, intf_name=eth0 | Traffic no longer crosses the link. | 2/2 | pass |
| test | link_packet_corruption | m | scenario default | none | corruption_percentage=10, host_name=br1_edge, intf_name=eth3, observer_device=br1_corp_pc, probe_dst_ip=10.0.20.2 | Corrupted frames are dropped: echoes are lost and TCP goodput collapses. | 2/2 | pass |
| test | nat_mapping_removed_without_drain | l | scenario default | none | host_name=br1_edge, nat_ip_a=198.18.1.10, nat_ip_b=198.18.1.11, source_prefix=10.1.40.0/24, wan_interface=eth3 | The pre-inject flow breaks and new flows leave through ``nat_ip_b``. | 2/2 | pass |
| test | snat_port_pool_exhaustion | s | scenario default | none | host_name=br1_edge, port_end=40063, port_start=40000, public_ip=198.18.1.10, source_prefix=10.1.40.0/24 | After inject, concurrent flows stop at the SNAT pool size. | 2/2 | pass |
| test | tcp_receive_window_limited | s | scenario default | none | host_name=br1_corp_pc, large_url=http://10.0.20.2/large.bin, sender_host=hq_srv, sender_ip=10.0.20.2, small_url=http://10.0.20.2/small.bin | Bulk TCP into the receiver is window-bound; another receiver is not. | 2/2 | pass |
| test | vrf_dscp_remarking | m | scenario default | none | corp_prefix=10.0.10.0/24, direction=lan_to_overlay, dst_host=br1_corp_pc, host_name=hq_edge, intf_name=wg_br1, src_host=hq_corp_pc | Demoted media competes with bulk in best effort and loses packets. | 2/2 | pass |
| test | wireguard_allowed_ips_misconfiguration | s | scenario default | none | host_name=br1_edge, intf_name=wg_hq, target_prefix=10.0.20.0/24 | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| test | wireguard_peer_key_misconfiguration | l | scenario default | none | host_name=br1_edge, intf_name=wg_hq | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| test | healthy | m | scenario default | none | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |

### `k8s_lab`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | arp_cache_poisoning | none | scenario default | none | host_name=client | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| dev | frr_service_down | none | scenario default | none | host_name=leaf_1_1 | Every remote OSPF, IS-IS, or BGP neighbor of the router loses its adjacency. | 2/2 | pass |
| dev | host_incorrect_gateway | none | scenario default | none | host_name=client | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| dev | k8s_coredns_isolated | none | scenario default | none | control_node=controller, node_name=controller, symptom_host=worker1 | Another node's lookups fail while the CoreDNS pod stays reachable by IP. | 2/2 | pass |
| test | bgp_asn_misconfig | none | scenario default | none | host_name=leaf_1_1 | Remote peers refuse the router's OPEN with "Bad Peer AS"; sessions stay down. | 2/2 | pass |
| test | host_incorrect_netmask | none | scenario default | none | host_name=worker3, netmask_prefix=8 | A remote host inside the wrong prefix loses the host; a sibling does not. | 2/2 | pass |
| test | k8s_clusterip_routing_broken | none | scenario default | none | control_node=controller, node_name=controller | The node cannot use a Service VIP; its backend and other nodes still can. | 2/2 | pass |
| test | k8s_networkpolicy_deny | none | scenario default | none | control_node=controller, control_url=http://datacenter.com/weather?location=London, namespace=word-ns, pod_selector=app=word, symptom_host=client, symptom_url=http://datacenter.com/word | The isolated path stops answering HTTP while a control path still answers. | 2/2 | pass |
| test | k8s_worker_apiserver_partition | none | scenario default | none | control_node=controller, node_name=worker1 | NotReady worker whose pods leave endpoints and refuse logs, still pingable. | 2/2 | pass |
| test | healthy | none | scenario default | none | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |

### `isp_nobel-eu`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_acl_block | m | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=amsterdam | Every remote BGP peer of the router loses its session. | 2/2 | pass |

### `isp_cost266`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_asn_misconfig | l | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=amsterdam | Remote peers refuse the router's OPEN with "Bad Peer AS"; sessions stay down. | 2/2 | pass |

### `isp_abilene_ebgp_rtbh`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_blackhole_community_leak | s | kathara | device_profile=frr | host_name=kscyng, peer_host=pc_kscyng, probe_dst_ip=198.51.100.1, symptom_host=iplsng | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |

### `isp_janos-us`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_hijacking | m | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=albany, observer_host=pc_newyork, probe_dst_ip=203.0.115.1, symptom_host=newyork, target_network=203.0.115.0/24 | The remote observer's best path points at the hijacker and its host's traffic follows. | 2/2 | pass |

### `isp_geant`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_max_prefix_exceeded | m | kathara | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=frr | flood_count=120, neighbor_ip=10.0.0.35, peer_name=nl1_nl, receiver_name=de1_de | The peer's session is down with the receiver's max-prefix Cease notification. | 2/2 | pass |

### `isp_pioro40`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_missing_route_advertisement | l | kathara | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=frr | host_name=n7, peer_host=pc_n7, prefix=203.0.113.0/24, probe_dst_ip=203.0.113.1, symptom_host=n0 | Every observer loses the originator's route for the baseline prefixes. | 2/2 | pass |

### `isp_abilene_ebgp_rpki`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bgp_rpki_invalid_route_leak | s | kathara | device_profile=frr | host_name=kscyng | The non-ROV observer accepts the leaked origin; the ROV observer rejects it. | 2/2 | pass |

### `p4_dc_fabric`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | bmv2_switch_down | l | scenario default | none | host_name=leaf_1 | HTTP on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| dev | host_missing_ip | s | scenario default | none | host_name=client_3_1, intf_name=eth0 | Peers can no longer reach the host at the address it should hold. | 2/2 | pass |
| dev | p4_action_selector_member_misconfig | m | scenario default | none | host_name=leaf_1 | Flows hashed to the bad member leave on the bad port and are lost. | 2/2 | pass |
| dev | p4_ecmp_group_member_missing | l | scenario default | none | host_name=leaf_1 | The removed member's next hop receives none of the sweep; nothing is lost. | 2/2 | pass |
| dev | p4_table_entry_misconfig | m | scenario default | none | host_name=leaf_1, observer_device=client_1_1, probe_dst_ip=10.0.2.11 | HTTP on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| dev | p4_table_resource_exhaustion | s | scenario default | none | host_name=leaf_1 | The full LPM table rejects a new entry; installed routes still forward. | 2/2 | pass |
| dev | p4runtime_partial_write | l | scenario default | none | host_name=leaf_1 | Victim traffic lands on the local host, which is itself unreachable. | 2/2 | pass |
| dev | p4runtime_pipeline_mismatch | s | scenario default | none | host_name=leaf_1 | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| test | incast_traffic_network_limitation | l | scenario default | none | host_name=web_2, observer_device=client_1_1, probe_dst_ip=10.0.2.11 | Synchronized fan-in overflows the shallow egress queue: the port tail-drops while its average send rate stays well below the port rate, and a control flow that avoids the port stays healthy. | 2/2 | pass |
| test | p4_table_entry_missing | m | scenario default | none | host_name=leaf_1, observer_device=client_1_1, probe_dst_ip=10.0.2.11 | HTTP on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |

### `sdn_l3_clos`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | device_forwarding_packet_corruption | s | scenario default | none | forwarding_device=leaf_2, intf_name=eth0, observer_device=client_1_1, probe_dst_ip=10.0.2.11, seed=42 | End hosts receive TCP segments that fail their checksum. | 2/2 | pass |
| dev | flow_rule_loop | m | scenario default | none | host_name=leaf_1, host_name_2=spine_2, port_name=eth5, port_name_2=eth5 | Flows hashed onto the looped link are lost and multiply on that link. | 2/2 | pass |
| dev | flow_rule_shadowing | l | scenario default | none | host_name=spine_1 | Flows to the shadowed prefix stop arriving; other prefixes still do. | 2/2 | pass |
| dev | sdn_controller_crash | m | scenario default | none | host_name=onos | Switches lose their controller and new flows stop reaching them. | 2/2 | pass |
| dev | sender_resource_contention | l | scenario default | none | client_host=client_2_1, cpu_quota=0.05, dst_ip=10.0.1.11, duration=3600, host_name=web_1, large_url=http://10.0.1.11/large.bin, small_url=http://10.0.1.11/small.bin, stress_cpus=16 | Bulk downloads from the sender slow down; small objects and the path do not. | 2/2 | pass |
| dev | southbound_port_block | l | scenario default | none | host_name=onos, southbound_port=6653 | Switches lose their controller and new flows stop reaching them. | 2/2 | pass |
| dev | southbound_port_mismatch | s | scenario default | none | host_name=onos, mismatched_port=6633, original_port=6653 | Switches lose their controller and new flows stop reaching them. | 2/2 | pass |
| test | arp_cache_poisoning | l | scenario default | none | host_name=client_10_1 | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| test | flow_rule_loop | s | scenario default | none | host_name=leaf_1, host_name_2=spine_1, port_name=eth2, port_name_2=eth2 | Flows hashed onto the looped link are lost and multiply on that link. | 2/2 | pass |
| test | flow_rule_shadowing | s | scenario default | none | host_name=spine_1 | Flows to the shadowed prefix stop arriving; other prefixes still do. | 2/2 | pass |
| test | link_flap | s | scenario default | none | down_time=1, host_name=client_1_1, intf_name=eth0, observer_device=client_1_1, probe_dst_ip=10.0.2.11, up_time=1 | Echo loss comes in repeated outages separated by recoveries. | 2/2 | pass |
| test | sdn_controller_crash | l | scenario default | none | host_name=onos | Switches lose their controller and new flows stop reaching them. | 2/2 | pass |
| test | southbound_port_block | m | scenario default | none | host_name=onos, southbound_port=6653 | Switches lose their controller and new flows stop reaching them. | 2/2 | pass |
| test | southbound_port_mismatch | l | scenario default | none | host_name=onos, mismatched_port=6633, original_port=6653 | Switches lose their controller and new flows stop reaching them. | 2/2 | pass |
| test | web_dos_attack | s | scenario default | none | attacker_device=client_4_1, host_name=web_2, observer_device=client_1_1, probe_url=http://10.0.2.11/ | The victim answers far slower or not at all; another server does not. | 2/2 | pass |
| test | healthy | m | scenario default | none | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |
| test | healthy | l | scenario default | none | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |

### `campus_lan`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | dhcp_missing_subnet | m | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | The client segment gets no offer, holds no address and loses its gateway. | 2/2 | pass |
| dev | dhcp_service_down | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | The client segment gets no offer, holds no address and loses its gateway. | 2/2 | pass |
| dev | dhcp_spoofed_dns | s | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | The client queries the leased resolver and names stop resolving. | 2/2 | pass |
| dev | dhcp_spoofed_gateway | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | The client routes via the leased gateway and loses off-subnet hosts. | 2/2 | pass |
| dev | dhcp_spoofed_subnet | m | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | The leased prefix no longer covers the gateway, so it is unreachable. | 2/2 | pass |
| dev | load_balancer_overload | s | scenario default | none | backend_cpu_host=backend_web_0, backend_probe_host=load_balancer, backend_url=http://20.200.0.2/small, client_host=pc_1_1_1_1, concurrency=200, control_url=http://web0.local/small, cpu_quota=0.2, duration_sec=300, host_name=load_balancer, load_client_hosts=pc_2_1_1_1, load_workers=4, probe_concurrency=4, probe_requests=60, vip_url=http://web99.local/small, warmup_sec=5 | Requests through the VIP slow down or fail; the parallel server does not. | 2/2 | pass |
| dev | mac_address_conflict | m | scenario default | none | host_name=pc_1_1_1_2, host_name_2=pc_1_1_1_1 | Traffic to both holders of the MAC is lost; the control's is not. | 2/2 | pass |
| test | device_forwarding_packet_corruption | l | scenario default | none | forwarding_device=router_core_2, intf_name=eth5, observer_device=pc_1_1_1_1, probe_dst_ip=10.200.0.3, seed=42 | End hosts receive TCP segments that fail their checksum. | 2/2 | pass |
| test | dhcp_missing_subnet | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | The client segment gets no offer, holds no address and loses its gateway. | 2/2 | pass |
| test | dhcp_service_down | m | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | The client segment gets no offer, holds no address and loses its gateway. | 2/2 | pass |
| test | dhcp_spoofed_dns | l | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | The client queries the leased resolver and names stop resolving. | 2/2 | pass |
| test | dhcp_spoofed_gateway | s | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1 | The client routes via the leased gateway and loses off-subnet hosts. | 2/2 | pass |
| test | dhcp_spoofed_subnet | s | scenario default | none | host_name=dhcp_server, host_name_2=pc_1_1_1_1, subnet=10.1.1.0 | The leased prefix no longer covers the gateway, so it is unreachable. | 2/2 | pass |
| test | dns_lookup_latency | m | scenario default | none | delay_ms=1000, host_name=dns_server, intf_name=eth0 | Name lookups take far longer than before; the pinned fetch stays fast. | 2/2 | pass |
| test | dns_port_blocked | l | scenario default | none | host_name=dns_server | Every fetch by name fails to resolve; the pinned fetch still loads. | 2/2 | pass |
| test | dns_record_error | m | scenario default | none | host_name=dns_server, target_domain=local, target_website=web1 | The client resolves the name to another address and the page fails. | 2/2 | pass |
| test | dns_service_down | m | scenario default | none | host_name=dns_server | Every fetch by name fails to resolve; the pinned fetch still loads. | 2/2 | pass |
| test | host_incorrect_dns | m | scenario default | none | host_name=pc_1_1_1_1 | Fetching the service by name works before inject and fails after. | 2/2 | pass |
| test | load_balancer_overload | l | scenario default | none | backend_cpu_host=backend_web_0, backend_probe_host=load_balancer, backend_url=http://20.200.0.2/small, client_host=pc_1_1_1_1, concurrency=200, control_url=http://web0.local/small, cpu_quota=0.2, duration_sec=300, host_name=load_balancer, load_client_hosts=pc_2_1_1_1, load_workers=4, probe_concurrency=4, probe_requests=60, vip_url=http://web99.local/small, warmup_sec=5 | Requests through the VIP slow down or fail; the parallel server does not. | 2/2 | pass |
| test | mac_address_conflict | s | scenario default | none | host_name=web_server_1, host_name_2=web_server_0 | Traffic to both holders of the MAC is lost; the control's is not. | 2/2 | pass |
| test | ospf_acl_block | s | scenario default | none | host_name=router_core_1 | Every remote OSPF neighbor of the router drops its adjacency. | 2/2 | pass |
| test | ospf_area_misconfiguration | m | scenario default | none | host_name=router_core_1 | Some remote OSPF neighbors of the router drop their adjacency. | 2/2 | pass |
| test | ospf_neighbor_missing | l | scenario default | none | host_name=router_core_1 | One remote OSPF neighbor drops its adjacency; the router keeps the others. | 2/2 | pass |

### `dc_clos`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | dns_lookup_latency | s | scenario default | none | delay_ms=1000, host_name=dns_pod0, intf_name=eth0 | Name lookups take far longer than before; the pinned fetch stays fast. | 2/2 | pass |
| dev | dns_port_blocked | m | scenario default | none | host_name=dns_pod0 | Every fetch by name fails to resolve; the pinned fetch still loads. | 2/2 | pass |
| dev | dns_record_error | l | scenario default | none | host_name=dns_pod0, target_domain=pod0, target_website=web0 | The client resolves the name to another address and the page fails. | 2/2 | pass |
| dev | dns_service_down | s | scenario default | none | host_name=dns_pod0 | Every fetch by name fails to resolve; the pinned fetch still loads. | 2/2 | pass |
| dev | host_incorrect_dns | s | scenario default | none | host_name=client_0 | Fetching the service by name works before inject and fails after. | 2/2 | pass |
| dev | web_dos_attack | m | scenario default | none | attacker_device=client_0, host_name=webserver0_pod0, observer_device=dns_pod0, probe_url=http://10.0.1.2/small.bin | The victim answers far slower or not at all; another server does not. | 2/2 | pass |
| test | arp_acl_block | m | scenario default | none | host_name=client_0 | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| test | host_incorrect_gateway | l | scenario default | none | host_name=client_0 | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| test | link_detach | m | scenario default | none | host_name=client_0, intf_name=eth0 | Traffic no longer crosses the link. | 2/2 | pass |
| test | receiver_resource_contention | s | scenario default | none | duration=3600, host_name=client_0 | The receiver's closed window holds a TCP stream back; its path is healthy. | 2/2 | pass |
| test | healthy | m | scenario default | none | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |
| test | healthy | l | scenario default | none | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |
| test | healthy | s | scenario default | none | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |

### `llmd_lab`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | host_incorrect_ip | none | scenario default | none | host_name=client | Peers can no longer reach the host at the address it should hold. | 2/2 | pass |
| dev | http_acl_block | none | scenario default | none | host_name=client | HTTP on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| dev | k8s_clusterip_routing_broken | none | scenario default | none | control_node=controller, node_name=controller | The node cannot use a Service VIP; its backend and other nodes still can. | 2/2 | pass |
| dev | k8s_networkpolicy_deny | none | scenario default | none | control_node=controller, control_url=http://200.0.0.8/, namespace=llm-d, pod_selector=gateway.networking.k8s.io/gateway-name=llm-d-gateway, symptom_host=client, symptom_url=http://llmd/v1/models | The isolated path stops answering HTTP while a control path still answers. | 2/2 | pass |
| dev | k8s_worker_apiserver_partition | none | scenario default | none | control_node=controller, node_name=worker1 | NotReady worker whose pods leave endpoints and refuse logs, still pingable. | 2/2 | pass |
| dev | receiver_resource_contention | none | scenario default | none | duration=3600, host_name=client | The receiver's closed window holds a TCP stream back; its path is healthy. | 2/2 | pass |
| test | host_ip_conflict | none | scenario default | none | host_name=client, host_name_2=web | Two hosts answer ARP for the same address, as a third node sees it. | 2/2 | pass |
| test | host_missing_ip | none | scenario default | none | host_name=client, intf_name=eth0 | Peers can no longer reach the host at the address it should hold. | 2/2 | pass |
| test | k8s_coredns_isolated | none | scenario default | none | control_node=controller, node_name=controller, symptom_host=worker1 | Another node's lookups fail while the CoreDNS pod stays reachable by IP. | 2/2 | pass |
| test | sender_resource_contention | none | scenario default | none | client_host=client, cpu_quota=0.05, dst_ip=200.0.0.8, duration=3600, host_name=web, large_url=http://200.0.0.8/large.bin, small_url=http://200.0.0.8/small.bin, stress_cpus=16 | Bulk downloads from the sender slow down; small objects and the path do not. | 2/2 | pass |
| test | healthy | none | scenario default | none | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |

### `isp_geant_ebgp_rpki`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | host_static_blackhole | m | kathara | device_profile=frr | host_name=at1_at | Hosts behind the router lose the prefix; other destinations stay up. | 2/2 | pass |
| test | bgp_rpki_invalid_route_leak | m | kathara | device_profile=frr | host_name=es1_es | The non-ROV observer accepts the leaked origin; the ROV observer rejects it. | 2/2 | pass |
| test | mtu_mismatch | m | kathara | device_profile=frr | host_name=at1_at, intf_name=eth0, mtu=500 | Small packets still cross, but the largest packet that does is far smaller. | 2/2 | pass |

### `isp_janos-us-ca`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | icmp_acl_block | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=pc_atlanta | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |

### `isp_abilene`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | icmp_frag_needed_filter_misconfiguration | s | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=atlam5 | Fragmentation Needed no longer crosses the node; other ICMP still does. | 2/2 | pass |
| test | bgp_max_prefix_exceeded | s | kathara | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=frr | flood_count=120, neighbor_ip=10.0.0.23, peer_name=kscyng, receiver_name=iplsng | The peer's session is down with the receiver's max-prefix Cease notification. | 2/2 | pass |

### `p4_dc_gateway`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | incast_traffic_network_limitation | m | scenario default | none | host_name=service_1_1, observer_device=client_1, probe_dst_ip=10.0.1.11 | Synchronized fan-in overflows the shallow egress queue: the port tail-drops while its average send rate stays well below the port rate, and a control flow that avoids the port stays healthy. | 2/2 | pass |
| dev | int_insufficient_mtu_headroom | s | scenario default | none | bmv2_port=2, host_name=gateway_1, int_mtu=1480, intf_name=eth1 | Near-MTU packets on the faulted port lose INT; small packets keep it. | 2/2 | pass |
| dev | p4_action_selector_member_misconfig | l | scenario default | none | host_name=leaf_1 | Flows hashed to the bad member leave on the bad port and are lost. | 2/2 | pass |
| dev | lb_pending_connection_update_race | m | scenario default | none | host_name=gateway_1, learning_delay_ms=8000, seed=42 | Connections opened just before a routine pool update break; learned ones do not. | 2/2 | pass |
| dev | p4_ecn_threshold_misconfiguration | s | scenario default | none | bmv2_port=2, host_name=gateway_1, intf_name=eth1, threshold=1024 | The same burst passes the probed port unmarked; a sibling port still marks. | 2/2 | pass |
| dev | p4_table_entry_missing | l | scenario default | none | host_name=gateway_1 | HTTP on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| dev | p4_tcam_entry_corruption | m | scenario default | none | control_source=client_2, host_name=gateway_3, target_ip=10.0.4.11 | Flows through the corrupt entry never reach a leaf; other flows still do. | 2/2 | pass |
| dev | silent_egress_packet_loss | s | scenario default | none | bmv2_port=2, host_name=gateway_1, intf_name=eth1, loss_basis_points=200, seed=42 | Flows on the faulted port lose packets; flows on other ports do not. | 2/2 | pass |
| dev | tcp_syn_flood_attack | m | scenario default | none | attacker_device=client_1, duration=3600, flows=100, rate_pps=1000, seed=42, target_ip=10.0.1.12, target_port=80 | Flood SYNs pile up half-open handshakes on the target listener only. | 2/2 | pass |
| test | bmv2_switch_down | s | scenario default | none | host_name=gateway_1 | HTTP on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| test | host_incorrect_ip | s | scenario default | none | host_name=client_1 | Peers can no longer reach the host at the address it should hold. | 2/2 | pass |
| test | http_acl_block | m | scenario default | none | host_name=client_1 | HTTP on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| test | icmp_acl_block | s | scenario default | none | host_name=client_1 | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| test | icmp_frag_needed_filter_misconfiguration | m | scenario default | none | host_name=gateway_1 | Fragmentation Needed no longer crosses the node; other ICMP still does. | 2/2 | pass |
| test | int_insufficient_mtu_headroom | m | scenario default | none | bmv2_port=2, host_name=gateway_1, int_mtu=1480, intf_name=eth1 | Near-MTU packets on the faulted port lose INT; small packets keep it. | 2/2 | pass |
| test | p4_table_resource_exhaustion | s | scenario default | none | host_name=leaf_1 | The full LPM table rejects a new entry; installed routes still forward. | 2/2 | pass |
| test | lb_pending_connection_update_race | l | scenario default | none | host_name=gateway_1, learning_delay_ms=8000, seed=42 | Connections opened just before a routine pool update break; learned ones do not. | 2/2 | pass |
| test | p4_action_selector_member_misconfig | s | scenario default | none | host_name=leaf_1 | Flows hashed to the bad member leave on the bad port and are lost. | 2/2 | pass |
| test | p4_ecmp_group_member_missing | m | scenario default | none | host_name=leaf_1 | The removed member's next hop receives none of the sweep; nothing is lost. | 2/2 | pass |
| test | p4_ecn_threshold_misconfiguration | l | scenario default | none | bmv2_port=10, host_name=spine_1, intf_name=eth9, threshold=1024 | The same burst passes the probed port unmarked; a sibling port still marks. | 2/2 | pass |
| test | p4_table_entry_misconfig | s | scenario default | none | host_name=leaf_1 | HTTP on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| test | p4_table_resource_exhaustion | l | scenario default | none | host_name=leaf_1 | The full LPM table rejects a new entry; installed routes still forward. | 2/2 | pass |
| test | p4_tcam_entry_corruption | s | scenario default | none | control_source=client_2, host_name=spine_1, target_ip=10.0.1.12 | Flows through the corrupt entry never reach a leaf; other flows still do. | 2/2 | pass |
| test | p4runtime_partial_write | m | scenario default | none | host_name=leaf_1 | Victim traffic lands on the local host, which is itself unreachable. | 2/2 | pass |
| test | p4runtime_pipeline_mismatch | l | scenario default | none | host_name=gateway_1 | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |
| test | silent_egress_packet_loss | m | scenario default | none | bmv2_port=2, host_name=gateway_1, intf_name=eth1, loss_basis_points=200, seed=42 | Flows on the faulted port lose packets; flows on other ports do not. | 2/2 | pass |
| test | tcp_syn_flood_attack | l | scenario default | none | attacker_device=client_1, duration=3600, flows=100, rate_pps=1000, seed=42, target_ip=10.0.4.12, target_port=80 | Flood SYNs pile up half-open handshakes on the target listener only. | 2/2 | pass |
| test | healthy | m | scenario default | none | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |

### `min3clos`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | link_capacity_bottleneck | none | scenario default | none | burst=16kb, host_name=client1, intf_name=eth1, limit=32kb, rate=512kbit | The receiver takes in far less than on the healthy link, but traffic flows. | 2/2 | pass |
| dev | healthy | none | scenario default | none | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |
| test | bgp_asn_misconfig | none | scenario default | none | host_name=leaf1 | Remote peers refuse the router's OPEN with "Bad Peer AS"; sessions stay down. | 2/2 | pass |

### `isp_ta2`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | link_detach | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=n10, intf_name=eth0, peer_host=pc_n2, probe_dst_ip=10.254.0.46, symptom_host=pc_n10 | Traffic no longer crosses the link. | 2/2 | pass |

### `isp_dfn-bwin`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | link_down | s | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=berlin, intf_name=eth0, peer_host=pc_frankfurt, probe_dst_ip=10.254.0.6, symptom_host=pc_berlin | Traffic no longer crosses the link. | 2/2 | pass |
| dev | healthy | s | containerlab | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=nokia_srlinux | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |

### `isp_nobel-germany`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | link_flap | m | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | down_time=1, host_name=berlin, intf_name=eth0, peer_host=pc_hamburg, probe_dst_ip=10.254.0.26, symptom_host=pc_berlin, up_time=1 | Echo loss comes in repeated outages separated by recoveries. | 2/2 | pass |

### `isp_india35`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | link_packet_corruption | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | corruption_percentage=10, host_name=n_0, intf_name=eth0, peer_host=pc_n_24, probe_dst_ip=10.254.0.70, symptom_host=pc_n_0 | Corrupted frames are dropped: echoes are lost and TCP goodput collapses. | 2/2 | pass |

### `isp_ta1`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | ospf_acl_block | m | kathara | igp=ospf, bgp_mode=none, rpki=False, device_profile=frr | host_name=n1 | Every remote OSPF neighbor of the router drops its adjacency. | 2/2 | pass |

### `isp_germany50`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | ospf_area_misconfiguration | l | kathara | igp=ospf, bgp_mode=none, rpki=False, device_profile=frr | host_name=aachen | Some remote OSPF neighbors of the router drop their adjacency. | 2/2 | pass |
| test | frr_service_down | l | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | host_name=aachen | Every remote OSPF, IS-IS, or BGP neighbor of the router loses its adjacency. | 2/2 | pass |

### `isp_dfn-bwin_ebgp_rtbh`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | ospf_neighbor_missing | s | kathara | device_profile=frr | host_name=berlin | One remote OSPF neighbor drops its adjacency; the router keeps the others. | 2/2 | pass |
| test | bgp_blackhole_community_leak | s | kathara | device_profile=frr | host_name=frankfurt, peer_host=pc_frankfurt, probe_dst_ip=198.51.100.1, symptom_host=hamburg | Ping on the scenario probe path works before inject and fails after; the control path, if any, stays up. | 2/2 | pass |

### `isp_di-yuan`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | healthy | s | containerlab | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=nokia_srlinux | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |
| dev | healthy | s | containerlab | igp=isis, bgp_mode=none, rpki=False, device_profile=nokia_srlinux | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |
| dev | healthy | s | containerlab | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=nokia_srlinux | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |
| test | link_capacity_bottleneck | s | kathara | igp=isis, bgp_mode=none, rpki=False, device_profile=frr | burst=16kb, host_name=n_3, intf_name=eth4, limit=32kb, peer_host=pc_n_10, probe_dst_ip=10.254.0.6, rate=512kbit, symptom_host=pc_n_3 | The receiver takes in far less than on the healthy link, but traffic flows. | 2/2 | pass |

### `isp_pdh`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | healthy | s | containerlab | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=nokia_srlinux | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |
| dev | healthy | s | containerlab | igp=isis, bgp_mode=none, rpki=False, device_profile=nokia_srlinux | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |
| dev | healthy | s | containerlab | igp=ospf, bgp_mode=ebgp, rpki=False, device_profile=nokia_srlinux | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |
| test | bgp_acl_block | s | containerlab | igp=isis, bgp_mode=ibgp_rr, rpki=False, device_profile=nokia_srlinux | host_name=n1 | Every remote BGP peer of the router loses its session. | 2/2 | pass |

### `isp_dfn-gwin`

| Split | Fault | Scale | Backend | Design | Inject | Symptom checked | Repeats passed | Admission |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dev | healthy | s | containerlab | igp=isis, bgp_mode=none, rpki=False, device_profile=nokia_srlinux | none | Healthy lab: the scenario checks pass, no symptom. | 2/2 | pass |
