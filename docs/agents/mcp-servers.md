# MCP servers

Use this reference to find the tools available during an agent diagnosis session. The MCP gateway exposes session-scoped HTTP endpoints. Agents load their URLs with `DiagnosisAgent.diagnosis_mcp_config()`; see [Python MCP client](agent-reference.md#python-mcp-client) for a client example.

The [server registry](../../src/nika/mcp/registry.py) selects diagnosis servers from the scenario name, tags, backend, and router platform. Every diagnosis session receives `kathara_base_mcp_server` and `packet_capture_mcp_server`. The base server works with Kathara and Containerlab despite its name. The gateway exposes `task_mcp_server` only after the agent advances to submission.

## Run commands on environment nodes

Use `exec_shell(host_name, command, timeout=10)` for commands on hosts, servers and controllers. Set `timeout` in seconds when a command such as `kubectl` needs longer. Command output is subject to the [tool output limit](#tool-output-limit), so request bounded output with options such as `--no-headers`, `--tail`, or `-o jsonpath`. Commands run inside the named node and remain subject to the session's node access policy.

| Task | Node | Command |
| --- | --- | --- |
| Check a host route | `pc1` | `ip route` |
| Probe a destination | `pc1` | `ping -c 2 195.11.14.1` |
| Inspect host interfaces | `pc1` | `ip -s addr` |
| Read a host file | `pc1` | `cat /etc/resolv.conf` |
| Inspect Kubernetes nodes | `controller` | `kubectl get nodes --no-headers` |
| Inspect Kubernetes Pods | `controller` | `kubectl get pods -A --no-headers` |

`exec_shell` provides targeted host checks; `device_read` and the vendor CLI tools provide device checks. The base server also exposes `curl_web_test` for repeated HTTP timing measurements (a normal client: it sends `Accept-Encoding`, and each line ends with `http_code` and `size_download`), `iperf_test` for a two-node throughput test, and `active_tcp_probe` for deterministic TCP traffic over a selected five-tuple.

`active_tcp_probe` takes node names for `source` and `destination` and runs the listener on `destination`. By default the source node connects to the destination node's data-plane address; set `destination_address` to another IPv4 or IPv6 address the destination node owns, such as its loopback. Both nodes need `python3`. Missing dependencies return `status: unsupported`; connection failures return `status: failed` with an error, and a probe with missing acks returns `status: failed`. Each probe result reports the `destination_address` it targeted; successful probes also return `acked` and `elapsed_ms` in `result`.

## Use dedicated diagnosis tools

| Server | When selected | Tools and purpose |
| --- | --- | --- |
| `packet_capture_mcp_server` | Every diagnosis | `packet_capture_start`, `packet_capture_stop`, and `packet_capture_inspect` collect bounded packet evidence on a node. |
| `kathara_frr_mcp_server` | Kathara scenarios tagged `rpki` | `frr_get_rpki_status` gathers RTR cache state and optional prefix validation evidence. |
| `kathara_iosxr_mcp_server` | Kathara IOS-XR scenarios | `iosxr_exec` runs commands through the XR CLI path. |
| `kathara_routeros_mcp_server` | Kathara RouterOS scenarios | `routeros_exec` reaches RouterOS through its internal management hop. |
| `containerlab_srl_mcp_server` | Containerlab routing scenarios on SR Linux | `srl_exec_cli` runs commands through SR Linux CLI. |
| `containerlab_sonic_mcp_server` | Containerlab routing scenarios on SONiC (`device_profile: sonic`) | `sonic_exec_cli` runs one SONiC CLI command: `show`, `config`, `vtysh`, `redis-cli`, or `sonic-db-cli`. It rejects shell operators such as pipes. |
| `kathara_bmv2_mcp_server` | Kathara P4 scenarios | `p4rt_exec` reads and writes P4Runtime state through the fabric controller `fabric_mgr`. |
| `kathara_sdn_mcp_server` | Kathara SDN scenarios | `sdn_onos_rest` queries ONOS REST through `fabric_mgr`. |
| `kathara_telemetry_mcp_server` | Kathara telemetry scenarios | `int_query_telemetry` filters observed INT-MD packet and hop records. |
| `kathara_netflow_mcp_server` | Sessions started with `telemetry: [netflow]` | `flow_coverage`, `flow_timeline`, `flow_current_load`, `flow_top_talkers`, `flow_conversations`, `flow_records`, and restricted `nfdump` query observed flows. |
| `kathara_prometheus_mcp_server` | Sessions with `prometheus`, `netflow`, or `pingmesh` telemetry | `prometheus_query` runs PromQL against the device metrics Prometheus stores on `telemetry_collector`. |
| `kathara_syslog_mcp_server` | Sessions started with `telemetry: [syslog]` | `syslog_query` reads device syslog stored by `telemetry_collector`. |
| `k8s_mcp_server` | Kubernetes scenarios when `nika.k8s.access` permits MCP | `k8s_list_events` queries session-scoped Kubernetes events. |

### Router and switch commands

Routers and switches expose their device CLI instead of `exec_shell`. Use `iosxr_exec`, `routeros_exec`, `srl_exec_cli` or `sonic_exec_cli` for native routers, and `p4rt_exec` for P4Runtime state. Access follows the node roles and capabilities declared by the scenario.

For Linux/FRR routers, call `device_read` with a node and a list of commands:

```json
{"host_name": "leaf_1", "commands": ["vtysh -c 'show bgp summary'", "ip route show", "nft list ruleset"]}
```

`device_read` accepts configuration and operational commands from `vtysh`, `ip`, `tc`, `nft`, `ss`, `ethtool`, `bridge`, `wg`, `conntrack`, `ping`, and `traceroute`. SONiC routers expose `vtysh` through this tool and their other device commands through `sonic_exec_cli`. OVS switches expose `ovs-ofctl` and `ovs-vsctl`. Arguments are quoted before execution. A call accepts up to eight commands, limits each command to ten seconds, and caps their combined output at 64 KiB. If output exceeds that cap, narrow the command.

Configuration commands can enter the device's configuration mode. For example:

```json
{"host_name": "leaf_1", "commands": ["vtysh -c 'configure terminal' -c 'interface lo' -c 'description observation-point' -c 'end'"]}
```

Native router tools accept their platform's CLI commands, including configuration writes. Use the platform's normal commit command when it requires one. For SR Linux, separate commands with newlines to enter candidate mode, change configuration, and run `commit now` in one call.

`device_interface_state(host_name, interface)` reads physical carrier for a declared router or switch attachment. Use it when a virtual NOS reports protocol state independently of its backing link. It accepts only interfaces from the session inventory.

`exec_shell` remains available on hosts, servers and controllers. Infrastructure nodes have no shell access. Wildcard tool permissions and explicit node exceptions do not grant shell access to routers or switches.

### SDN (`kathara_sdn_mcp_server`)

Call `sdn_onos_rest` with paths such as `/onos/v1/devices`, `/onos/v1/flows`, or `/onos/v1/statistics/ports`. Use `device_read` on a switch for read commands such as `ovs-ofctl -O OpenFlow13 dump-flows leaf_1` or `ovs-vsctl show`. Controller nodes retain `exec_shell` for log inspection. Enable [NetFlow and Prometheus telemetry](../operations/network-scenarios.md#telemetry) when the agent should query stored flow records and OpenFlow port time series.

### P4 / BMv2 (`kathara_bmv2_mcp_server`)

Use `p4rt_exec` to inspect or change switch state through the fabric controller. The tool accepts the manager's commands and options; arguments are quoted at the controller shell boundary. Run `--help` for the complete syntax.

| Command | Effect |
| --- | --- |
| `probe --switch NAME` | Check controller and switch P4Runtime availability. |
| `read [--switch NAME]` | Read pipeline, forwarding, tables, counters, and registers. |
| `read-table --switch NAME --table T` | Read a table's entries, priorities, actions, and parameters. |
| `counters [--switch NAME]` | Read ingress and egress counters. |
| `insert`, `modify`, `delete` | Write an entry in any table declared by the pipeline. |
| `apply [--switch NAME]` | Reinstall the pipeline and original controller intent. |
| `delete-lpm --switch NAME --prefix P` | Delete a forwarding prefix. |

Table writes require `--switch` and `--table`. Repeat `--match field=value` and `--param name=value` for keys and action parameters. Use `--action NAME` for a direct action, or `--member-id` or `--group-id` for an indirect action. `--priority` selects the table priority; `--default` selects its default action. Match values follow the pipeline's declared types: exact values, IPv4 prefixes, `value&&&mask` for ternary matches, and `low..high` for ranges. Omit optional, ternary, or LPM fields to match any value. Integers accept decimal or hexadecimal notation. Delete an entry using the same keys and priority used to insert it.

Device and controller writes are allowed during a trial and recorded in the tool log. Native benchmark trials redeploy the environment; Firecracker trials restore a private disk and memory state. Targeted fault recovery restores the injected artifact and can retain other edits within the current trial. P4 `apply` restores the original pipeline and entries on its selected switches. Controller nodes retain `exec_shell`. Enable [Prometheus telemetry](../operations/network-scenarios.md#telemetry) to query pipeline-counter time series.

`probe` reports controller reachability separately from switch P4Runtime availability. `bmv2_get_ports(switch)` returns port bindings, carrier state, MTUs and interface counters. On gateway fabrics, `lb_get_state(gateway)` reads the LB management endpoint for connection capacity, occupancy, pending and active connections, the pool version, and recent update events. Connection and event lists accept `limit` and `offset` for bounded pages.

### Telemetry (`kathara_telemetry_mcp_server`)

Call `int_query_telemetry` with a start time and optional flow or packet filters. It reads per-flow INT-MD traces decoded from P4.org Telemetry Reports in collector-side storage. Each hop includes its switch ID, ingress and egress port, `hop_latency` in BMv2 microseconds, and `queue_occupancy` in packets at dequeue. `packet_timestamp` is collector receipt time in Unix nanoseconds. Each egress exports a report after dequeue and before physical transmission. Transit switches also report ingress arrival before queueing. The collector correlates reports by packet identity and keeps the longest observed path. A partial path older than two seconds has passed the in-flight grace period. `sink_seen` records whether the collector received a sink report. The tool returns `records`, `offset`, and `next_offset`. Follow `next_offset` with the same filters and a fixed `end_time` to retrieve older traces. Pages fit the tool output budget; the default requests up to 20 records. An individual trace that exceeds the budget returns an explicit error. Set `view="delivery"`, `egress_switch`, `egress_port` and `downstream_switch` to count observed packets and downstream ingress arrivals at that wire. Without `downstream_switch`, delivery counts sink observations. The response includes `observed`, `delivered`, `missing` and `max_queue_occupancy`. A downstream ingress report or hop proves delivery across the selected wire even when the next queue drops the packet. Missing reports can also reflect collector loss; compare repeated observations, queue measurements and endpoint symptoms. The `p4_dc_gateway` scenario selects this server.

On `p4_dc_gateway`, select TCP or UDP flows by writing `int_watchlist` at the source gateway or leaf. Its ternary keys are `srcAddr`, `dstAddr`, `ipProtocol`, `srcPort`, and `dstPort`. Use a positive priority; higher priorities win. The healthy gateway entry watches destination `10.0.0.0/8` at priority 1. Leaves start with an empty watchlist. For example, monitor a service-to-client flow with these `p4rt_exec` arguments:

```text
insert --switch leaf_1 --table int_watchlist --match srcAddr=10.0.1.11/32 --match dstAddr=192.0.2.11/32 --match ipProtocol=6 --match srcPort=42101 --match dstPort=42102 --priority 20 --action watch_int
```

Use `modify` with `--action NoAction` to stop monitoring a matching flow while retaining the entry, or `delete` with the same keys and priority to remove it. Endpoint-facing gateway and leaf ports start INT and strip it before delivery. Service-to-client reverse flows use the same reports and query filters as client-to-service flows. INT covers flows with an existing healthy forwarding path. East-west service-to-service traffic across leaves has no forwarding path and is outside INT coverage. `int_query_telemetry` supplies the flow identity and complete hop sequence. Prometheus exports the latest observation per switch and egress port in `p4_int_queue_depth_packets`, `p4_int_hop_latency_seconds`, and `p4_int_last_report_timestamp_seconds`; labels remain bounded by the topology rather than the number of selected flows.

INT selection covers unfragmented IPv4 TCP and UDP with a 20-byte IP header and sufficient MTU headroom. The data plane reserves space before insertion and checks MTU again before each hop. VIP address translation and INT insertion share an L4-header constraint: VIP-addressed traffic does not start INT at a gateway, and service-side reports cannot identify a translated connection by VIP. Switch-generated ICMP does not start INT.

### NetFlow (`kathara_netflow_mcp_server`)

Start with `flow_coverage` to distinguish unsupported exporters from devices with no queryable flows. It reports each device's newest record and last flow metric update. Use `limit` and `offset` to page through devices. Records and counters lag traffic by one capture rotation plus one scrape interval. Flow counters scrape every 10 seconds, matching the capture rotation.

| Tool | Use |
| --- | --- |
| `flow_timeline` | Counter rates over time, per node, optionally split by `proto` or `in_if` |
| `flow_current_load` | Latest interval rate, rolling average, and ratio |
| `flow_top_talkers` | Top groups by bytes, packets, flows, pps, bps, or bytes per packet |
| `flow_conversations` | Bidirectional flows from one exporter with forward and reverse packet and byte counters |
| `flow_records` | Individual records with a time window and nfdump filter |
| `nfdump` | A query argument list, such as `["-A", "router,proto", "-O", "bytes", "-n", "10"]` |

`flow_records`, `flow_top_talkers`, and `flow_conversations` accept `start_time="-600"` for the last ten minutes. `exporter` selects a device. `filter` accepts nfdump expressions, such as `proto tcp and dst port 80`. Record rows include `in_if_name`, `link_peers`, raw `tos`, decoded `dscp`, and `tcp_flags`. NIKA records interface maps from the healthy network before injection; old flows keep their original interface names after an interface changes. `tos` includes ECN bits; `dscp` is `tos >> 2`.

`flow_top_talkers` accepts `aggregate_by` keys `router`, `inif`, `outif`, `proto`, `srcip`, `dstip`, `srcport`, `dstport`, `tos`, `nexthop`, `srcas`, and `dstas`, plus subnet keys such as `srcip4/24`. Groups keep the exporter identity. Each device exports observations of the traffic it receives, so summing across devices counts the same traffic once per hop.

Each tool returns its executed command or PromQL query. The `nfdump` tool always reads the flow directory and disables external configuration and geolocation files. It accepts query options only and rejects file options. Queries have a 25-second runtime limit, a 100,000-character output cap, and a 1,000-row ceiling. Direct shell and device CLI access to infrastructure nodes, including `telemetry_collector`, is denied by default. The telemetry tools retain their service access.

### Prometheus (`kathara_prometheus_mcp_server`)

Call `prometheus_query` with a PromQL expression. Each series carries the device as `node`; `device` is an interface name on Linux, OVS, SR Linux, SONiC, IOS XR, and RouterOS series, and `neighbor` is a BGP peer. Omit `start_time` for an instant query. With `start_time` (for example `"-600"`), the tool returns a range at `step` resolution. `limit` sets how many series are returned. Kathara exporters are scraped every 5 seconds; SR Linux and SONiC use a 15-second scrape. `up` shows whether a device answers its scrape. The server is selected for `prometheus`, `netflow`, or `pingmesh` [telemetry](../operations/network-scenarios.md#telemetry).

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

Linux nodes export the network stack: `node_network_*`, `node_netstat_*`, `node_sockstat_*`, `node_nf_conntrack_*`, and `node_arp_*`. FRR also exports `frr_bgp_peer_up` and `frr_bgp_peer_prefixes`. OVS exports `openflow_port_*`. P4 fabrics export `p4_ingress_*` and `p4_egress_*`, and on the gateway pipeline SYN and non-SYN flow totals. SR Linux exports `srl_interface_*` and `srl_bgp_peer_up`. SONiC exports `sonic_interface_*` port counters from COUNTERS_DB, which `show interfaces counters` also reads, plus `frr_bgp_peer_up` and `frr_bgp_peer_prefixes`. XRd exports `xr_interface_*` and `xr_bgp_peer_*`. RouterOS exports `routeros_interface_*` and `routeros_bgp_peer_up`.

### Continuous endpoint probes

Use `prometheus_query` to read `probe_success` and `probe_duration_seconds`. The mesh runs throughout the task, including before injection. Labels identify `source`, `target`, `module`, attachment groups, and `expected` (`reachable` or `isolated`). The expected value comes from the environment's declared isolation policy.

```text
avg_over_time(probe_success{module="tcp_connect",expected="reachable"}[2m])
max_over_time(probe_duration_seconds{module="tcp_connect"}[2m])
```

Each endpoint opens a new TCP connection per probe. ICMP probes provide a separate reachability signal; declared web and DNS services receive HTTP and DNS probes. DNS probes check responsiveness and accept `REFUSED` from authoritative servers for names outside their zones. Check application records separately to verify name resolution. Connection success and duration include retries. They do not directly measure per-packet loss. Compare latency with the network's design RTT and with its healthy history.

The mesh groups endpoints by topology attachment and rotates through all directed pairs. `pingmesh_candidate_targets`, `pingmesh_active_targets`, and `pingmesh_coverage_window_seconds` report coverage. `pingmesh_endpoint_supported{node}` reports endpoint support. The host shares a budget of 128 active targets among collectors, with one scrape per target every 15 seconds. Representatives rotate every 30 seconds. A pair outside the current target list has historical samples but no current sample.

### Syslog (`kathara_syslog_mcp_server`)

Call `syslog_query` with a time window, for example `start_time="-600"` for the last ten minutes. Pass `host` for one device, `program` for a syslog tag such as `bgpd`, and `contains` for a message substring. Rows are `{time, host, program, message}`. Linux and OVS devices forward `/dev/log` over the management VRF after FRR `log syslog`. SR Linux sends syslog over UDP 514 from the mgmt network-instance on the lab management network. SONiC's rsyslogd forwards over the lab management network. XRd uses that Linux forwarder because extra Kathara NICs stay on Linux. RouterOS uses a remote logging action. The server is selected when a session enables `syslog` [telemetry](../operations/network-scenarios.md#telemetry).

### Kubernetes (`k8s_mcp_server`)

Call `k8s_list_events(namespace=..., limit=...)` for event evidence. It returns one JSON object per event. Use `exec_shell` on `controller` for other Kubernetes operations, such as `kubectl get nodes --no-headers`, `kubectl describe pod ...`, or `kubectl logs ... --tail=20`. Set a longer timeout for slow API calls. The server is selected when `nika.k8s.access` is `auto` or `mcp`; `kubectl_only` omits it.

## Capture packets

Capture needs `dumpcap` or `tcpdump` on the node. Inspection uses the node's `tshark` when available; otherwise NIKA runs it in a temporary container using its base image. No host `tshark` installation is needed. Capture files and metadata stay on the node.

1. Call `packet_capture_start(device, interface, capture_filter=..., max_duration_sec=..., max_packets=...)`. It returns a `capture_id`.
2. Run traffic with `exec_shell`, `active_tcp_probe`, or the scenario's traffic workflow.
3. Call `packet_capture_stop(capture_id)`.
4. Call `packet_capture_inspect(capture_id, view="summary", limit=...)` or use the `packets`, `protocol`, or `expert` view. Pass a Wireshark display filter when needed.

Set bounded duration and packet limits. The capture and metadata remain on the node; the stop result includes the container path.

A valid capture with zero packets still returns an artifact. If no nonempty capture file exists, stopping fails and marks the capture as failed; start a new capture rather than inspecting the failed one.

`interface` must exist on the node; otherwise `packet_capture_start` fails and lists the node's interfaces. On SR Linux nodes, `ethernet-1/N`, `ethernet-1/N.0`, `e1-N`, and the plan name `eth{N-1}` all capture on data port `e1-N`, which carries both transit and control-plane traffic. On SONiC nodes, port `EthernetN` captures on the container data interface `eth{N/4+1}` that carries it, the same interface the topology names.

## Tool output limit

Some tool results, such as long command output, full routing tables, device logs, or event lists, can run to hundreds of kilobytes. One such result could fill the model's context window and end the trial for a reason unrelated to the diagnosis. The MCP gateway therefore passes at most 10,000 characters of each tool result's text to the agent.

The limit is part of what the agent observes, like the tool list and the phase rules, so every agent gets the same one and each release's [benchmark engine](../development/benchmark-contracts.md#c-agent-interface) fixes it. A larger or disabled limit would change the task and make scores incomparable, so NIKA has no setting for it.

The limit is a ceiling, not context management. To read less, an agent asks for less: `prometheus_query`, `syslog_query`, `flow_records`, and `k8s_list_events` take a `limit`; `packet_capture_start` takes a capture filter, `max_packets`, and `max_duration_sec`, and `packet_capture_inspect` takes a display filter and a `limit`; and `exec_shell` commands can pipe through `head` or `grep`. Deciding what stays in the model's context across calls is the harness's job, because only the harness knows its model's context window.

The truncation warning and elision markers are added on top of that limit, and non-text content is not counted. Text over the limit keeps its first and last 5,000 characters and reports how many were elided. List results, such as `prometheus_query`, `syslog_query`, and `k8s_list_events`, keep whole items from both ends and report how many items were elided. Each kept item therefore stays valid JSON. If neither the first nor the last item fits, the result is truncated by characters instead. Both cases start with the same warning that the output was truncated.

Flow responses with a `rows` field retain their executed command or query and whole rows when bounded. `omitted_rows` reports how many rows the output limit removed.

## Submit a diagnosis

`task_mcp_server` exposes `submit` after `begin_submission_mcp_phase(session_id, diagnosis_report)`. Read `resource_id` and `fault_type` values from the frozen submission context, then call `submit` with `is_anomaly` and `root_causes: [{resource_id, fault_type}, ...]`. The server accepts only catalog IDs. See [Root-cause ground truth and scoring](../benchmarks/root-cause-evaluation.md).
