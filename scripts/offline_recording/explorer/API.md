# Explorer HTTP API

The backend serves JSON under `/api`. All timestamps are returned both as ISO strings (where they appear in the dataset) and as epoch milliseconds (`*_ms`, `t`). Time-series use a shared `t` array of epoch milliseconds and one `values` array per series, aligned with `t`; missing samples are `null`.

| Endpoint | Response |
| --- | --- |
| `GET /api/tasks` | `[{task_id, network, topology_size, n_devices, n_segments, telemetry: [str], recording_start, alert_time, recording_end, has_label}]` |
| `GET /api/tasks/{tid}` | Task detail, see below. |
| `GET /api/tasks/{tid}/label` | `{is_anomaly, fault_category, root_causes: [{fault_type, component}], fault_injected_at, fault_injected_at_ms}`; 404 when there is no label. |
| `GET /api/tasks/{tid}/devices/{dev}/views/{view}?phase=snapshot\|before_fault` | `{device, view, phase, text}`; 404 when the file is missing. |
| `GET /api/tasks/{tid}/controllers/{name}` | `{name, text}` (JSON files are pretty-printed). |
| `GET /api/tasks/{tid}/topology.png` | Pre-rendered layered topology diagram (PNG) when the task has one (`has_topology_png`); 404 otherwise. |
| `GET /api/tasks/{tid}/metrics` | `[{metric, kind: "counter"\|"gauge", devices: [str], n_series}]`, sorted by metric name. |
| `GET /api/tasks/{tid}/metrics/{metric}?device=a&device=b&rate=auto\|true\|false` | `{metric, kind, rate: bool, t: [ms], series: [{id, device, labels: {k: v}, values: [num\|null]}]}`. `rate=auto` (default) turns counters into per-second rates; counter resets produce `null`. `device` is the NIKA device; node_exporter's own `device` label (the interface, such as `eth0`) stays in `labels`. |
| `GET /api/tasks/{tid}/interface?device=X&port=eth1` | `{device, port, source: "node_network"\|"sonic"\|null, column, t: [ms], counters: {rx_bytes, tx_bytes, rx_packets, tx_packets, rx_errs, tx_errs, rx_drop, tx_drop}}`; each counter is a per-second rate array (or absent when the metric does not exist). |
| `GET /api/tasks/{tid}/syslog?device=&program=&q=&start=&end=&limit=5000` | `{total, matched, rows: [{t, device, program, message}], devices: [str], programs: [str]}`. `device` and `program` may repeat; `start` and `end` are epoch ms. |
| `GET /api/tasks/{tid}/netflow?exporter=&q=&start=&end=&limit=2000` | `{total, matched, rows: [{first_ms, last_ms, exporter, in_if, proto, src_ip, src_port, dst_ip, dst_port, packets, bytes, flows}], top_talkers: [{src_ip, dst_ip, proto, dst_port, bytes, packets, flows}], by_exporter: [{exporter, bytes, packets, flows}], timeseries: {t: [ms], bucket_s, series: [{exporter, values: [bytes per second]}]}}`. `start`/`end` (epoch ms) keep flow records whose active interval overlaps the window. |
| `GET /api/tasks/{tid}/probes/pingmesh` | `{t: [ms], summary: [{reachable_pairs, total_pairs, anomaly_count, unreachable, packet_loss, high_latency, policy_violation, rtt_median}], pairs: [{source, target, status: [str\|null], loss: [num\|null], rtt: [num\|null], expected: [str\|null]}], endpoints: {device: ip}}`. `expected` is `"isolated"` for pairs that policy keeps apart, so a `down` status there is intended. |
| `GET /api/tasks/{tid}/probes/http_dns` | `{rows: [{t, kind: "http"\|"dns", source, target, ok, code, latency_ms, detail}]}`; `target` is the HTTP target device or the DNS name, `detail` holds the remaining fields. |
| `GET /api/tasks/{tid}/probes/iperf` | `{rows: [{t, source, target, sender_mbps, receiver_mbps, retransmits, ok}]}` |
| `GET /api/tasks/{tid}/probes/traceroute` | `[{source, target, output}]` |
| `GET /api/tasks/{tid}/int/summary` | `{available, n_reports, t: [ms], switches: [{switch_id, reports: [], hop_latency_avg_us: [], hop_latency_max_us: [], queue_max: []}], top_flows: [{src, dst, protocol, dst_port, reports, hops}]}` per one-second bucket of the reports' own `packet_timestamp`, which may fall outside the recording window. |
| `GET /api/tasks/{tid}/int/records?offset=0&limit=50` | `{total, rows: [raw INT report]}` |

## Task detail

```json
{
  "task_id": "...", "network": "campus_lan", "scenario": "...", "topology_size": "s",
  "description": "Network Description: ...",
  "probe_plan": {}, "telemetry": ["netflow", "prometheus", "syslog"],
  "devices": [{"name": "r1", "role": "router", "capabilities": ["frr"], "views": ["interfaces", "routes"], "before_fault_views": ["interfaces"], "has_metrics": true}],
  "segments": [{"id": "seg0", "kind": "link" | "lan", "ends": [{"device": "r1", "port": "eth0"}]}],
  "timeline": {"recording_start": "...", "alert_time": "...", "state_snapshot_time": "...", "recording_end": "...",
               "recording_start_ms": 0, "alert_time_ms": 0, "state_snapshot_time_ms": 0, "recording_end_ms": 0},
  "collectors": {"prometheus": true, "syslog": true, "netflow": true, "int": false, "pingmesh": true, "http_dns": true, "iperf": true, "traceroute": true},
  "controllers": ["p4runtime_read.json"],
  "has_topology_png": true,
  "netflow_exporters": ["r1"]
}
```
