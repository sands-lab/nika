// Types mirror the explorer HTTP API contract (../API.md).

export interface TaskSummary {
  task_id: string;
  network: string;
  topology_size: string | null;
  n_devices: number;
  n_segments: number;
  telemetry: string[];
  recording_start: string;
  alert_time: string;
  recording_end: string;
  has_label: boolean;
}

export interface Device {
  name: string;
  role: string;
  capabilities: string[];
  views: string[];
  before_fault_views: string[];
  has_metrics: boolean;
}

export interface SegmentEnd {
  device: string;
  port: string;
}

export interface Segment {
  id: string;
  kind: "link" | "lan" | string;
  ends: SegmentEnd[];
}

export interface Timeline {
  recording_start: string;
  alert_time: string;
  state_snapshot_time: string | null;
  recording_end: string;
  recording_start_ms: number;
  alert_time_ms: number;
  state_snapshot_time_ms: number | null;
  recording_end_ms: number;
}

export interface Collectors {
  prometheus: boolean;
  syslog: boolean;
  netflow: boolean;
  int: boolean;
  pingmesh: boolean;
  http_dns: boolean;
  iperf: boolean;
  traceroute: boolean;
}

export interface TaskDetail {
  task_id: string;
  network: string;
  scenario: string;
  topology_size: string | null;
  description: string;
  probe_plan: Record<string, unknown>;
  telemetry: string[];
  devices: Device[];
  segments: Segment[];
  timeline: Timeline;
  collectors: Collectors;
  controllers: string[];
  /** Whether a pre-rendered diagram is served at /api/tasks/{tid}/topology.png. */
  has_topology_png?: boolean;
  netflow_exporters?: string[];
}

export interface RootCause {
  fault_type: string;
  component: string;
}

export interface Label {
  is_anomaly: boolean;
  fault_category: string | null;
  root_causes: RootCause[];
  fault_injected_at: string | null;
  fault_injected_at_ms: number | null;
}

export interface ViewText {
  device: string;
  view: string;
  phase: string;
  text: string;
}

export interface MetricInfo {
  metric: string;
  kind: "counter" | "gauge";
  devices: string[];
  n_series: number;
}

export interface MetricSeries {
  id: string;
  device: string;
  labels: Record<string, string>;
  values: (number | null)[];
}

export interface MetricData {
  metric: string;
  kind: string;
  rate: boolean;
  t: number[];
  series: MetricSeries[];
}

export type CounterName =
  | "rx_bytes"
  | "tx_bytes"
  | "rx_packets"
  | "tx_packets"
  | "rx_errs"
  | "tx_errs"
  | "rx_drop"
  | "tx_drop";

export interface InterfaceData {
  device: string;
  port: string;
  source: string | null;
  column: string | null;
  t: number[];
  counters: Partial<Record<CounterName, (number | null)[]>>;
}

export interface SyslogRow {
  t: number;
  device: string;
  program: string;
  message: string;
}

export interface SyslogData {
  total: number;
  matched: number;
  rows: SyslogRow[];
  devices: string[];
  programs: string[];
}

export interface NetflowRow {
  first_ms: number;
  last_ms: number;
  exporter: string;
  in_if: string;
  proto: string;
  src_ip: string;
  src_port: number;
  dst_ip: string;
  dst_port: number;
  packets: number;
  bytes: number;
  flows: number;
}

export interface NetflowData {
  total: number;
  matched: number;
  rows: NetflowRow[];
  top_talkers: { src_ip: string; dst_ip: string; proto: string; dst_port: number; bytes: number; packets: number; flows: number }[];
  by_exporter: { exporter: string; bytes: number; packets: number; flows: number }[];
  timeseries: { t: number[]; bucket_s: number; series: { exporter: string; values: (number | null)[] }[] };
}

export interface PingSummary {
  reachable_pairs: number;
  total_pairs: number;
  anomaly_count: number;
  unreachable: number;
  packet_loss: number;
  high_latency: number;
  policy_violation: number;
  rtt_median: number | null;
}

export interface PingmeshData {
  t: number[];
  summary: PingSummary[];
  pairs: { source: string; target: string; status: (string | null)[]; loss: (number | null)[]; rtt: (number | null)[]; expected?: (string | null)[] }[];
  endpoints: Record<string, string>;
}

export interface HttpDnsRow {
  t: number;
  kind: "http" | "dns";
  source: string;
  target: string;
  ok: boolean;
  code: string | null;
  latency_ms: number | null;
  detail: Record<string, unknown>;
}

export interface IperfRow {
  t: number;
  source: string;
  target: string;
  sender_mbps: number | null;
  receiver_mbps: number | null;
  retransmits: number | null;
  ok: boolean;
}

export interface TracerouteEntry {
  source: string;
  target: string;
  output: string;
}

export interface IntSwitch {
  switch_id: number | string;
  reports: (number | null)[];
  hop_latency_avg_us: (number | null)[];
  hop_latency_max_us: (number | null)[];
  queue_max: (number | null)[];
}

export interface IntSummary {
  available: boolean;
  n_reports: number;
  t: number[];
  switches: IntSwitch[];
  top_flows: { src: string; dst: string; protocol: number | string; dst_port: number; reports: number; hops: string }[];
}

export interface IntRecords {
  total: number;
  rows: Record<string, unknown>[];
}

export interface ControllerFile {
  name: string;
  text: string;
}
