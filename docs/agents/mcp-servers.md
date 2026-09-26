# MCP servers

Use this reference to find the tools available during an agent diagnosis session. The MCP gateway exposes session-scoped HTTP endpoints. Load their URLs with `agent.utils.mcp_client.load_session_mcp_config`; see [Custom agent integration](custom-agents.md) for a client example.

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

`exec_shell` is the main tool for targeted checks. The base server also exposes `curl_web_test` for repeated HTTP timing measurements, `iperf_test` for a two-node throughput test, and `active_tcp_probe` for deterministic TCP traffic over a selected five-tuple.

## Use dedicated diagnosis tools

| Server | When selected | Tools and purpose |
| --- | --- | --- |
| `pingmesh_mcp_server` | Every diagnosis | `run_pingmesh_snapshot` probes endpoint pairs and reports reachability, loss, and RTT. |
| `packet_capture_mcp_server` | Every diagnosis | `packet_capture_start`, `packet_capture_stop`, and `packet_capture_inspect` collect bounded packet evidence on a node. |
| `kathara_frr_mcp_server` | Kathara scenarios tagged `rpki` | `frr_get_rpki_status` gathers RTR cache state and optional prefix validation evidence. |
| `kathara_iosxr_mcp_server` | Kathara IOS-XR scenarios | `iosxr_exec` runs commands through the XR CLI path. |
| `kathara_routeros_mcp_server` | Kathara RouterOS scenarios | `routeros_exec` reaches RouterOS through its internal management hop. |
| `containerlab_srl_mcp_server` | Containerlab routing scenarios | `srl_exec_cli` runs commands through SR Linux CLI. |
| `kathara_bmv2_mcp_server` | Kathara P4 scenarios | `p4rt_exec` queries P4Runtime through `fabric_mgr` and removes private fault fields from JSON output. |
| `kathara_sdn_mcp_server` | Kathara SDN scenarios | `sdn_onos_rest` queries ONOS REST through `fabric_mgr`. |
| `kathara_telemetry_mcp_server` | Kathara telemetry scenarios | `int_query_telemetry` filters observed INT-MX packet and hop records. |
| `k8s_mcp_server` | Kubernetes scenarios when `nika.k8s.access` permits MCP | `k8s_list_events` queries session-scoped Kubernetes events. |

### SDN (`kathara_sdn_mcp_server`)

Call `sdn_onos_rest` with paths such as `/onos/v1/devices` or `/onos/v1/flows`. Use `exec_shell` on a switch for `ovs-ofctl` or `ovs-vsctl`, and on `onos` to inspect controller logs.

### P4 / BMv2 (`kathara_bmv2_mcp_server`)

Call `p4rt_exec` with `read` or `read --switch leaf_1`. Its response removes private post-counter fault fields. Use `exec_shell` for ordinary switch commands.

### Telemetry (`kathara_telemetry_mcp_server`)

Call `int_query_telemetry` with a start time and optional flow or packet filters. It reads observed INT-MX traces from the collector. The `p4_dc_gateway` scenario selects this server.

### Kubernetes (`k8s_mcp_server`)

Call `k8s_list_events(namespace=..., limit=...)` for event evidence. Use `exec_shell` on `controller` for other Kubernetes operations, such as `kubectl get nodes --no-headers`, `kubectl describe pod ...`, or `kubectl logs ... --tail=20`. Keep output below the command result limit and set a longer timeout for slow API calls. The server is selected when `nika.k8s.access` is `auto` or `mcp`; `kubectl_only` omits it.

## Capture packets

The lab node needs `tshark` for inspection. NIKA images include it. Capture, metadata, and inspection stay inside the target container.

1. Call `packet_capture_start(device, interface, capture_filter=..., max_duration_sec=..., max_packets=...)`. It returns a `capture_id`.
2. Run traffic with `exec_shell`, `active_tcp_probe`, or the scenario's traffic workflow.
3. Call `packet_capture_stop(capture_id)`.
4. Call `packet_capture_inspect(capture_id, view="summary", limit=...)` or use the `packets`, `protocol`, or `expert` view. Pass a Wireshark display filter when needed.

Set bounded duration and packet limits. The capture and metadata remain on the node; the stop result includes the container path.

## Submit a diagnosis

`submit` accepts only IDs present in those catalogs. After `agent.submit_reject_limit` consecutive rejected calls, `submit` refuses every later call for the session, including a valid one. Scoring details: [Root-cause ground truth and scoring](../benchmarks/root-cause-evaluation.md).

## Gateway access checks and output limits

The gateway checks each diagnosis tool call against the `agent.access` role policy before the server runs it:

- Node arguments name the lab nodes a call targets, for example `host_name`, `router_name`, `device_name`, or the `sources` and `targets` lists of `run_pingmesh_snapshot`.
- Some tools have a fixed target: `onos` for `sdn_onos_rest` and `sdn_controller_logs`, `fabric_mgr` for `p4rt_exec`, `collector` for `int_query_telemetry`, and `controller` for every `k8s_*` tool.
- When the role limits `node_roles`, the gateway denies tools that have no target mapping and requires explicit `sources` and `targets` for `run_pingmesh_snapshot`.

The gateway binds each MCP connection to the `NIKA-Session-Id` sent with `initialize`. A later request on that connection with a different `NIKA-Session-Id` gets HTTP `403`.

`nika.mcp.tool_output_max_chars` caps the text of one tool result across all its content blocks. A tool that returns a list gets one block per item, so an oversized list keeps the leading items, cuts the item that crosses the cap, and drops the rest. A truncated result carries no `structuredContent`. `tools/list` omits `outputSchema`, so MCP clients do not require `structuredContent`.

## Packet capture workflow

Every diagnosis session includes `packet_capture_mcp_server`. Use it when you need bounded packet evidence:

1. `packet_capture_start(device, interface, capture_filter=..., max_duration_sec=..., max_packets=...)`: start async capture. Pass a BPF (libpcap) filter plus duration or packet caps.
2. Run probes (`ping_pair`, `active_tcp_probe`, or scenario traffic) while capture runs.
3. `packet_capture_stop(capture_id)`: stop capture. The pcap and metadata stay on the lab node; the stop payload returns the container path.
4. `packet_capture_inspect(capture_id, view=..., display_filter=..., limit=..., offset=...)`: page through `summary`, `packets`, `protocol`, or `expert` with a Wireshark display filter. Inspection runs `tshark` inside the capture node.

Set capture limits on each call; values above the hard ceilings fail. Use BPF at start and Wireshark display filters at inspect. Default inspect pages return protocol fields without application payload.

## Tool output length

`nika.mcp.tool_output_max_chars` (default `10000`) caps the combined text of one tool result. When the text is longer, the gateway keeps the first and last halves, reports how many characters were elided, and warns the agent to retry with a narrower command (`head`/`tail`/`sed`/`grep`) or redirect output to a file. Truncated results drop `structuredContent`. `tools/list` omits `outputSchema`, so MCP clients do not require it.

## Related docs

| Topic | Doc |
| --- | --- |
| Agent helpers and registration | [Custom agent integration](custom-agents.md) |
| `nika.mcp.*`, `nika.k8s.access` | [Run configuration](../operations/configuration.md) |
| Sandbox MCP endpoints | [Docker Sandbox execution](../operations/agent-sandbox.md) |
| Remote lab MCP gateway | [Remote lab execution](../operations/remote.md) |
| Scenario topology and deploy | [Network scenarios](../operations/network-scenarios.md) |
| Submit schema and scoring | [Root-cause ground truth and scoring](../benchmarks/root-cause-evaluation.md) |
