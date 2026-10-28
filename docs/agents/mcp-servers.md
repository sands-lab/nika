# MCP servers

Use this reference to find the tools available during an agent diagnosis session. The MCP gateway exposes session-scoped HTTP endpoints. Agents load their URLs with `DiagnosisAgent.diagnosis_mcp_config()`; see [Python MCP client](agent-reference.md#python-mcp-client) for a client example.

The [server registry](../../src/nika/mcp/registry.py) selects diagnosis servers from the scenario name, tags, and backend. Every diagnosis session receives `kathara_base_mcp_server`, `pingmesh_mcp_server`, and `packet_capture_mcp_server`. The base server works with Kathara and Containerlab despite its name. The gateway exposes `task_mcp_server` only after the agent advances to submission.

## Run commands on lab nodes

Use `exec_shell(host_name, command, timeout=10)` for ordinary commands. Set `timeout` in seconds when a command such as `kubectl` needs longer. Kathara limits each command result to 4,000 characters, so request bounded output with options such as `--no-headers`, `--tail`, or `-o jsonpath`. Commands run inside the named lab node and remain subject to the session's node access policy.

| Task | Node | Command |
| --- | --- | --- |
| Check a route | `router1` | `ip route` or `vtysh -c 'show ip route'` |
| Probe a destination | `pc1` | `ping -c 2 195.11.14.1` |
| Inspect interfaces | `router1` | `ip -s addr` |
| Read a file | `router1` | `cat /etc/frr/frr.conf` |
| Inspect Kubernetes nodes | `controller` | `kubectl get nodes --no-headers` |
| Inspect Kubernetes Pods | `controller` | `kubectl get pods -A --no-headers` |

`exec_shell` is the main tool for targeted checks. The base server also exposes `curl_web_test` for repeated HTTP timing measurements (a normal client: it sends `Accept-Encoding`, and each line ends with `http_code` and `size_download`), `iperf_test` for a two-node throughput test, and `active_tcp_probe` for deterministic TCP traffic over a selected five-tuple.

Both nodes need `python3` for `active_tcp_probe`. Missing dependencies return
`status: unsupported`; connection failures return `status: failed` with an
error. Successful probes return `acked` and `elapsed_ms` in `result`.

## Use dedicated diagnosis tools

| Server | When selected | Tools and purpose |
| --- | --- | --- |
| `pingmesh_mcp_server` | Every diagnosis | `run_pingmesh_snapshot` probes endpoint pairs and returns the summary and anomalies first. Pairs are sampled evenly across sources, and the summary reports `candidate_pairs` and whether `max_pairs` capped the probe. Pairs a scenario isolates by design are probed separately and are anomalies (`policy_violation`) only when reachable. The latency threshold never drops below the scenario's design RTT. Healthy per-pair rows appear only with `include_healthy=true`. A pair that times out or fails is reported as that pair's `unknown` anomaly; the rest of the snapshot still returns. |
| `packet_capture_mcp_server` | Every diagnosis | `packet_capture_start`, `packet_capture_stop`, and `packet_capture_inspect` collect bounded packet evidence on a node. |
| `kathara_frr_mcp_server` | Kathara scenarios tagged `rpki` | `frr_get_rpki_status` gathers RTR cache state and optional prefix validation evidence. |
| `kathara_iosxr_mcp_server` | Kathara IOS-XR scenarios | `iosxr_exec` runs commands through the XR CLI path. |
| `kathara_routeros_mcp_server` | Kathara RouterOS scenarios | `routeros_exec` reaches RouterOS through its internal management hop. |
| `containerlab_srl_mcp_server` | Containerlab routing scenarios | `srl_exec_cli` runs commands through SR Linux CLI. |
| `kathara_bmv2_mcp_server` | Kathara P4 scenarios | `p4rt_exec` runs read-only P4Runtime queries through the fabric controller `fabric_mgr`. |
| `kathara_sdn_mcp_server` | Kathara SDN scenarios | `sdn_onos_rest` queries ONOS REST through `fabric_mgr`. |
| `kathara_telemetry_mcp_server` | Kathara telemetry scenarios | `int_query_telemetry` filters observed INT-MX packet and hop records. |
| `kathara_netflow_mcp_server` | Sessions started with `telemetry: [netflow]` | `netflow_query` reads and aggregates flow records stored by `telemetry_collector` (IPFIX, NetFlow, or sFlow). |
| `kathara_prometheus_mcp_server` | Sessions started with `telemetry: [prometheus]` | `prometheus_query` runs PromQL against the device metrics Prometheus stores on `telemetry_collector`. |
| `kathara_syslog_mcp_server` | Sessions started with `telemetry: [syslog]` | `syslog_query` reads device syslog stored by `telemetry_collector`. |
| `k8s_mcp_server` | Kubernetes scenarios when `nika.k8s.access` permits MCP | `k8s_list_events` queries session-scoped Kubernetes events. |

### SDN (`kathara_sdn_mcp_server`)

Call `sdn_onos_rest` with paths such as `/onos/v1/devices`, `/onos/v1/flows`, or `/onos/v1/statistics/ports`. Use `exec_shell` on a switch for `ovs-ofctl` or `ovs-vsctl`, and on `onos` to inspect controller logs. Enable [NetFlow and Prometheus telemetry](../operations/network-scenarios.md#telemetry) when the agent should query stored flow records and OpenFlow port time series.

### P4 / BMv2 (`kathara_bmv2_mcp_server`)

Call `p4rt_exec` with `read`, `read --switch leaf_1`, `read-table --switch leaf_1 --table ipv4_lpm`, or `counters`; `--help` lists these commands. The tool rejects every other subcommand and option, so an agent cannot change switch state through it. Its response removes private post-counter fault fields. Use `exec_shell` for ordinary switch and controller commands, such as reading the controller's write log in `/var/log/p4rt/controller.log`. Enable [Prometheus telemetry](../operations/network-scenarios.md#telemetry) when the agent should query pipeline-counter time series. On `p4_dc_gateway`, `int_query_telemetry` returns observed INT-MX traces.

### Telemetry (`kathara_telemetry_mcp_server`)

Call `int_query_telemetry` with a start time and optional flow or packet filters. It reads observed INT-MX traces from the collector. The `p4_dc_gateway` scenario selects this server.

### NetFlow (`kathara_netflow_mcp_server`)

Call `netflow_query` with a time window, for example `start_time="-600"` for the last ten minutes. To narrow the results, pass `exporter` (a device name) and an nfdump `filter` such as `proto udp and dst ip 10.254.0.6`. To sum packets, bytes, and flows, set `aggregate_by` to one or more of `router`, `inif`, `proto`, `srcip`, `srcport`, `dstip`, and `dstport`. Rows are sorted by `order_by` (`bytes`, `packets`, or `flows`), and `limit` sets how many are returned.

Devices export only the traffic they receive, so a flow appears once for each device on its path. On FRR exporters, `in_if` is the kernel ifindex; `ip -o link` maps it to a name. On OVS exporters in `sdn_l3_clos`, `in_if` is the OpenFlow port; `ovs-ofctl dump-ports-desc` maps it. On Containerlab SR Linux, `in_if` is the sFlow ifIndex. On Kathara RouterOS, `in_if` is the traffic-flow interface index. Records become queryable about 20 seconds after the traffic. The server is selected when a session enables `netflow` [telemetry](../operations/network-scenarios.md#telemetry).

### Prometheus (`kathara_prometheus_mcp_server`)

Call `prometheus_query` with a PromQL expression. Each series carries the device as `node`; `device` is an interface name on Linux, OVS, SR Linux, IOS XR, and RouterOS series, and `neighbor` is a BGP peer. Omit `start_time` for an instant query. With `start_time` (for example `"-600"`), the tool returns a range at `step` resolution. `limit` sets how many series are returned. Kathara exporters are scraped every 5 seconds; SR Linux uses a 15-second scrape. `up` shows whether a device answers its scrape. The server is selected when a session enables `prometheus` [telemetry](../operations/network-scenarios.md#telemetry).

| Query | What it shows |
| --- | --- |
| `node_network_up == 0` | Linux interfaces that are down |
| `rate(node_network_receive_bytes_total{node="chinng"}[30s])` | Receive rate on one Linux node |
| `frr_bgp_peer_up{node="leaf_router_0_0"}` | FRR BGP session |
| `openflow_port_rx_bytes{node="leaf_1"}` | OVS port counters |
| `p4_ingress_packets{node="spine_1"}` | BMv2 pipeline counters |
| `srl_interface_in_packets{node="leaf1"}` | SR Linux interface packets |
| `srl_bgp_peer_up{node="leaf1"}` | SR Linux BGP session |
| `xr_interface_in_packets{node="router1"}` | XRd interface packets |
| `xr_bgp_peer_up{node="router1"}` | XRd BGP session |
| `routeros_interface_rx_packets{node="router1"}` | RouterOS interface packets |

Linux nodes export the network stack: `node_network_*`, `node_netstat_*`, `node_sockstat_*`, `node_nf_conntrack_*`, and `node_arp_*`. FRR also exports `frr_bgp_peer_up` and `frr_bgp_peer_prefixes`. OVS exports `openflow_port_*`. P4 fabrics export `p4_ingress_*` and `p4_egress_*`, and on the gateway pipeline SYN and non-SYN flow totals. SR Linux exports `srl_interface_*` and `srl_bgp_peer_up`. XRd exports `xr_interface_*` and `xr_bgp_peer_*`. RouterOS exports `routeros_interface_*` and `routeros_bgp_peer_up`.

### Syslog (`kathara_syslog_mcp_server`)

Call `syslog_query` with a time window, for example `start_time="-600"` for the last ten minutes. Pass `host` for one device, `program` for a syslog tag such as `bgpd`, and `contains` for a message substring. Rows are `{time, host, program, message}`. Linux and OVS devices forward `/dev/log` over the management VRF after FRR `log syslog`. SR Linux sends syslog over UDP 514 from the mgmt network-instance on the lab management network. XRd uses that Linux forwarder because extra Kathara NICs stay on Linux. RouterOS uses a remote logging action. The server is selected when a session enables `syslog` [telemetry](../operations/network-scenarios.md#telemetry).

### Kubernetes (`k8s_mcp_server`)

Call `k8s_list_events(namespace=..., limit=...)` for event evidence. It returns JSON that stays within the MCP output budget: long messages are shortened and extra events are dropped, so the payload stays parseable. Use `exec_shell` on `controller` for other Kubernetes operations, such as `kubectl get nodes --no-headers`, `kubectl describe pod ...`, or `kubectl logs ... --tail=20`. Keep command output below the result limit and set a longer timeout for slow API calls. The server is selected when `nika.k8s.access` is `auto` or `mcp`; `kubectl_only` omits it.

## Capture packets

Capture needs `dumpcap` or `tcpdump` on the node. Inspection uses the node's `tshark` when available, otherwise the host's `tshark`. Capture files and metadata stay on the node.

1. Call `packet_capture_start(device, interface, capture_filter=..., max_duration_sec=..., max_packets=...)`. It returns a `capture_id`.
2. Run traffic with `exec_shell`, `active_tcp_probe`, or the scenario's traffic workflow.
3. Call `packet_capture_stop(capture_id)`.
4. Call `packet_capture_inspect(capture_id, view="summary", limit=...)` or use the `packets`, `protocol`, or `expert` view. Pass a Wireshark display filter when needed.

Set bounded duration and packet limits. The capture and metadata remain on the node; the stop result includes the container path.

A valid capture with zero packets still returns an artifact. If no nonempty
capture file exists, stopping fails and marks the capture as failed; start a
new capture rather than inspecting the failed one.

`interface` must exist on the node; otherwise `packet_capture_start` fails and lists the node's interfaces. On SR Linux nodes, `ethernet-1/N`, `ethernet-1/N.0`, `e1-N`, and the plan name `eth{N-1}` all capture on data port `e1-N`, which carries both transit and control-plane traffic.

## Submit a diagnosis

`task_mcp_server` exposes `submit` after `begin_submission_mcp_phase(session_id, diagnosis_report)`. Read `resource_id` and `fault_type` values from the frozen submission context, then call `submit` with `is_anomaly` and `root_causes: [{resource_id, fault_type}, ...]`. The server accepts only catalog IDs. See [Root-cause ground truth and scoring](../benchmarks/root-cause-evaluation.md).
