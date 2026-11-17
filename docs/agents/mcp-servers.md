# MCP servers

During diagnosis, an agent investigates the live network through MCP tools. This page shows which tools a diagnosis can use and what each one returns. Your agent gets the server URLs from `DiagnosisAgent.diagnosis_mcp_config()`; see the [Python MCP client](agent-reference.md#python-mcp-client) for a client example.

Every diagnosis gets the general tools below. NIKA adds more servers for the environment and telemetry the session uses. The [tool table](#use-dedicated-diagnosis-tools) lists them.

## Run commands on environment nodes

Use `exec_shell(host_name, command, timeout=10)` for ordinary commands. `timeout` is in seconds; raise it for slow commands such as `kubectl`. Output is subject to the [tool output limit](#tool-output-limit), so ask for bounded output with options such as `--no-headers`, `--tail`, or `-o jsonpath`.

| Task | Node | Command |
| --- | --- | --- |
| Check a route | `router1` | `ip route` or `vtysh -c 'show ip route'` |
| Probe a destination | `pc1` | `ping -c 2 195.11.14.1` |
| Inspect interfaces | `router1` | `ip -s addr` |
| Read a file | `router1` | `cat /etc/frr/frr.conf` |
| Inspect Kubernetes nodes | `controller` | `kubectl get nodes --no-headers` |
| Inspect Kubernetes Pods | `controller` | `kubectl get pods -A --no-headers` |

`exec_shell` is the main tool for targeted checks. The general server also provides:

- `curl_web_test` for repeated HTTP timing measurements. It sends `Accept-Encoding`, and each line ends with `http_code` and `size_download`.
- `iperf_test` for a throughput test between two nodes.
- `active_tcp_probe` for deterministic TCP traffic over a chosen five-tuple. It takes node names for `source` and `destination` and runs the listener on `destination`. By default, the source connects to the destination's data-plane address. Set `destination_address` to another IPv4 or IPv6 address the destination owns, such as a loopback. Both nodes need `python3`. Missing dependencies return `status: unsupported`. Connection failures and missing acks return `status: failed`, with an error.

## Capture packets

Packet capture needs `dumpcap` or `tcpdump` on the node. Inspection uses the node's `tshark` when present. Otherwise NIKA runs it in a temporary container, so no host `tshark` is needed. Capture files stay on the node.

1. Call `packet_capture_start(device, interface, capture_filter=..., max_duration_sec=..., max_packets=...)`. It returns a `capture_id`.
2. Run traffic with `exec_shell`, `active_tcp_probe`, or the environment's traffic workflow.
3. Call `packet_capture_stop(capture_id)`.
4. Call `packet_capture_inspect(capture_id, view="summary", limit=...)`, or use the `packets`, `protocol`, or `expert` view. Pass a Wireshark display filter when you need one.

Set a duration and packet limit on every capture. A valid capture with zero packets still returns an artifact. If no non-empty capture file exists, stopping fails and marks the capture as failed. Start a new capture rather than inspecting the failed one.

`interface` must exist on the node. Otherwise `packet_capture_start` fails and lists the node's interfaces. On SR Linux nodes, `ethernet-1/N`, `ethernet-1/N.0`, `e1-N`, and the plan name `eth{N-1}` all capture data port `e1-N`, which carries transit and control-plane traffic. On SONiC nodes, port `EthernetN` captures on the container interface `eth{N/4+1}` that carries it, the same interface the topology names.

---

The rest of this page is reference: which servers each environment adds, and what each tool returns.

## Use dedicated diagnosis tools

NIKA selects these servers from the environment's backend, router platform, and telemetry. The `pingmesh_mcp_server` and `packet_capture_mcp_server` are part of every diagnosis, as is the general server above.

| Server | When selected | Tools and purpose |
| --- | --- | --- |
| `pingmesh_mcp_server` | Every diagnosis | `run_pingmesh_snapshot` probes endpoint pairs and returns the summary and anomalies first. Pairs are sampled evenly across sources. The summary reports `candidate_pairs` and whether the probe was capped by `max_pairs`, or by the snapshot's time budget at a high `count`. Pairs that the environment isolates by design are probed separately, and reported as `policy_violation` anomalies only when they are reachable. The latency threshold never drops below the environment's design RTT. Healthy pairs appear only with `include_healthy=true`. A pair that times out or fails is reported as that pair's `unknown` anomaly, and the rest of the snapshot still returns. |
| `packet_capture_mcp_server` | Every diagnosis | `packet_capture_start`, `packet_capture_stop`, and `packet_capture_inspect` collect bounded packet evidence on a node. |
| `kathara_frr_mcp_server` | Kathara environments tagged `rpki` | `frr_get_rpki_status` gathers RTR cache state and optional prefix validation evidence. |
| `kathara_iosxr_mcp_server` | Kathara IOS-XR environments | `iosxr_exec` runs commands through the XR CLI. |
| `kathara_routeros_mcp_server` | Kathara RouterOS environments | `routeros_exec` reaches RouterOS through its management hop. |
| `containerlab_srl_mcp_server` | Containerlab routing environments on SR Linux | `srl_exec_cli` runs commands through the SR Linux CLI. |
| `containerlab_sonic_mcp_server` | Containerlab routing environments on SONiC (`device_profile: sonic`) | `sonic_exec_cli` runs one SONiC CLI command: `show`, `config`, `vtysh`, `redis-cli`, or `sonic-db-cli`. It rejects shell operators such as pipes; use `exec_shell` for those. |
| `kathara_bmv2_mcp_server` | Kathara P4 environments | `p4rt_exec` runs read-only P4Runtime queries through the fabric controller `fabric_mgr`. |
| `kathara_sdn_mcp_server` | Kathara SDN environments | `sdn_onos_rest` queries ONOS REST through `fabric_mgr`. |
| `kathara_telemetry_mcp_server` | Kathara telemetry environments | `int_query_telemetry` filters observed INT-MX packet and hop records. |
| `kathara_netflow_mcp_server` | Sessions started with `telemetry: [netflow]` | `netflow_query` reads and aggregates flow records that `telemetry_collector` stores (IPFIX, NetFlow, or sFlow). |
| `kathara_prometheus_mcp_server` | Sessions started with `telemetry: [prometheus]` | `prometheus_query` runs PromQL against the device metrics that Prometheus stores on `telemetry_collector`. |
| `kathara_syslog_mcp_server` | Sessions started with `telemetry: [syslog]` | `syslog_query` reads device syslog stored by `telemetry_collector`. |
| `k8s_mcp_server` | Kubernetes environments when `nika.k8s.access` is `auto` or `mcp` | `k8s_list_events` returns session-scoped Kubernetes events. With `kubectl_only`, the server is not registered. |

For the telemetry options, see [telemetry](../operations/network-scenarios.md#telemetry).

### SDN (`kathara_sdn_mcp_server`)

Call `sdn_onos_rest` with paths such as `/onos/v1/devices`, `/onos/v1/flows`, or `/onos/v1/statistics/ports`. Use `exec_shell` on a switch for `ovs-ofctl` or `ovs-vsctl`, and on `onos` for controller logs. Enable [NetFlow and Prometheus telemetry](../operations/network-scenarios.md#telemetry) when the agent should query stored flow records and OpenFlow port time series.

### P4 / BMv2 (`kathara_bmv2_mcp_server`)

Call `p4rt_exec` with `read`, `read --switch leaf_1`, `read-table --switch leaf_1 --table ipv4_lpm`, or `counters`. `--help` lists these commands. The tool rejects every other subcommand and option, so an agent cannot change switch state through it. Use `exec_shell` for ordinary switch and controller commands, such as reading the controller's write log in `/var/log/p4rt/controller.log`. Enable [Prometheus telemetry](../operations/network-scenarios.md#telemetry) to query pipeline counter time series. On `p4_dc_gateway`, `int_query_telemetry` returns observed INT-MX traces.

### Telemetry (`kathara_telemetry_mcp_server`)

Call `int_query_telemetry` with a start time and optional flow or packet filters. It reads observed INT-MX traces from the collector and returns `records`, `offset`, and `next_offset`. To retrieve older traces, pass `next_offset` with the same filters and a fixed `end_time`. Pages fit the tool output budget, and the default requests up to 20 records. A single trace that exceeds the budget returns an explicit error. The `p4_dc_gateway` environment selects this server.

### NetFlow (`kathara_netflow_mcp_server`)

Call `netflow_query` with a time window, for example `start_time="-600"` for the last ten minutes. To narrow the results, pass `exporter` (a device name) and an nfdump `filter` such as `proto udp and dst ip 10.254.0.6`. To sum packets, bytes, and flows, set `aggregate_by` to one or more of `router`, `inif`, `proto`, `srcip`, `srcport`, `dstip`, and `dstport`. Rows are sorted by `order_by` (`bytes`, `packets`, or `flows`), and `limit` sets how many rows are returned.

Each device exports only the traffic it receives, so a flow appears once for each device on its path. The meaning of `in_if` depends on the device: the kernel ifindex on FRR and Containerlab SONiC, the OpenFlow port on OVS in `sdn_l3_clos`, the sFlow ifIndex on SR Linux, and the traffic-flow interface index on RouterOS. Use `ip -o link` or `ovs-ofctl dump-ports-desc` to map an index to a name. Records become queryable about 20 seconds after the traffic. The server is selected when a session enables `netflow` [telemetry](../operations/network-scenarios.md#telemetry).

### Prometheus (`kathara_prometheus_mcp_server`)

Call `prometheus_query` with a PromQL expression. Each series carries the device as `node`. `device` is an interface name on Linux, OVS, SR Linux, SONiC, IOS XR, and RouterOS series, and `neighbor` is a BGP peer. Omit `start_time` for an instant query. With `start_time` (for example `"-600"`), the tool returns a range at `step` resolution. `limit` sets how many series are returned. Metrics are scraped at the `nika.metrics.scrape_interval` setting (default `15s`). `up` shows whether a device answers its scrape. The server is selected when a session enables `prometheus` [telemetry](../operations/network-scenarios.md#telemetry).

| Query | What it shows |
| --- | --- |
| `node_network_up == 0` | Linux interfaces that are down |
| `rate(node_network_receive_bytes_total{node="chinng"}[30s])` | Receive rate on one Linux node |
| `frr_bgp_peer_up{node="leaf_1"}` | FRR BGP session |
| `openflow_port_rx_bytes{node="leaf_1"}` | OVS port counters |
| `p4_ingress_packets{node="spine_1"}` | BMv2 pipeline counters |
| `srl_interface_in_packets{node="leaf1"}` | SR Linux interface packets |
| `srl_bgp_peer_up{node="leaf1"}` | SR Linux BGP session |
| `sonic_interface_in_packets{node="leaf1"}` | SONiC port packets |
| `xr_interface_in_packets{node="router1"}` | XRd interface packets |
| `xr_bgp_peer_up{node="router1"}` | XRd BGP session |
| `routeros_interface_rx_packets{node="router1"}` | RouterOS interface packets |

Linux nodes export the network stack: `node_network_*`, `node_netstat_*`, `node_sockstat_*`, `node_nf_conntrack_*`, and `node_arp_*`. FRR also exports `frr_bgp_peer_up` and `frr_bgp_peer_prefixes`. OVS exports `openflow_port_*`. P4 fabrics export `p4_ingress_*` and `p4_egress_*`, and the gateway pipeline also exports SYN and non-SYN flow totals. SR Linux exports `srl_interface_*` and `srl_bgp_peer_up`. SONiC exports `sonic_interface_*` port counters from COUNTERS_DB, the same data that `show interfaces counters` reads, plus `frr_bgp_peer_up` and `frr_bgp_peer_prefixes`. XRd exports `xr_interface_*` and `xr_bgp_peer_*`. RouterOS exports `routeros_interface_*` and `routeros_bgp_peer_up`.

### Syslog (`kathara_syslog_mcp_server`)

Call `syslog_query` with a time window, for example `start_time="-600"` for the last ten minutes. Pass `host` for one device, `program` for a syslog tag such as `bgpd`, and `contains` for a message substring. Each row has `time`, `host`, `program`, and `message`. The server is selected when a session enables `syslog` [telemetry](../operations/network-scenarios.md#telemetry).

### Kubernetes (`k8s_mcp_server`)

Call `k8s_list_events(namespace=..., limit=...)` for event evidence. It returns one JSON object per event. Use `exec_shell` on `controller` for other Kubernetes operations, such as `kubectl get nodes --no-headers`, `kubectl describe pod ...`, or `kubectl logs ... --tail=20`. Set a longer timeout for slow API calls.

## Tool output limit

Some results, such as long command output, full routing tables, device logs, or event lists, can run to hundreds of kilobytes. One such result could fill the model's context window and end the trial for a reason unrelated to the diagnosis. The MCP gateway therefore passes at most 10,000 characters of each tool result's text to the agent.

The limit is part of what the agent observes, like the tool list and the phase rules. Every agent gets the same limit, and each release's [benchmark engine](../../specs/benchmark-contracts.md#c-agent-interface) fixes it. A larger or disabled limit would change the task and make scores incomparable, so NIKA has no setting for it.

The limit is a ceiling, not context management. To read less, ask for less:

- `prometheus_query`, `syslog_query`, `netflow_query`, and `k8s_list_events` take a `limit`.
- `packet_capture_start` takes a capture filter, `max_packets`, and `max_duration_sec`. `packet_capture_inspect` takes a display filter and a `limit`.
- `exec_shell` commands can pipe through `head` or `grep`.

Deciding what stays in the model's context across calls is the harness's job, because only the harness knows its model's context window.

When a result is over the limit, text keeps its first and last 5,000 characters and reports how many characters were elided. List results, such as `prometheus_query`, `syslog_query`, `netflow_query`, and `k8s_list_events`, keep whole items from both ends and report how many items were elided, so each kept item stays valid JSON. If neither the first nor the last item fits, the result is cut by characters instead. The warning that the output was truncated is added on top of the limit, and non-text content is not counted.

## Submit a diagnosis

After diagnosis, NIKA freezes the report and advances the gateway to submission. The `submit` tool of `task_mcp_server` then becomes available, and the gateway offers no diagnosis tools. Read the `resource_id` and `fault_type` values from the frozen submission context, then call `submit` with `is_anomaly` and `root_causes: [{resource_id, fault_type}, ...]`. The server accepts only catalog IDs. See [Root-cause ground truth and scoring](../benchmarks/root-cause-evaluation.md).
