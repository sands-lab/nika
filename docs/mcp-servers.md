# MCP servers

Agent implementers use this reference to see which MCP servers a diagnosis session mounts, which tools each server exposes, and how to capture packets or submit a root cause.

You reach tools through the session MCP gateway over HTTP. Load server URLs with `agent.utils.mcp_client.load_session_mcp_config`. Wire the client in [Custom agent integration](custom-agents.md). Set gateway host, port, and timeouts in [Run configuration](configuration.md).

Source of truth: [`registry.py`](../src/nika/service/mcp_server/registry.py). Server modules live under [`mcp_server/`](../src/nika/service/mcp_server/) and [`k8s_mcp_server/`](../src/nika/service/k8s_mcp_server/).

## Which servers a session mounts

`select_diagnosis_servers(scenario_name, backend=...)` returns these three on every diagnosis session:

- `kathara_base_mcp_server`
- `pingmesh_mcp_server`
- `packet_capture_mcp_server`

The same function appends optional servers from scenario name and net-env `TAGS` tokens, then keeps only servers that match the lab backend (`kathara` or `containerlab`). Session client config also includes `task_mcp_server`. During diagnosis the gateway phase gate opens the diagnosis servers. After `begin_submission_mcp_phase`, it opens only `task_mcp_server`.

| Token set (name + TAGS) | Keywords | Server |
| --- | --- | --- |
| Routing | `bgp`, `ebgp`, `ospf`, `rip`, `frr`, `routing`, `rpki` | `kathara_frr_mcp_server` (Kathara) or `containerlab_srl_mcp_server` (Containerlab) |
| Switch / P4 | `p4`, `bmv2`, `bloom`, `mpls`, `int`, `counter` | `kathara_bmv2_mcp_server` |
| SDN | `sdn` | `kathara_sdn_mcp_server` |
| Telemetry | `telemetry` | `kathara_telemetry_mcp_server` |
| Kubernetes | `kubernetes`, `k3s`, `k8s` | `k8s_mcp_server` when `nika.k8s.access` is `auto`, `mcp`, or empty |

Lab topology and deploy steps live in [Network scenarios](network-scenarios.md).

## Always-on diagnosis servers

| Name | Role | Main tools |
| --- | --- | --- |
| `kathara_base_mcp_server` | host | `get_reachability`, `ping_pair`, `traceroute`, `systemctl_ops`, `get_host_net_config`, `get_tc_statistics`, `netstat`, `ip_addr_statistics`, `ethtool`, `curl_web_test`, `iperf_test`, `active_tcp_probe`, `cat_file`, `exec_shell`, `exec_shell_dual` |
| `pingmesh_mcp_server` | host | `run_pingmesh_snapshot` |
| `packet_capture_mcp_server` | observability | `packet_capture_start`, `packet_capture_stop`, `packet_capture_inspect` |

Install `tshark` on the NIKA controller before you call inspect. Lab nodes prefer `dumpcap`, then fall back to `tcpdump`.

## Kathara optional servers

| Name | When mounted | Role | Main tools |
| --- | --- | --- | --- |
| `kathara_frr_mcp_server` | Kathara + routing tokens | routing | `frr_get_bgp_conf`, `frr_show_running_config`, `frr_show_ip_route`, `frr_get_ospf_conf`, `frr_exec`, `frr_get_routing_state`, `frr_get_rpki_status` |
| `kathara_bmv2_mcp_server` | Switch / P4 tokens | switch | `p4_get_runtime_state` |
| `kathara_sdn_mcp_server` | SDN token | switch | `sdn_get_fabric_state`, `sdn_controller_logs`, `sdn_endpoint_reachability` |
| `kathara_telemetry_mcp_server` | Telemetry token | telemetry | `int_query_telemetry` |
| `k8s_mcp_server` | Kubernetes tokens and `nika.k8s.access` ≠ `kubectl_only` | kubernetes | `k8s_list_nodes`, `k8s_get_node`, `k8s_list_pods`, `k8s_get_pod`, `k8s_get_logs`, `k8s_list_events`, `k8s_list_services`, `k8s_get_endpoints`, `k8s_get_network_policies`, `k8s_dns_query`, `k8s_check_connectivity` |

### SDN (`kathara_sdn_mcp_server`)

On `sdn` scenarios such as `sdn_l3_clos`, the session includes this server. Call `sdn_get_fabric_state` for ONOS topology and apps, the live ONOS flow and group store, and observed OVS flows, groups, ports, and status. Use `sdn_endpoint_reachability` for a probe and `sdn_controller_logs` for a log tail. Each response keeps controller-live evidence and switch-observed evidence in separate sections.

### P4 / BMv2 (`kathara_bmv2_mcp_server`)

On Switch / P4 scenarios such as `p4_dc_fabric` and `p4_dc_gateway`, the session includes this server. Call `p4_get_runtime_state` for the live pipeline, LPM entries, selector members and groups, counters, port and queue data, flow statistics, and ECN configuration. Private post-counter failure tables and registers on gateway pipelines stay out of agent responses.

### Telemetry (`kathara_telemetry_mcp_server`)

On scenarios with the `telemetry` token (INT labs such as `p4_dc_gateway`), the session includes this server. Call `int_query_telemetry` for observed INT-MX traces keyed by flow and packet ID, then judge path correctness from those traces.

### Kubernetes (`k8s_mcp_server`)

On Kubernetes-token scenarios, the session includes this server when `nika.k8s.access` is `auto`, `mcp`, or empty. After verification you receive a session kubeconfig and call the listed `k8s_*` tools against that API. Typical labs: `k8s_lab`, `llmd_lab`.

## Containerlab optional servers

| Name | When mounted | Role | Main tools |
| --- | --- | --- | --- |
| `containerlab_srl_mcp_server` | Containerlab + routing tokens | routing | `srl_exec_cli`, `srl_get_bgp_as`, `srl_show_running_config`, `srl_show_bgp_summary`, `srl_show_ip_route` |

## Submission server

| Name | When available | Role | Main tools |
| --- | --- | --- | --- |
| `task_mcp_server` | Every session; usable after phase advance | task | `submit` |

1. Call `begin_submission_mcp_phase(session_id, diagnosis_report)`.
2. Read `resource_id` and `fault_type` values from the frozen submission context in the prompt (or `load_submission_context`).
3. Call `submit` once with `is_anomaly` and `root_causes: [{resource_id, fault_type}, ...]`.

`submit` accepts only IDs present in those catalogs. Scoring details: [Root-cause ground truth and scoring](root-cause-evaluation.md).

## Packet capture workflow

Every diagnosis session includes `packet_capture_mcp_server`. Use it when you need bounded packet evidence:

1. `packet_capture_start(device, interface, capture_filter=..., max_duration_sec=..., max_packets=...)`: start async capture. Pass a BPF (libpcap) filter plus duration or packet caps.
2. Run probes (`ping_pair`, `active_tcp_probe`, or scenario traffic) while capture runs.
3. `packet_capture_stop(capture_id)`: stop capture and write `capture.pcapng` under the session directory.
4. `packet_capture_inspect(capture_id, view=..., display_filter=..., limit=..., offset=...)`: page through `summary`, `packets`, `protocol`, or `expert` with a Wireshark display filter.

Set capture limits on each call; values above the hard ceilings fail. Use BPF at start and Wireshark display filters at inspect. Default inspect pages return protocol fields without application payload.

## Related docs

| Topic | Doc |
| --- | --- |
| Agent helpers and registration | [Custom agent integration](custom-agents.md) |
| `nika.mcp.*`, `nika.k8s.access` | [Run configuration](configuration.md) |
| Sandbox MCP endpoints | [Docker Sandbox execution](agent-sandbox.md) |
| Remote lab MCP gateway | [Remote lab execution](remote.md) |
| Scenario topology and deploy | [Network scenarios](network-scenarios.md) |
| Submit schema and scoring | [Root-cause ground truth and scoring](root-cause-evaluation.md) |
