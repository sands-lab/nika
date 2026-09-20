export type EventSource = "agent" | "nika";
export type EventKind =
  | "lifecycle"
  | "llm"
  | "tool_call"
  | "tool_result"
  | "tool_error"
  | "phase"
  | "score"
  | "submission"
  | "system"
  | "other";

export interface ToolPayload {
  name?: string | null;
  input?: unknown;
  output?: unknown;
  tool_call_id?: string | null;
  error?: string | null;
}

export interface CanonicalTraceEvent {
  id: string;
  timestamp?: string | null;
  source: EventSource;
  kind: EventKind;
  title: string;
  summary: string;
  phase?: string | null;
  event?: string | null;
  tool?: ToolPayload | null;
  duration_ms?: number | null;
  raw: Record<string, unknown>;
}

export interface ArtifactFlags {
  run: boolean;
  messages: boolean;
  nika: boolean;
  ground_truth: boolean;
  submission: boolean;
  eval_metrics: boolean;
  llm_judge: boolean;
}

export interface SessionSummary {
  session_id: string;
  session_key?: string | null;
  session_dir: string;
  status: "running" | "finished";
  lab_name?: string | null;
  scenario_name?: string | null;
  scenario_topo_size?: string | null;
  agent_type?: string | null;
  model?: string | null;
  llm_provider?: string | null;
  problem_names: string[];
  failure_domain?: string | null;
  inject_params?: Record<string, string>;
  start_time?: string | null;
  end_time?: string | null;
  outcome?: string | null;
  detection_score?: number | null;
  rca_f1?: number | null;
  localization_f1?: number | null;
  in_tokens?: number | null;
  out_tokens?: number | null;
  steps?: number | null;
  tool_calls?: number | null;
  artifacts: ArtifactFlags;
  is_benchmark?: boolean;
  case_key?: string | null;
  trial_id?: string | null;
  trial_index?: number | null;
  benchmark_id?: string | null;
  benchmark_version?: string | null;
  benchmark_split?: string | null;
  benchmark_run_id?: string | null;
  benchmark_official?: boolean | null;
  benchmark_n_trials?: number | null;
  scoring_id?: string | null;
  benchmark_label?: string | null;
}

export interface BenchmarkRunSummary {
  run_key: string;
  label: string;
  benchmark_id?: string | null;
  benchmark_version?: string | null;
  benchmark_split?: string | null;
  benchmark_run_id?: string | null;
  benchmark_official?: boolean | null;
  agent_type?: string | null;
  model?: string | null;
  scoring_id?: string | null;
  expected_trials?: number | null;
  session_count: number;
  finished_count: number;
  mean_rca_f1?: number | null;
}

export interface SessionDetail extends SessionSummary {
  run: Record<string, unknown>;
  task_description?: string | null;
}

export interface SessionFacets {
  statuses: string[];
  scenarios: string[];
  agents: string[];
  models: string[];
  problems: string[];
  failure_domains: string[];
  topo_sizes: string[];
  trial_indices?: number[];
}

export interface SessionListResponse {
  sessions: SessionSummary[];
  benchmarks: BenchmarkRunSummary[];
  facets?: SessionFacets;
  results_root: string;
  selected_root?: string;
  total: number;
}

export interface ResultsRootOption {
  id: string;
  label: string;
  path: string;
}

export interface ResultsRootsResponse {
  base_root: string;
  selected_root: string;
  results_root: string;
  roots: ResultsRootOption[];
}

export interface TimelineResponse {
  session_id: string;
  events: CanonicalTraceEvent[];
  total: number;
}

export interface ScoresResponse {
  session_id: string;
  eval_metrics?: Record<string, unknown> | null;
  ground_truth?: Record<string, unknown> | null;
  submission?: Record<string, unknown> | null;
  llm_judge?: Record<string, unknown> | null;
}

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

function withRoot(params: URLSearchParams, root?: string | null) {
  const out = new URLSearchParams(params);
  if (root && root !== ".") out.set("root", root);
  else out.delete("root");
  return out;
}

function rootQuery(root?: string | null): string {
  if (!root || root === ".") return "";
  return `?root=${encodeURIComponent(root)}`;
}

export function fetchRoots(root?: string | null) {
  const q = rootQuery(root);
  return getJson<ResultsRootsResponse>(`/api/roots${q}`);
}

export function fetchSessions(params: URLSearchParams, root?: string | null) {
  return getJson<SessionListResponse>(`/api/sessions?${withRoot(params, root)}`);
}

export function fetchSession(id: string, root?: string | null) {
  return getJson<SessionDetail>(
    `/api/sessions/${encodeURIComponent(id)}${rootQuery(root)}`,
  );
}

export function fetchTimeline(id: string, source?: string, root?: string | null) {
  const params = new URLSearchParams();
  if (source) params.set("source", source);
  const q = withRoot(params, root).toString();
  return getJson<TimelineResponse>(
    `/api/sessions/${encodeURIComponent(id)}/timeline${q ? `?${q}` : ""}`,
  );
}

export function fetchScores(id: string, root?: string | null) {
  return getJson<ScoresResponse>(
    `/api/sessions/${encodeURIComponent(id)}/scores${rootQuery(root)}`,
  );
}

export function fetchRaw(id: string, filename: string, root?: string | null) {
  return getJson<{ filename: string; data: unknown }>(
    `/api/sessions/${encodeURIComponent(id)}/raw/${encodeURIComponent(filename)}${rootQuery(root)}`,
  );
}

export function formatTs(value?: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value.slice(11, 19) || value;
  return d.toLocaleTimeString(undefined, {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function formatScore(value?: number | null): string {
  if (value === null || value === undefined) return "—";
  return Number.isInteger(value) ? String(value) : value.toFixed(3);
}
