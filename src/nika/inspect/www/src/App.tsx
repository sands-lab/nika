import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AnnotationComment,
  Annotations,
  CanonicalTraceEvent,
  BrowseEntry,
  BenchmarkProgressDoc,
  deleteSession,
  formatScore,
  formatTs,
  fetchAnnotations,
  fetchBenchmarkProgress,
  fetchBrowse,
  fetchRaw,
  fetchRoots,
  fetchScores,
  fetchSession,
  fetchSessionSearch,
  fetchSessions,
  fetchTimeline,
  saveAnnotations,
  ScoresResponse,
  SessionDetail,
  SessionFacets,
  SessionSummary,
} from "./api";
import {
  OverviewLane,
  DisplayEvent,
  buildOverviewSpans,
  collapsePairedEvents,
  annotateLlmRetryAttempts,
  closeSupersededOpenSpans,
  displayDurationMs,
  formatDuration,
  formatOverviewClock,
  formatTokenCount,
  buildLlmTurnDetail,
  extractModelName,
  isRunningSpan,
  llmDurationStats,
  llmTokenUsage,
  OverviewLayoutMode,
  overviewLaneHeightPx,
  overviewStackBarGeometry,
  overviewTurnBoundaryPcts,
  assignLaneStackRows,
  type LaidOutOverviewSpan,
  parseLlmMessageContent,
  parseTs,
  projectOverviewDomain,
  roleLabel,
  Role,
  isToolDisplay,
} from "./roles";
import {
  buildUrlSearch,
  matchesNumericFilter,
  parseNumericFilter,
  parseUrlState,
  type SessionTab,
  type UrlState,
} from "./viewState";

type Tab = SessionTab;

/** Per-session location mirrored into the URL (see ``viewState.ts``). */
type SessionNav = { tab: Tab | null; event: string | null; find: string | null };

const EMPTY_NAV: SessionNav = { tab: null, event: null, find: null };

function navFromUrl(url: UrlState): SessionNav {
  return { tab: url.tab, event: url.event, find: url.find };
}

const RAW_FILES = [
  "run.json",
  "messages.jsonl",
  "nika.jsonl",
  "ground_truth.json",
  "submission.json",
  "eval_metrics.json",
  "llm_judge.json",
  "annotations.json",
];

/** Single-key shortcuts must not fire while the user types in a field. */
function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return (
    target.isContentEditable ||
    target.tagName === "INPUT" ||
    target.tagName === "TEXTAREA" ||
    target.tagName === "SELECT"
  );
}

/** Timeline event ids folded into one ledger row (search hits map onto rows). */
function rowEventIds(row: DisplayEvent): string[] {
  const ids = [row.id, row.start.id];
  if (row.end) ids.push(row.end.id);
  for (const ev of row.interiors ?? []) ids.push(ev.id);
  return ids;
}

const LANES: { id: OverviewLane; label: string }[] = [
  { id: "nika", label: "NIKA" },
  { id: "model", label: "Agent" },
  { id: "tools", label: "Tools" },
];

/** Inclusive time window for overview brush-zoom (ms epoch). */
type TimeBrush = { startMs: number; endMs: number };

function overlapsBrush(
  startMs: number | null,
  endMs: number | null,
  brush: TimeBrush,
): boolean {
  if (startMs == null) return false;
  const end = endMs ?? startMs;
  // Inclusive: point events at brush.startMs (typical for equal-mode chip
  // selection) must match; half-open (start, end) left them out.
  return startMs <= brush.endMs && end >= brush.startMs;
}

function rowInBrush(row: DisplayEvent, brush: TimeBrush): boolean {
  const startMs = parseTs(row.timestamp);
  if (startMs == null) return false;
  const endFromTs = parseTs(row.endTimestamp);
  const endMs =
    endFromTs != null
      ? endFromTs
      : row.durationMs != null
        ? startMs + row.durationMs
        : startMs;
  return overlapsBrush(startMs, endMs, brush);
}


function sessionOpenId(s: Pick<SessionSummary, "session_key" | "session_id">): string {
  return s.session_key || s.session_id;
}

/** Human title: failure / problem names, not the encoded session id. */
function sessionTitle(s: Pick<SessionSummary, "problem_names" | "scenario_name" | "session_id">): string {
  if (s.problem_names?.length) return s.problem_names.join(", ");
  return "—";
}

function sessionDuration(s: SessionSummary, nowMs: number = Date.now()): string {
  const start = parseTs(s.start_time);
  if (start == null) return "—";
  if (s.status === "running") {
    return formatDuration(Math.max(0, nowMs - start));
  }
  const end = parseTs(s.end_time);
  if (end == null || end < start) return "—";
  return formatDuration(end - start);
}

/** Elapsed wall time; only running sessions extend to ``nowMs`` when end_time is missing. */
function sessionElapsedMs(s: SessionSummary, nowMs: number = Date.now()): number | null {
  const start = parseTs(s.start_time);
  if (start == null) return null;
  const end =
    s.status === "running"
      ? (parseTs(s.end_time) ?? nowMs)
      : parseTs(s.end_time);
  if (end == null || end < start) return null;
  return end - start;
}

/** Whole-second duration for the run monitor (e.g. ``12s``, ``5m 3s``). */
function formatDurationSeconds(ms: number | null): string {
  if (ms == null || ms < 0) return "—";
  const totalSec = Math.round(ms / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const mins = Math.floor(totalSec / 60);
  const secs = totalSec % 60;
  return secs === 0 ? `${mins}m` : `${mins}m ${secs}s`;
}

function labDurationStats(sessions: SessionSummary[]): {
  minMs: number | null;
  avgMs: number | null;
  maxMs: number | null;
  n: number;
} {
  const values: number[] = [];
  for (const s of sessions) {
    if (s.status !== "finished") continue;
    const ms = sessionElapsedMs(s);
    if (ms != null) values.push(ms);
  }
  if (!values.length) {
    return { minMs: null, avgMs: null, maxMs: null, n: 0 };
  }
  const sum = values.reduce((a, b) => a + b, 0);
  return {
    minMs: Math.min(...values),
    avgMs: sum / values.length,
    maxMs: Math.max(...values),
    n: values.length,
  };
}

/**
 * Active suite time: union of session [start, end] intervals.
 * Idle gaps (suite paused between trials) are excluded; parallel sessions
 * are not double-counted. Running sessions extend to ``nowMs``.
 */
function suiteElapsedMs(
  sessions: SessionSummary[],
  nowMs: number = Date.now(),
): number | null {
  const intervals: Array<[number, number]> = [];
  for (const s of sessions) {
    const start = parseTs(s.start_time);
    if (start == null) continue;
    const end = s.status === "running" ? nowMs : parseTs(s.end_time);
    if (end == null || end < start) continue;
    intervals.push([start, end]);
  }
  if (!intervals.length) return null;
  intervals.sort((a, b) => a[0] - b[0]);
  let total = 0;
  let curStart = intervals[0][0];
  let curEnd = intervals[0][1];
  for (let i = 1; i < intervals.length; i++) {
    const [s, e] = intervals[i];
    if (s <= curEnd) {
      if (e > curEnd) curEnd = e;
    } else {
      total += curEnd - curStart;
      curStart = s;
      curEnd = e;
    }
  }
  total += curEnd - curStart;
  return total;
}

function trialsPerHour(
  completed: number,
  elapsedMs: number | null,
): number | null {
  if (elapsedMs == null || elapsedMs <= 0 || completed <= 0) return null;
  return completed / (elapsedMs / 3_600_000);
}

/** Completion-token generation rate over suite active elapsed. */
function outTokensPerSec(
  sumOutTok: number | null,
  elapsedMs: number | null,
): number | null {
  if (elapsedMs == null || elapsedMs <= 0 || sumOutTok == null || sumOutTok <= 0)
    return null;
  return sumOutTok / (elapsedMs / 1000);
}

function fmtTokPerSec(value: number | null): string {
  if (value == null) return "—";
  if (value >= 100) return `${Math.round(value)}/s`;
  if (value >= 10) return `${value.toFixed(1)}/s`;
  return `${value.toFixed(2)}/s`;
}

/** Compact wall time for the sessions table (sortable via start_time). */
function sessionShortTime(s: SessionSummary): string {
  const raw = s.start_time;
  if (!raw) return "—";
  const d = new Date(raw);
  if (!Number.isNaN(d.getTime())) {
    const mm = String(d.getMonth() + 1).padStart(2, "0");
    const dd = String(d.getDate()).padStart(2, "0");
    const hh = String(d.getHours()).padStart(2, "0");
    const mi = String(d.getMinutes()).padStart(2, "0");
    return `${mm}-${dd} ${hh}:${mi}`;
  }
  const m = raw.match(/(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})/);
  if (m) return `${m[2]}-${m[3]} ${m[4]}:${m[5]}`;
  return formatTs(raw);
}

type SessionSortKey =
  | "status"
  | "trial"
  | "failure"
  | "scenario"
  | "size"
  | "agent"
  | "model"
  | "rca_f1"
  | "detection"
  | "localization_f1"
  | "steps"
  | "tool_calls"
  | "in_tokens"
  | "out_tokens"
  | "backend"
  | "tags"
  | "time"
  | "duration";

const DESC_FIRST_SORT_KEYS = new Set<SessionSortKey>([
  "rca_f1",
  "detection",
  "localization_f1",
  "steps",
  "tool_calls",
  "in_tokens",
  "out_tokens",
  "time",
  "duration",
]);

function scoreChip(value: number | null | undefined): ReactNode {
  return value != null ? <span className="chip score">{formatScore(value)}</span> : "—";
}

function countCell(value: number | null | undefined): ReactNode {
  return value != null ? formatTokenCount(value) : "—";
}

type SessionColumn = {
  id: SessionSortKey;
  label: string;
  /** Numeric columns get a ``<0.5`` / ``>=30`` filter box. */
  numeric?: (s: SessionSummary) => number | null | undefined;
};

const SESSION_COLUMNS: SessionColumn[] = [
  { id: "status", label: "Status" },
  { id: "trial", label: "Trial" },
  { id: "failure", label: "Failure" },
  { id: "scenario", label: "Scenario" },
  { id: "size", label: "Size" },
  { id: "agent", label: "Agent" },
  { id: "model", label: "Model" },
  { id: "backend", label: "Backend" },
  { id: "tags", label: "Tags" },
  { id: "rca_f1", label: "RCA F1", numeric: (s) => s.rca_f1 },
  { id: "detection", label: "Detection", numeric: (s) => s.detection_score },
  { id: "localization_f1", label: "Loc F1", numeric: (s) => s.localization_f1 },
  { id: "steps", label: "Steps", numeric: (s) => s.steps },
  { id: "tool_calls", label: "Tool calls", numeric: (s) => s.tool_calls },
  { id: "in_tokens", label: "In tok", numeric: (s) => s.in_tokens },
  { id: "out_tokens", label: "Out tok", numeric: (s) => s.out_tokens },
  { id: "time", label: "Time" },
  { id: "duration", label: "Duration" },
];

const DEFAULT_COLUMNS: SessionSortKey[] = [
  "status",
  "trial",
  "failure",
  "scenario",
  "size",
  "agent",
  "model",
  "rca_f1",
  "time",
  "duration",
];

const COLUMNS_KEY = "nika-inspect-columns";
const SAVED_VIEWS_KEY = "nika-inspect-saved-views";

function loadColumns(): SessionSortKey[] {
  try {
    const raw = JSON.parse(localStorage.getItem(COLUMNS_KEY) || "null");
    if (Array.isArray(raw)) {
      const known = new Set(SESSION_COLUMNS.map((c) => c.id));
      const cols = raw.filter((c): c is SessionSortKey => known.has(c));
      if (cols.length) return cols;
    }
  } catch {
    /* ignore */
  }
  return DEFAULT_COLUMNS;
}

function saveColumns(columns: SessionSortKey[]) {
  try {
    localStorage.setItem(COLUMNS_KEY, JSON.stringify(columns));
  } catch {
    /* ignore quota / private mode */
  }
}

type SavedView = {
  name: string;
  status: string;
  trial: string;
  scenario: string;
  problem: string;
  topoSize: string;
  agent: string;
  model: string;
  tag: string;
  numeric: Record<string, string>;
  columns: SessionSortKey[];
  sortKey: SessionSortKey | null;
  sortDir: "asc" | "desc";
};

function loadSavedViews(): SavedView[] {
  try {
    const raw = JSON.parse(localStorage.getItem(SAVED_VIEWS_KEY) || "[]");
    return Array.isArray(raw)
      ? raw.filter((v): v is SavedView => typeof v?.name === "string")
      : [];
  } catch {
    return [];
  }
}

function storeSavedViews(views: SavedView[]) {
  try {
    localStorage.setItem(SAVED_VIEWS_KEY, JSON.stringify(views));
  } catch {
    /* ignore quota / private mode */
  }
}

function sessionSortValue(s: SessionSummary, key: SessionSortKey): string | number | null {
  switch (key) {
    case "status":
      return s.status;
    case "trial":
      return s.trial_index ?? null;
    case "failure":
      return sessionTitle(s).toLowerCase();
    case "scenario":
      return (s.scenario_name || "").toLowerCase();
    case "size":
      return (s.scenario_topo_size || "").toLowerCase();
    case "agent":
      return (s.agent_type || "").toLowerCase();
    case "model":
      return (s.model || "").toLowerCase();
    case "rca_f1":
      return s.rca_f1 ?? null;
    case "detection":
      return s.detection_score ?? null;
    case "localization_f1":
      return s.localization_f1 ?? null;
    case "steps":
      return s.steps ?? null;
    case "tool_calls":
      return s.tool_calls ?? null;
    case "in_tokens":
      return s.in_tokens ?? null;
    case "out_tokens":
      return s.out_tokens ?? null;
    case "backend":
      return (s.backend || "").toLowerCase();
    case "tags":
      return (s.tags ?? []).join(" ").toLowerCase();
    case "time":
      return parseTs(s.start_time);
    case "duration": {
      const start = parseTs(s.start_time);
      if (start == null) return null;
      if (s.status === "running") return Date.now() - start;
      const end = parseTs(s.end_time);
      if (end == null || end < start) return null;
      return end - start;
    }
  }
}

function compareSessions(
  a: SessionSummary,
  b: SessionSummary,
  key: SessionSortKey,
  dir: "asc" | "desc",
): number {
  const av = sessionSortValue(a, key);
  const bv = sessionSortValue(b, key);
  const aMissing = av == null || av === "";
  const bMissing = bv == null || bv === "";
  if (aMissing && bMissing) return a.session_id.localeCompare(b.session_id);
  if (aMissing) return 1;
  if (bMissing) return -1;
  let cmp = 0;
  if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
  else cmp = String(av).localeCompare(String(bv), undefined, { numeric: true });
  if (cmp === 0) cmp = a.session_id.localeCompare(b.session_id);
  return dir === "asc" ? cmp : -cmp;
}

type SessionColumnFilters = {
  status: string;
  trial: string;
  scenario: string;
  problem: string;
  topoSize: string;
  agent: string;
  model: string;
  tag: string;
  numeric: Record<string, string>;
  onStatus: (v: string) => void;
  onTrial: (v: string) => void;
  onScenario: (v: string) => void;
  onProblem: (v: string) => void;
  onTopoSize: (v: string) => void;
  onAgent: (v: string) => void;
  onModel: (v: string) => void;
  onTag: (v: string) => void;
  onNumeric: (id: SessionSortKey, value: string) => void;
  facets: SessionFacets;
};

type EnumFilter = {
  value: string;
  allValue: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
};

function enumFilters(cf: SessionColumnFilters): Partial<Record<SessionSortKey, EnumFilter>> {
  const plain = (values: string[] | undefined) =>
    (values ?? []).map((v) => ({ value: v, label: v }));
  const statuses = cf.facets.statuses.length
    ? cf.facets.statuses
    : ["running", "finished", "aborted", "error"];
  return {
    status: { value: cf.status, allValue: "all", onChange: cf.onStatus, options: plain(statuses) },
    trial: {
      value: cf.trial,
      allValue: "",
      onChange: cf.onTrial,
      options: (cf.facets.trial_indices || []).map((idx) => ({
        value: String(idx),
        label: `t${String(idx).padStart(2, "0")}`,
      })),
    },
    failure: { value: cf.problem, allValue: "", onChange: cf.onProblem, options: plain(cf.facets.problems) },
    scenario: { value: cf.scenario, allValue: "", onChange: cf.onScenario, options: plain(cf.facets.scenarios) },
    size: { value: cf.topoSize, allValue: "", onChange: cf.onTopoSize, options: plain(cf.facets.topo_sizes) },
    agent: { value: cf.agent, allValue: "", onChange: cf.onAgent, options: plain(cf.facets.agents) },
    model: { value: cf.model, allValue: "", onChange: cf.onModel, options: plain(cf.facets.models) },
    tags: { value: cf.tag, allValue: "", onChange: cf.onTag, options: plain(cf.facets.tags) },
  };
}

function SessionsTable({
  sessions,
  columns,
  sortKey,
  sortDir,
  onSort,
  contentHits,
  onOpen,
  onDelete,
  showTrial,
  columnFilters,
}: {
  sessions: SessionSummary[];
  columns: SessionSortKey[];
  sortKey: SessionSortKey;
  sortDir: "asc" | "desc";
  onSort: (key: SessionSortKey) => void;
  /** Trajectory search hit counts by session key, while a search is active. */
  contentHits?: Record<string, number>;
  onOpen: (id: string) => void;
  onDelete?: (session: SessionSummary) => void | Promise<void>;
  showTrial?: boolean;
  columnFilters?: SessionColumnFilters;
}) {
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const hasRunning = useMemo(
    () => sessions.some((s) => s.status === "running"),
    [sessions],
  );
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    if (!hasRunning) return;
    setNowMs(Date.now());
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [hasRunning]);

  const sorted = useMemo(() => {
    return [...sessions].sort((a, b) => compareSessions(a, b, sortKey, sortDir));
  }, [sessions, sortKey, sortDir]);

  const shown = SESSION_COLUMNS.filter(
    (c) => columns.includes(c.id) && (c.id !== "trial" || showTrial),
  );
  const filters = columnFilters ? enumFilters(columnFilters) : {};

  const sortTh = (key: SessionSortKey, label: string) => {
    const active = sortKey === key;
    return (
      <th key={key}>
        <button
          type="button"
          className={`sort-btn${active ? " active" : ""}`}
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            onSort(key);
          }}
        >
          {label}
          <span className="sort-ind" aria-hidden>
            {active ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
          </span>
        </button>
      </th>
    );
  };

  const filterCell = (col: SessionColumn): ReactNode => {
    if (!columnFilters) return null;
    const ef = filters[col.id];
    if (ef) {
      return (
        <th key={col.id} className="th-filter-cell">
          <select
            className="th-filter"
            value={ef.value}
            onClick={(e) => e.stopPropagation()}
            onChange={(e) => ef.onChange(e.target.value)}
          >
            <option value={ef.allValue}>All</option>
            {ef.options.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </th>
      );
    }
    if (col.numeric) {
      const text = columnFilters.numeric[col.id] ?? "";
      const invalid = text.trim() !== "" && parseNumericFilter(text) == null;
      return (
        <th key={col.id} className="th-filter-cell">
          <input
            className={`th-filter th-filter-num${invalid ? " invalid" : ""}`}
            placeholder="<0.5"
            title="Numeric filter: <, <=, >, >=, =, != followed by a number"
            value={text}
            onClick={(e) => e.stopPropagation()}
            onChange={(e) => columnFilters.onNumeric(col.id, e.target.value)}
          />
        </th>
      );
    }
    return <th key={col.id} className="th-filter-cell" />;
  };

  const renderCell = (col: SessionColumn, s: SessionSummary): ReactNode => {
    switch (col.id) {
      case "status":
        return (
          <>
            <span className={`chip ${s.status}`}>{s.status}</span>
            {s.stage ? (
              <div className="session-sub" title="Current pipeline stage">
                {s.stage}
              </div>
            ) : null}
          </>
        );
      case "trial":
        return s.trial_index != null ? (
          <span className="chip">t{String(s.trial_index).padStart(2, "0")}</span>
        ) : (
          "—"
        );
      case "failure": {
        const hits = contentHits?.[sessionOpenId(s)];
        return (
          <>
            <div className="session-title" title={s.session_id}>
              {sessionTitle(s)}
              {hits ? (
                <span className="chip hit-chip" title="Trajectory search hits">
                  {hits} {hits === 1 ? "hit" : "hits"}
                </span>
              ) : null}
            </div>
            {s.failure_domain ? (
              <div className="session-sub">{s.failure_domain}</div>
            ) : null}
          </>
        );
      }
      case "scenario":
        return s.scenario_name || "—";
      case "size":
        return s.scenario_topo_size || "—";
      case "agent":
        return s.agent_type || "—";
      case "model":
        return (
          <span className="session-model" title={s.model || undefined}>
            {s.model || "—"}
          </span>
        );
      case "backend":
        return s.backend || "—";
      case "tags":
        return s.tags?.length
          ? s.tags.map((t) => (
              <span key={t} className="chip tag-chip">
                {t}
              </span>
            ))
          : "—";
      case "rca_f1":
        return scoreChip(s.rca_f1);
      case "detection":
        return scoreChip(s.detection_score);
      case "localization_f1":
        return scoreChip(s.localization_f1);
      case "steps":
        return s.steps ?? "—";
      case "tool_calls":
        return s.tool_calls ?? "—";
      case "in_tokens":
        return countCell(s.in_tokens);
      case "out_tokens":
        return countCell(s.out_tokens);
      case "time":
        return <span className="session-time">{sessionShortTime(s)}</span>;
      case "duration":
        return sessionDuration(s, nowMs);
    }
  };

  const requestDelete = async (s: SessionSummary) => {
    if (!onDelete || deletingId) return;
    if (s.status === "running") {
      window.alert(
        "This session is still running. Stop it before deleting the result.",
      );
      return;
    }
    const id = sessionOpenId(s);
    const ok = window.confirm(
      `Delete result for “${sessionTitle(s)}”?\n\nThis removes the session folder under results/ and cannot be undone.`,
    );
    if (!ok) return;
    setDeletingId(id);
    try {
      await onDelete(s);
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <div className="table-wrap">
      <table className="sessions">
        <thead>
          <tr className="th-sort-row">
            {shown.map((col) => sortTh(col.id, col.label))}
            {onDelete ? <th className="th-actions">Actions</th> : null}
          </tr>
          {columnFilters ? (
            <tr className="th-filter-row">
              {shown.map(filterCell)}
              {onDelete ? <th className="th-filter-cell" /> : null}
            </tr>
          ) : null}
        </thead>
        <tbody>
          {sorted.length === 0 && (
            <tr className="sessions-empty-row">
              <td colSpan={shown.length + (onDelete ? 1 : 0)}>
                No sessions match the column filters
              </td>
            </tr>
          )}
          {sorted.map((s) => {
            const id = sessionOpenId(s);
            const busy = deletingId === id;
            return (
              <tr key={id} onClick={() => onOpen(id)}>
                {shown.map((col) => (
                  <td
                    key={col.id}
                    title={col.id === "time" ? s.start_time || undefined : undefined}
                  >
                    {renderCell(col, s)}
                  </td>
                ))}
                {onDelete ? (
                  <td className="td-actions">
                    <button
                      type="button"
                      className="row-delete"
                      disabled={busy || s.status === "running"}
                      title={
                        s.status === "running"
                          ? "Stop the session before deleting its result"
                          : "Delete this session result folder"
                      }
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        void requestDelete(s);
                      }}
                    >
                      {busy ? "…" : "Delete"}
                    </button>
                  </td>
                ) : null}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

type PathTreeNode = {
  path: string;
  name: string;
  count: number;
  isSession: boolean;
  children: PathTreeNode[];
  /** Virtual group: exact session keys (not a real directory prefix). */
  memberKeys?: string[];
};

function sessionPathKey(s: SessionSummary): string {
  return s.session_key || s.session_id;
}

function trialIndexOf(s: SessionSummary): number | null {
  if (s.trial_index != null && Number.isFinite(s.trial_index)) return s.trial_index;
  const leaf = sessionPathKey(s).split("/").pop() || s.session_id;
  const m = leaf.match(/__t(\d+)$/i);
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isFinite(n) ? n : null;
}

function isTrialSession(s: SessionSummary): boolean {
  return trialIndexOf(s) != null;
}

function buildTrialIndexFolders(
  parentPath: string,
  trialLeaves: PathTreeNode[],
  sessionsByKey: Map<string, SessionSummary>,
): PathTreeNode[] {
  const byTrial = new Map<number, PathTreeNode[]>();
  for (const leaf of trialLeaves) {
    const session = sessionsByKey.get(leaf.path);
    if (!session) continue;
    const idx = trialIndexOf(session);
    if (idx == null) continue;
    const list = byTrial.get(idx) || [];
    list.push(leaf);
    byTrial.set(idx, list);
  }

  const folders: PathTreeNode[] = [];
  for (const idx of [...byTrial.keys()].sort((a, b) => a - b)) {
    const leaves = byTrial.get(idx) || [];
    const label = `t${String(idx).padStart(2, "0")}`;
    const trialPath = parentPath ? `${parentPath}/~${label}~` : `~${label}~`;
    folders.push({
      path: trialPath,
      name: label,
      count: leaves.length,
      isSession: false,
      children: [],
      // Selecting t01/t02 shows the session table directly — no per-case folders.
      memberKeys: leaves.map((l) => l.path),
    });
  }
  return folders;
}

/**
 * Prefer ``t01``, ``t02`` over a flat ``trials/case__t01`` list.
 * Replaces a real ``trials/`` folder of trial leaves, or wraps loose trial leaves.
 * Do not wrap inside ``trials/`` itself — that would nest as ``trials/t01/...``.
 */
function regroupByTrialIndex(
  parentPath: string,
  children: PathTreeNode[],
  sessionsByKey: Map<string, SessionSummary>,
  opts: { insideTrials?: boolean } = {},
): PathTreeNode[] {
  const mapped = children.map((child) => ({
    ...child,
    children: regroupByTrialIndex(child.path, child.children, sessionsByKey, {
      insideTrials: child.name === "trials",
    }),
  }));

  const trialsFolder = mapped.find((c) => c.name === "trials" && !c.isSession);
  if (trialsFolder) {
    const trialLeaves = trialsFolder.children.filter(
      (c) => c.isSession && sessionsByKey.has(c.path) && isTrialSession(sessionsByKey.get(c.path)!),
    );
    const otherUnderTrials = trialsFolder.children.filter((c) => !trialLeaves.includes(c));
    if (trialLeaves.length >= 1) {
      const folders = buildTrialIndexFolders(parentPath, trialLeaves, sessionsByKey);
      const rest = mapped.filter((c) => c !== trialsFolder);
      // Keep non-trial content under a residual trials folder if any.
      if (otherUnderTrials.length) {
        rest.push({
          ...trialsFolder,
          children: otherUnderTrials,
          count: otherUnderTrials.reduce((n, c) => n + c.count, 0),
          memberKeys: undefined,
        });
      }
      return [...rest, ...folders].sort((a, b) =>
        a.name.localeCompare(b.name, undefined, { numeric: true }),
      );
    }
  }

  if (opts.insideTrials) return mapped;

  const trialLeaves = mapped.filter(
    (c) =>
      c.isSession &&
      c.children.length === 0 &&
      sessionsByKey.has(c.path) &&
      isTrialSession(sessionsByKey.get(c.path)!),
  );
  if (trialLeaves.length >= 2) {
    const rest = mapped.filter((c) => !trialLeaves.includes(c));
    const folders = buildTrialIndexFolders(parentPath, trialLeaves, sessionsByKey);
    return [...rest, ...folders].sort((a, b) =>
      a.name.localeCompare(b.name, undefined, { numeric: true }),
    );
  }

  return mapped;
}

/** Drop session dirs from the sidebar — selecting the parent lists them in the table. */
function pruneSessionLeaves(nodes: PathTreeNode[]): PathTreeNode[] {
  return nodes
    .filter((n) => !n.isSession)
    .map((n) => ({
      ...n,
      children: pruneSessionLeaves(n.children),
    }));
}

/**
 * Flat ``results/<session>`` trees would disappear after pruning; keep them
 * reachable via one virtual folder.
 */
function wrapRootSessionOrphans(nodes: PathTreeNode[]): PathTreeNode[] {
  const sessionLeaves = nodes.filter((n) => n.isSession);
  if (sessionLeaves.length === 0) return nodes;
  const rest = nodes.filter((n) => !n.isSession);
  const wrapped: PathTreeNode = {
    path: "~sessions~",
    name: "sessions",
    count: sessionLeaves.reduce((n, c) => n + c.count, 0),
    isSession: false,
    children: [],
    memberKeys: sessionLeaves.map((l) => l.path),
  };
  return [...rest, wrapped].sort((a, b) =>
    a.name.localeCompare(b.name, undefined, { numeric: true }),
  );
}

function buildPathTree(sessions: SessionSummary[]): PathTreeNode[] {
  type Mutable = {
    path: string;
    name: string;
    count: number;
    isSession: boolean;
    children: Map<string, Mutable>;
  };
  const root: Mutable = {
    path: "",
    name: "",
    count: 0,
    isSession: false,
    children: new Map(),
  };

  const sessionsByKey = new Map<string, SessionSummary>();
  for (const s of sessions) {
    sessionsByKey.set(sessionPathKey(s), s);
  }

  for (const s of sessions) {
    const key = sessionPathKey(s);
    const parts = key.split("/").filter(Boolean);
    if (!parts.length) continue;
    let node = root;
    let acc = "";
    for (let i = 0; i < parts.length; i++) {
      const part = parts[i];
      acc = acc ? `${acc}/${part}` : part;
      let child = node.children.get(part);
      if (!child) {
        child = {
          path: acc,
          name: part,
          count: 0,
          isSession: false,
          children: new Map(),
        };
        node.children.set(part, child);
      }
      child.count += 1;
      if (i === parts.length - 1) child.isSession = true;
      node = child;
    }
  }

  const toList = (m: Mutable): PathTreeNode[] =>
    [...m.children.values()]
      .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }))
      .map((c) => ({
        path: c.path,
        name: c.name,
        count: c.count,
        isSession: c.isSession && c.children.size === 0,
        children: toList(c),
      }));

  const regrouped = regroupByTrialIndex("", toList(root), sessionsByKey);
  return pruneSessionLeaves(wrapRootSessionOrphans(regrouped));
}

function sessionsUnderSelection(
  sessions: SessionSummary[],
  path: string,
  memberKeys?: string[] | null,
): SessionSummary[] {
  if (memberKeys?.length) {
    const set = new Set(memberKeys);
    return sessions.filter((s) => set.has(sessionPathKey(s)));
  }
  if (!path) return sessions;
  return sessions.filter((s) => {
    const key = sessionPathKey(s);
    return key === path || key.startsWith(`${path}/`);
  });
}

function findPathNode(nodes: PathTreeNode[], path: string): PathTreeNode | null {
  for (const node of nodes) {
    if (node.path === path) return node;
    const child = findPathNode(node.children, path);
    if (child) return child;
  }
  return null;
}

const TREE_EXPANDED_KEY = "nika-inspect-tree-expanded";

function loadTreeExpanded(): Set<string> {
  try {
    const raw = localStorage.getItem(TREE_EXPANDED_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((x): x is string => typeof x === "string"));
  } catch {
    return new Set();
  }
}

function saveTreeExpanded(ids: Set<string>) {
  try {
    localStorage.setItem(TREE_EXPANDED_KEY, JSON.stringify([...ids]));
  } catch {
    /* ignore */
  }
}

function FolderGlyph() {
  return (
    <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden>
      <path
        fill="#3b82f6"
        d="M2 3.5A1.5 1.5 0 0 1 3.5 2H7l1.5 1.5H12.5A1.5 1.5 0 0 1 14 5v7.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 2 12.5z"
      />
    </svg>
  );
}

function joinFsPath(root: string, rel: string | null): string {
  const base = root.replace(/\/+$/, "");
  if (!rel || rel === "." || rel === "") return base;
  return `${base}/${rel.replace(/^\/+/, "")}`;
}

function pathIsUnder(child: string, parent: string): boolean {
  const c = child.replace(/\/+$/, "");
  const p = parent.replace(/\/+$/, "");
  return c === p || c.startsWith(`${p}/`);
}

function pathsOverlap(a: string, b: string): boolean {
  return pathIsUnder(a, b) || pathIsUnder(b, a);
}

function sessionsUnderResultDir(
  sessions: SessionSummary[],
  resultDir: string,
): SessionSummary[] {
  const root = resultDir.replace(/\/+$/, "");
  return sessions.filter((s) => pathIsUnder(s.session_dir, root));
}

function meanOf(values: number[]): number | null {
  if (!values.length) return null;
  return values.reduce((a, b) => a + b, 0) / values.length;
}

type RunMonitorStats = {
  nika: { running: number; finished: number; aborted: number; error: number };
  agent: {
    ok: number;
    fail: number;
    passRate: number | null;
    meanRca: number | null;
    meanLoc: number | null;
    meanDet: number | null;
    meanInTok: number | null;
    meanOutTok: number | null;
    meanSteps: number | null;
    meanTools: number | null;
    sumInTok: number | null;
    sumOutTok: number | null;
  };
};

function aggregateRunSessions(sessions: SessionSummary[]): RunMonitorStats {
  const nika = { running: 0, finished: 0, aborted: 0, error: 0 };
  let ok = 0;
  let fail = 0;
  const rca: number[] = [];
  const loc: number[] = [];
  const det: number[] = [];
  const inTok: number[] = [];
  const outTok: number[] = [];
  const steps: number[] = [];
  const tools: number[] = [];
  for (const s of sessions) {
    if (s.status === "running") nika.running += 1;
    else if (s.status === "finished") nika.finished += 1;
    else if (s.status === "aborted") nika.aborted += 1;
    else if (s.status === "error") nika.error += 1;
    if (s.outcome === "success") ok += 1;
    else if (s.outcome === "agent_failed") fail += 1;
    if (s.rca_f1 != null) rca.push(s.rca_f1);
    if (s.localization_f1 != null) loc.push(s.localization_f1);
    if (s.detection_score != null) det.push(s.detection_score);
    if (s.in_tokens != null) inTok.push(s.in_tokens);
    if (s.out_tokens != null) outTok.push(s.out_tokens);
    if (s.steps != null) steps.push(s.steps);
    if (s.tool_calls != null) tools.push(s.tool_calls);
  }
  const decided = ok + fail;
  return {
    nika,
    agent: {
      ok,
      fail,
      passRate: decided > 0 ? (ok / decided) * 100 : null,
      meanRca: meanOf(rca),
      meanLoc: meanOf(loc),
      meanDet: meanOf(det),
      meanInTok: meanOf(inTok),
      meanOutTok: meanOf(outTok),
      meanSteps: meanOf(steps),
      meanTools: meanOf(tools),
      sumInTok: inTok.length ? inTok.reduce((a, b) => a + b, 0) : null,
      sumOutTok: outTok.length ? outTok.reduce((a, b) => a + b, 0) : null,
    },
  };
}

/**
 * Progress doc for the selected folder: a running run first, else the latest
 * stopped one. Running runs also match ancestor folders; stopped runs only
 * match their own result folder (or a folder inside it).
 */
function pickRunProgress(
  runs: BenchmarkProgressDoc[],
  folderAbs: string,
): BenchmarkProgressDoc | null {
  const matches = runs.filter((r) =>
    r.status === "running"
      ? pathsOverlap(r.result_dir, folderAbs)
      : pathIsUnder(folderAbs, r.result_dir),
  );
  if (!matches.length) return null;
  matches.sort((a, b) => {
    const ra = a.status === "running" ? 1 : 0;
    const rb = b.status === "running" ? 1 : 0;
    if (ra !== rb) return rb - ra;
    const ta = a.updated_at || "";
    const tb = b.updated_at || "";
    if (ta !== tb) return tb.localeCompare(ta);
    return b.run_id.localeCompare(a.run_id);
  });
  return matches[0];
}

function fmtAvg(value: number | null, digits = 0): string {
  if (value == null) return "—";
  return digits === 0 ? String(Math.round(value)) : value.toFixed(digits);
}

function fmtCompactCount(value: number | null): string {
  if (value == null) return "—";
  const n = Math.round(value);
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

function fmtRate(value: number | null, digits = 1): string {
  if (value == null) return "—";
  return `${value.toFixed(digits)}%`;
}

function fmtPace(value: number | null): string {
  if (value == null) return "—";
  if (value >= 10) return value.toFixed(1);
  return value.toFixed(2);
}

function AgentStat({
  label,
  value,
  tip,
  tone,
  active,
  onSelect,
}: {
  label: string;
  value: string;
  tip: string;
  tone?: "good" | "bad" | "warn" | "neutral";
  active?: boolean;
  onSelect?: () => void;
}) {
  const clickable = onSelect != null;
  const className = [
    "run-stat",
    tone ? `run-stat-${tone}` : "",
    clickable ? "run-stat-clickable" : "",
    active ? "run-stat-active" : "",
  ]
    .filter(Boolean)
    .join(" ");
  if (clickable) {
    return (
      <button
        type="button"
        className={className}
        title={tip}
        aria-pressed={active}
        onClick={onSelect}
      >
        <span className="run-stat-key">{label}</span>
        <span className="run-stat-value">{value}</span>
      </button>
    );
  }
  return (
    <span className={className} title={tip}>
      <span className="run-stat-key">{label}</span>
      <span className="run-stat-value">{value}</span>
    </span>
  );
}

type MonitorListFilter =
  | { kind: "status"; value: "running" | "finished" | "aborted" | "error" }
  | { kind: "outcome"; value: "success" | "agent_failed" };

function monitorFilterLabel(filter: MonitorListFilter): string {
  if (filter.kind === "status") return `status=${filter.value}`;
  if (filter.value === "agent_failed") return "outcome=agent_failed";
  return "outcome=success";
}

function applyMonitorListFilter(
  sessions: SessionSummary[],
  filter: MonitorListFilter | null,
): SessionSummary[] {
  if (!filter) return sessions;
  if (filter.kind === "status") {
    return sessions.filter((s) => s.status === filter.value);
  }
  return sessions.filter((s) => s.outcome === filter.value);
}

function toggleMonitorFilter(
  current: MonitorListFilter | null,
  next: MonitorListFilter,
): MonitorListFilter | null {
  if (
    current &&
    current.kind === next.kind &&
    current.value === next.value
  ) {
    return null;
  }
  return next;
}

/** Hover copy for run-monitor chips (operator reference). */
const RUN_MONITOR_TIPS = {
  status: "Suite status from runtime/benchmark_runs: running, finished, or aborted.",
  progress:
    "completed / total. Pending = total - completed from the progress file.",
  updated: "Last write time of the suite progress file.",
  running: "Sessions with status=running. Click to filter the list.",
  finished: "Sessions with status=finished. Click to filter the list.",
  error: "Sessions with status=error. Click to filter the list.",
  aborted: "Sessions with status=aborted. Click to filter the list.",
  labMin: (n: number) =>
    `Shortest finished-lab duration among ${n} sessions. min(end - start).`,
  labAvg: (n: number) =>
    `Mean finished-lab duration among ${n} sessions. avg(end - start).`,
  labMax: (n: number) =>
    `Longest finished-lab duration among ${n} sessions. max(end - start).`,
  suiteElapsed:
    "Active execution time: union of session intervals (idle gaps when the suite is paused are excluded).",
  trialsPerHour:
    "completed / suite_elapsed_hours. Throughput of finished trials so far.",
  ok: "Trials with outcome=success. Click to filter the list.",
  fail: "Trials with outcome=agent_failed. Click to filter the list.",
  passRate: "success / (success + agent_failed) × 100.",
  avgRca: "avg(rca_f1) over trials that reported it.",
  avgLoc: "avg(localization_f1) over trials that reported it.",
  avgDet: "avg(detection_score) over trials that reported it.",
  avgInTok: "avg(in_tokens) over trials that reported it (prompt tokens).",
  avgOutTok: "avg(out_tokens) over trials that reported it (completion tokens).",
  outTokPerSec:
    "sum(out_tokens) / suite_elapsed_seconds. Completion-token generation rate over active suite time.",
  avgSteps: "avg(steps) over trials that reported it.",
  avgTools: "avg(tool_calls) over trials that reported it.",
  llmMin: "Shortest completed LLM request duration in this run.",
  llmAvg: "Mean completed LLM request duration in this run.",
  llmMax: "Longest completed LLM request duration in this run.",
} as const;

function RunMonitor({
  progress,
  sessions,
  root,
  listFilter,
  onListFilter,
}: {
  progress: BenchmarkProgressDoc | null;
  sessions: SessionSummary[];
  root: string;
  listFilter: MonitorListFilter | null;
  onListFilter: (next: MonitorListFilter | null) => void;
}) {
  const stats = useMemo(() => aggregateRunSessions(sessions), [sessions]);
  const durationStats = useMemo(() => labDurationStats(sessions), [sessions]);
  const [nowMs, setNowMs] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNowMs(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  const elapsedMs = useMemo(
    () => suiteElapsedMs(sessions, nowMs),
    [sessions, nowMs],
  );
  const [llmRows, setLlmRows] = useState<DisplayEvent[]>([]);
  useEffect(() => {
    let cancelled = false;
    if (!sessions.length) {
      setLlmRows([]);
      return;
    }
    void Promise.all(
      sessions.map((s) => {
        const id = sessionOpenId(s);
        const live = s.status === "running";
        return fetchTimeline(id, "agent", root)
          .then((t) => {
            // Match SessionView: freeze unpaired llm_start once the session is
            // done, otherwise LLM max keeps ticking past the lab timeline.
            const collapsed = collapsePairedEvents(t.events);
            const closed = closeSupersededOpenSpans(collapsed);
            if (live) return closed;
            return closed.map((r) =>
              isRunningSpan(r)
                ? {
                    ...r,
                    stale: true,
                    summary:
                      r.summary === "in progress"
                        ? "no end logged"
                        : r.summary,
                  }
                : r,
            );
          })
          .catch(() => [] as DisplayEvent[]);
      }),
    ).then((parts) => {
      if (!cancelled) setLlmRows(parts.flat());
    });
    return () => {
      cancelled = true;
    };
  }, [sessions, root]);
  const llmStats = useMemo(() => llmDurationStats(llmRows), [llmRows]);
  const label =
    progress?.benchmark_id != null
      ? `${progress.benchmark_id}${progress.version ? `@${progress.version}` : ""}`
      : sessions.find((s) => s.benchmark_label)?.benchmark_label ||
        sessions.find((s) => s.benchmark_id)?.benchmark_id ||
        "Benchmark";
  const agentType =
    progress?.agent_type || sessions.find((s) => s.agent_type)?.agent_type;
  const model = progress?.model || sessions.find((s) => s.model)?.model;
  const completed =
    progress?.completed_trials ??
    sessions.filter((s) => s.outcome === "success" || s.outcome === "agent_failed")
      .length;
  const total =
    progress?.total_trials ||
    sessions.find((s) => s.benchmark_n_trials)?.benchmark_n_trials ||
    sessions.length;
  const pending =
    progress?.pending_trials ?? Math.max(0, total - completed);
  const status =
    progress?.status ||
    (sessions.some((s) => s.status === "running")
      ? "running"
      : sessions.some((s) => s.status === "aborted")
        ? "aborted"
        : "finished");
  const nFinished = durationStats.n;
  const pace = trialsPerHour(completed, elapsedMs);
  const tokRate = outTokensPerSec(stats.agent.sumOutTok, elapsedMs);

  const selectStatus = (
    value: "running" | "finished" | "aborted" | "error",
  ) => {
    onListFilter(
      toggleMonitorFilter(listFilter, { kind: "status", value }),
    );
  };
  const selectOutcome = (value: "success" | "agent_failed") => {
    onListFilter(
      toggleMonitorFilter(listFilter, { kind: "outcome", value }),
    );
  };
  const isStatus = (value: string) =>
    listFilter?.kind === "status" && listFilter.value === value;
  const isOutcome = (value: string) =>
    listFilter?.kind === "outcome" && listFilter.value === value;

  return (
    <div className="run-monitor benchmark-card">
      <div className="benchmark-card-head run-monitor-head">
        <div>
          <div className="run-monitor-title-row">
            <span className={`chip ${status}`} title={RUN_MONITOR_TIPS.status}>
              {status}
            </span>
            <h2>{label}</h2>
          </div>
          <div className="benchmark-meta">
            {[agentType, model].filter(Boolean).join(" · ") || "—"}
            {progress?.run_id ? ` · run ${progress.run_id}` : ""}
          </div>
        </div>
        <div className="benchmark-stats run-monitor-progress">
          <span className="run-monitor-progress-text" title={RUN_MONITOR_TIPS.progress}>
            <strong>
              {completed}/{total}
            </strong>
            {pending > 0 ? ` · ${pending} pending` : ""}
          </span>
          {progress?.updated_at && (
            <span
              className="run-monitor-updated"
              title={`${RUN_MONITOR_TIPS.updated} ${progress.updated_at}`}
            >
              updated {formatTs(progress.updated_at)}
            </span>
          )}
        </div>
      </div>
      <div className="run-monitor-body">
        <div className="run-monitor-row">
          <span className="run-monitor-label">NIKA</span>
          <div className="run-monitor-metrics">
            <AgentStat
              label="Running"
              value={String(stats.nika.running)}
              tone="warn"
              tip={RUN_MONITOR_TIPS.running}
              active={isStatus("running")}
              onSelect={() => selectStatus("running")}
            />
            <AgentStat
              label="Finished"
              value={String(stats.nika.finished)}
              tone="good"
              tip={RUN_MONITOR_TIPS.finished}
              active={isStatus("finished")}
              onSelect={() => selectStatus("finished")}
            />
            <AgentStat
              label="Error"
              value={String(stats.nika.error)}
              tone="bad"
              tip={RUN_MONITOR_TIPS.error}
              active={isStatus("error")}
              onSelect={() => selectStatus("error")}
            />
            <AgentStat
              label="Aborted"
              value={String(stats.nika.aborted)}
              tip={RUN_MONITOR_TIPS.aborted}
              active={isStatus("aborted")}
              onSelect={() => selectStatus("aborted")}
            />
            <span className="run-monitor-sep" aria-hidden />
            {nFinished > 0 && (
              <>
                <AgentStat
                  label="Lab min"
                  value={formatDurationSeconds(durationStats.minMs)}
                  tip={RUN_MONITOR_TIPS.labMin(nFinished)}
                />
                <AgentStat
                  label="Lab avg"
                  value={formatDurationSeconds(durationStats.avgMs)}
                  tip={RUN_MONITOR_TIPS.labAvg(nFinished)}
                />
                <AgentStat
                  label="Lab max"
                  value={formatDurationSeconds(durationStats.maxMs)}
                  tip={RUN_MONITOR_TIPS.labMax(nFinished)}
                />
                <span className="run-monitor-sep" aria-hidden />
              </>
            )}
            <AgentStat
              label="Suite elapsed"
              value={formatDurationSeconds(elapsedMs)}
              tip={RUN_MONITOR_TIPS.suiteElapsed}
            />
            <AgentStat
              label="Trials / h"
              value={fmtPace(pace)}
              tip={RUN_MONITOR_TIPS.trialsPerHour}
            />
          </div>
        </div>
        <div className="run-monitor-row">
          <span className="run-monitor-label">Agent</span>
          <div className="run-monitor-metrics">
            <AgentStat
              label="Success"
              value={String(stats.agent.ok)}
              tone="good"
              tip={RUN_MONITOR_TIPS.ok}
              active={isOutcome("success")}
              onSelect={() => selectOutcome("success")}
            />
            <AgentStat
              label="Failed"
              value={String(stats.agent.fail)}
              tone="bad"
              tip={RUN_MONITOR_TIPS.fail}
              active={isOutcome("agent_failed")}
              onSelect={() => selectOutcome("agent_failed")}
            />
            <AgentStat
              label="Pass rate"
              value={fmtRate(stats.agent.passRate, 0)}
              tip={RUN_MONITOR_TIPS.passRate}
            />
            <span className="run-monitor-sep" aria-hidden />
            <AgentStat
              label="Avg RCA"
              value={formatScore(stats.agent.meanRca)}
              tip={RUN_MONITOR_TIPS.avgRca}
            />
            <AgentStat
              label="Avg loc"
              value={formatScore(stats.agent.meanLoc)}
              tip={RUN_MONITOR_TIPS.avgLoc}
            />
            <AgentStat
              label="Avg det"
              value={formatScore(stats.agent.meanDet)}
              tip={RUN_MONITOR_TIPS.avgDet}
            />
            <span className="run-monitor-sep" aria-hidden />
            <AgentStat
              label="Avg in tokens"
              value={fmtCompactCount(stats.agent.meanInTok)}
              tip={RUN_MONITOR_TIPS.avgInTok}
            />
            <AgentStat
              label="Avg out tokens"
              value={fmtCompactCount(stats.agent.meanOutTok)}
              tip={RUN_MONITOR_TIPS.avgOutTok}
            />
            <AgentStat
              label="Out tok/s"
              value={fmtTokPerSec(tokRate)}
              tip={RUN_MONITOR_TIPS.outTokPerSec}
            />
            <span className="run-monitor-sep" aria-hidden />
            <AgentStat
              label="LLM min"
              value={formatDuration(llmStats.minMs)}
              tip={RUN_MONITOR_TIPS.llmMin}
            />
            <AgentStat
              label="LLM avg"
              value={formatDuration(llmStats.avgMs)}
              tip={RUN_MONITOR_TIPS.llmAvg}
            />
            <AgentStat
              label="LLM max"
              value={formatDuration(llmStats.maxMs)}
              tip={RUN_MONITOR_TIPS.llmMax}
            />
            <span className="run-monitor-sep" aria-hidden />
            <AgentStat
              label="Avg steps"
              value={fmtAvg(stats.agent.meanSteps)}
              tip={RUN_MONITOR_TIPS.avgSteps}
            />
            <AgentStat
              label="Avg tools"
              value={fmtAvg(stats.agent.meanTools, 1)}
              tip={RUN_MONITOR_TIPS.avgTools}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

const SIDEBAR_WIDTH_KEY = "nika-inspect-sidebar-width-v3";
const SIDEBAR_WIDTH_DEFAULT = 176;
const SIDEBAR_WIDTH_MIN = 160;
const SIDEBAR_WIDTH_MAX = 520;

function loadSidebarWidth(): number {
  try {
    const raw = localStorage.getItem(SIDEBAR_WIDTH_KEY);
    if (!raw) return SIDEBAR_WIDTH_DEFAULT;
    const n = Number(raw);
    if (!Number.isFinite(n)) return SIDEBAR_WIDTH_DEFAULT;
    return Math.min(SIDEBAR_WIDTH_MAX, Math.max(SIDEBAR_WIDTH_MIN, n));
  } catch {
    return SIDEBAR_WIDTH_DEFAULT;
  }
}

function ViewShell({
  sessionId,
  sessionNav,
  root,
  onOpenSession,
  onClearSession,
  onSessionNavChange,
}: {
  sessionId: string | null;
  sessionNav: SessionNav;
  root: string;
  onOpenSession: (id: string, find?: string) => void;
  onClearSession: () => void;
  onSessionNavChange: (nav: SessionNav) => void;
}) {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [facets, setFacets] = useState<SessionFacets>({
    statuses: [],
    scenarios: [],
    agents: [],
    models: [],
    problems: [],
    failure_domains: [],
    topo_sizes: [],
    trial_indices: [],
  });
  const [resultsRoot, setResultsRoot] = useState("");
  const [runProgress, setRunProgress] = useState<BenchmarkProgressDoc[]>([]);
  const [status, setStatus] = useState("all");
  const [trial, setTrial] = useState("");
  const [scenario, setScenario] = useState("");
  const [agent, setAgent] = useState("");
  const [model, setModel] = useState("");
  const [problem, setProblem] = useState("");
  const [topoSize, setTopoSize] = useState("");
  const [tag, setTag] = useState("");
  const [q, setQ] = useState("");
  const [debouncedQ, setDebouncedQ] = useState("");
  const [content, setContent] = useState("");
  const [debouncedContent, setDebouncedContent] = useState("");
  const [contentHits, setContentHits] = useState<Record<string, number>>({});
  const [numericFilters, setNumericFilters] = useState<Record<string, string>>({});
  const [columns, setColumns] = useState<SessionSortKey[]>(loadColumns);
  const [sortKey, setSortKey] = useState<SessionSortKey | null>(null);
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [savedViews, setSavedViews] = useState<SavedView[]>(loadSavedViews);
  const [activeView, setActiveView] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  /** Bumped on delete so an in-flight poll cannot bring the row back. */
  const listGenRef = useRef(0);
  const viewingSession = sessionId != null;
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(loadTreeExpanded);
  const [listFilter, setListFilter] = useState<MonitorListFilter | null>(null);
  const [sidebarWidth, setSidebarWidth] = useState(loadSidebarWidth);
  const sidebarDragging = useRef(false);
  const sidebarWidthRef = useRef(sidebarWidth);
  const shellRef = useRef<HTMLDivElement>(null);
  sidebarWidthRef.current = sidebarWidth;

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!sidebarDragging.current || !shellRef.current) return;
      const left = shellRef.current.getBoundingClientRect().left;
      setSidebarWidth(
        Math.min(
          SIDEBAR_WIDTH_MAX,
          Math.max(SIDEBAR_WIDTH_MIN, e.clientX - left),
        ),
      );
    };
    const onUp = () => {
      if (!sidebarDragging.current) return;
      sidebarDragging.current = false;
      document.body.classList.remove("is-resizing");
      try {
        localStorage.setItem(
          SIDEBAR_WIDTH_KEY,
          String(sidebarWidthRef.current),
        );
      } catch {
        /* ignore */
      }
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  useEffect(() => {
    const id = window.setTimeout(() => setDebouncedQ(q), 250);
    return () => window.clearTimeout(id);
  }, [q]);

  useEffect(() => {
    const id = window.setTimeout(() => setDebouncedContent(content.trim()), 400);
    return () => window.clearTimeout(id);
  }, [content]);

  // A different results root is a different list: show loading once.
  useEffect(() => {
    setLoading(true);
  }, [root]);

  // Poll the list only while it is on screen; filter changes keep the old
  // table visible (only the very first load shows "Loading sessions…").
  useEffect(() => {
    let cancelled = false;
    const params = new URLSearchParams({ status });
    if (scenario) params.set("scenario", scenario);
    if (agent) params.set("agent", agent);
    if (model) params.set("model", model);
    if (problem) params.set("problem", problem);
    if (topoSize) params.set("topo_size", topoSize);
    if (trial) params.set("trial_index", trial);
    if (tag) params.set("tag", tag);
    if (debouncedQ.trim()) params.set("q", debouncedQ.trim());
    if (debouncedContent) params.set("content", debouncedContent);
    const load = (background: boolean) => {
      const gen = listGenRef.current;
      fetchSessions(params, root)
        .then((data) => {
          if (cancelled || gen !== listGenRef.current) return;
          setSessions(data.sessions);
          setContentHits(data.content_hits ?? {});
          if (data.facets) setFacets(data.facets);
          setResultsRoot(data.results_root);
          setError(null);
        })
        .catch((err: Error) => {
          if (!cancelled && !background) setError(err.message);
        })
        .finally(() => {
          if (!cancelled && !background) setLoading(false);
        });
    };
    load(false);
    // Trajectory search scans every log, so it runs once per query, not per poll.
    const poll =
      viewingSession || debouncedContent
        ? undefined
        : window.setInterval(() => load(true), 3000);
    return () => {
      cancelled = true;
      window.clearInterval(poll);
    };
  }, [
    root,
    status,
    trial,
    scenario,
    agent,
    model,
    problem,
    topoSize,
    tag,
    debouncedQ,
    debouncedContent,
    viewingSession,
  ]);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      fetchBenchmarkProgress({ root, status: "all" })
        .then((data) => {
          if (!cancelled) setRunProgress(data.runs);
        })
        .catch(() => {
          if (!cancelled) setRunProgress([]);
        });
    };
    load();
    const poll = viewingSession ? undefined : window.setInterval(load, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(poll);
    };
  }, [root, viewingSession]);

  const tree = useMemo(() => buildPathTree(sessions), [sessions]);

  useEffect(() => {
    if (loading || !tree.length) return;
    if (selectedPath != null && findPathNode(tree, selectedPath)) return;
    setSelectedPath(tree[0].path);
    setExpanded((prev) => {
      const next = new Set(prev);
      next.add(tree[0].path);
      saveTreeExpanded(next);
      return next;
    });
  }, [selectedPath, loading, tree]);

  const selectedNode = useMemo(
    () => (selectedPath == null ? null : findPathNode(tree, selectedPath)),
    [tree, selectedPath],
  );

  const visibleSessions = useMemo(
    () =>
      selectedPath == null
        ? []
        : sessionsUnderSelection(sessions, selectedPath, selectedNode?.memberKeys),
    [sessions, selectedPath, selectedNode],
  );

  const folderAbs = useMemo(() => {
    if (!resultsRoot || selectedPath == null) return null;
    return joinFsPath(resultsRoot, selectedPath);
  }, [resultsRoot, selectedPath]);

  const activeProgress = useMemo(() => {
    if (!folderAbs) return null;
    return pickRunProgress(runProgress, folderAbs);
  }, [runProgress, folderAbs]);

  const monitorSessions = useMemo(() => {
    if (activeProgress) {
      return sessionsUnderResultDir(sessions, activeProgress.result_dir);
    }
    if (!folderAbs || selectedPath == null) return [];
    const under = sessionsUnderSelection(
      sessions,
      selectedPath,
      selectedNode?.memberKeys,
    );
    const bench = under.filter((s) => s.is_benchmark);
    if (bench.some((s) => s.status === "running")) return bench;
    // Runs without a progress doc (e.g. from a cases file) stay visible after
    // they stop, as long as the folder holds a single agent/model/benchmark.
    // Sessions aborted before run metadata was written carry no identity.
    const runKeys = new Set(
      bench
        .map((s) => [s.agent_type, s.model, s.benchmark_id, s.benchmark_run_id])
        .filter((k) => k.some((v) => v != null && v !== ""))
        .map((k) => k.join("|")),
    );
    return runKeys.size === 1 ? bench : [];
  }, [activeProgress, sessions, folderAbs, selectedPath, selectedNode]);

  const showMonitor =
    !sessionId && (activeProgress != null || monitorSessions.length > 0);

  // Monitor click-filters apply across the whole run, not just the selected trial folder.
  const tableSessions = useMemo(() => {
    const base =
      listFilter && monitorSessions.length
        ? applyMonitorListFilter(monitorSessions, listFilter)
        : applyMonitorListFilter(visibleSessions, listFilter);
    const active = SESSION_COLUMNS.flatMap((col) => {
      const parsed = col.numeric ? parseNumericFilter(numericFilters[col.id] ?? "") : null;
      return parsed && col.numeric ? [{ get: col.numeric, filter: parsed }] : [];
    });
    if (!active.length) return base;
    return base.filter((s) =>
      active.every(({ get, filter }) => matchesNumericFilter(get(s), filter)),
    );
  }, [listFilter, monitorSessions, visibleSessions, numericFilters]);

  const onMonitorListFilter = useCallback((next: MonitorListFilter | null) => {
    setListFilter(next);
    // Outcome filters need every status; reset the table Status dropdown.
    if (next?.kind === "outcome") setStatus("all");
    if (next?.kind === "status") setStatus("all");
  }, []);

  const breadcrumb = selectedPath
    ? selectedPath
        .split("/")
        .map((p) => {
          const m = p.match(/^~t(\d+)~$/i);
          if (m) return `t${String(m[1]).padStart(2, "0")}`;
          if (p === "~trials~") return null;
          if (p === "~sessions~") return "sessions";
          return p;
        })
        .filter((p): p is string => Boolean(p))
        .join(" / ")
    : "";

  const columnFilters: SessionColumnFilters = {
    status,
    trial,
    scenario,
    problem,
    topoSize,
    agent,
    model,
    tag,
    numeric: numericFilters,
    onStatus: setStatus,
    onTrial: setTrial,
    onScenario: setScenario,
    onProblem: setProblem,
    onTopoSize: setTopoSize,
    onAgent: setAgent,
    onModel: setModel,
    onTag: setTag,
    onNumeric: (id, value) =>
      setNumericFilters((prev) => {
        const next = { ...prev };
        if (value.trim()) next[id] = value;
        else delete next[id];
        return next;
      }),
    facets,
  };

  // Keep the table (and its filter row) on screen while column filters hide every row.
  const columnFiltersActive =
    status !== "all" ||
    Boolean(trial || scenario || problem || topoSize || agent || model || tag) ||
    Object.keys(numericFilters).length > 0;

  const currentView = (name: string): SavedView => ({
    name,
    status,
    trial,
    scenario,
    problem,
    topoSize,
    agent,
    model,
    tag,
    numeric: numericFilters,
    columns,
    sortKey,
    sortDir,
  });

  const applyView = (view: SavedView) => {
    setStatus(view.status);
    setTrial(view.trial);
    setScenario(view.scenario);
    setProblem(view.problem);
    setTopoSize(view.topoSize);
    setAgent(view.agent);
    setModel(view.model);
    setTag(view.tag);
    setNumericFilters(view.numeric);
    setColumns(view.columns);
    saveColumns(view.columns);
    setSortKey(view.sortKey);
    setSortDir(view.sortKey ? view.sortDir : "desc");
  };

  const saveCurrentView = () => {
    const name = window.prompt("Save the current filters, columns, and sort as:", activeView)?.trim();
    if (!name) return;
    const next = [...savedViews.filter((v) => v.name !== name), currentView(name)];
    next.sort((a, b) => a.name.localeCompare(b.name));
    setSavedViews(next);
    storeSavedViews(next);
    setActiveView(name);
  };

  const deleteActiveView = () => {
    if (!activeView) return;
    const next = savedViews.filter((v) => v.name !== activeView);
    setSavedViews(next);
    storeSavedViews(next);
    setActiveView("");
  };

  const toggleColumn = (id: SessionSortKey) => {
    setColumns((prev) => {
      const next = prev.includes(id) ? prev.filter((c) => c !== id) : [...prev, id];
      saveColumns(next);
      return next;
    });
  };

  const onSort = (key: SessionSortKey) => {
    const current = sortKey ?? defaultSortKey;
    if (current === key) {
      setSortKey(key);
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
      return;
    }
    setSortKey(key);
    setSortDir(DESC_FIRST_SORT_KEYS.has(key) ? "desc" : "asc");
  };

  const toggleExpanded = (id: string, e: ReactMouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      saveTreeExpanded(next);
      return next;
    });
  };

  const selectNode = (node: PathTreeNode) => {
    setSelectedPath(node.path);
    onClearSession();
    setExpanded((prev) => {
      const next = new Set(prev);
      const parts = node.path.split("/").filter(Boolean);
      let acc = "";
      for (const part of parts) {
        acc = acc ? `${acc}/${part}` : part;
        next.add(acc);
      }
      saveTreeExpanded(next);
      return next;
    });
  };

  const showTrialCol =
    (facets.trial_indices?.length ?? 0) > 0 ||
    visibleSessions.some((s) => s.trial_index != null);
  // Newest first until the user picks a column.
  const defaultSortKey: SessionSortKey = "time";

  const renderNode = (node: PathTreeNode, depth: number): ReactNode => {
    const hasKids = node.children.length > 0;
    const open = expanded.has(node.path);
    const selected = selectedPath === node.path;
    return (
      <div key={node.path}>
        <div
          className={`tree-row${selected ? " active" : ""}`}
          style={{ paddingLeft: 8 + depth * 14 }}
        >
          {hasKids ? (
            <button
              type="button"
              className="tree-chevron"
              aria-label={open ? "Collapse" : "Expand"}
              onClick={(e) => toggleExpanded(node.path, e)}
            >
              {open ? "▾" : "▸"}
            </button>
          ) : (
            <span className="tree-chevron spacer" />
          )}
          <button
            type="button"
            className="tree-main"
            title={node.path}
            onClick={() => selectNode(node)}
          >
            <span className="tree-folder-icon">
              <FolderGlyph />
            </span>
            <span className="tree-label">{node.name}</span>
            <span className="tree-meta">{node.count}</span>
          </button>
        </div>
        {hasKids && open
          ? node.children.map((child) => renderNode(child, depth + 1))
          : null}
      </div>
    );
  };

  return (
    <div
      ref={shellRef}
      className="app-shell"
      style={{ "--sidebar-width": `${sidebarWidth}px` } as CSSProperties}
    >
      <aside className="sidebar">
        <div className="sidebar-list">
          {error && <div className="sidebar-empty">{error}</div>}
          {!error && loading && <div className="sidebar-empty">Loading…</div>}
          {!error && !loading && sessions.length === 0 && (
            <div className="sidebar-empty">
              No sessions under <code>{resultsRoot || "results/"}</code>
            </div>
          )}
          {!error && !loading && tree.map((node) => renderNode(node, 0))}
        </div>
      </aside>
      <div
        className="sidebar-resizer"
        role="separator"
        aria-orientation="vertical"
        aria-valuenow={Math.round(sidebarWidth)}
        aria-valuemin={SIDEBAR_WIDTH_MIN}
        aria-valuemax={SIDEBAR_WIDTH_MAX}
        aria-label="Resize folder tree"
        onMouseDown={(e) => {
          e.preventDefault();
          sidebarDragging.current = true;
          document.body.classList.add("is-resizing");
        }}
      />
      <div className="app-main">
        {sessionId ? (
          <SessionView
            key={sessionId}
            sessionId={sessionId}
            root={root}
            initialNav={sessionNav}
            knownTags={facets.tags ?? []}
            onNavChange={onSessionNavChange}
            onBack={onClearSession}
          />
        ) : (
          <div className="main pane-list">
            {showMonitor && (
              <RunMonitor
                progress={activeProgress}
                sessions={monitorSessions}
                root={root}
                listFilter={listFilter}
                onListFilter={onMonitorListFilter}
              />
            )}
            <div className="home-toolbar">
              <div className="home-breadcrumb" title={breadcrumb}>
                {breadcrumb || "Select a folder"}
              </div>
              {listFilter && (
                <button
                  type="button"
                  className="home-filter-chip"
                  title="Clear monitor filter"
                  onClick={() => setListFilter(null)}
                >
                  Filter: {monitorFilterLabel(listFilter)}
                  <span aria-hidden> ×</span>
                </button>
              )}
              <input
                className="home-search"
                type="search"
                placeholder="Search sessions…"
                value={q}
                onChange={(e) => setQ(e.target.value)}
              />
              <input
                className="home-search"
                type="search"
                placeholder="Search trajectories…"
                title="Full-text search over agent messages, tool calls, and NIKA events"
                value={content}
                onChange={(e) => setContent(e.target.value)}
              />
            </div>
            <div className="home-view-bar">
              <details className="col-picker">
                <summary>Columns</summary>
                <div className="col-picker-menu">
                  {SESSION_COLUMNS.map((col) => (
                    <label key={col.id}>
                      <input
                        type="checkbox"
                        checked={columns.includes(col.id)}
                        onChange={() => toggleColumn(col.id)}
                      />
                      {col.label}
                    </label>
                  ))}
                </div>
              </details>
              <select
                className="view-select"
                aria-label="Saved views"
                value={activeView}
                onChange={(e) => {
                  const view = savedViews.find((v) => v.name === e.target.value);
                  setActiveView(e.target.value);
                  if (view) applyView(view);
                }}
              >
                <option value="">Views…</option>
                {savedViews.map((v) => (
                  <option key={v.name} value={v.name}>
                    {v.name}
                  </option>
                ))}
              </select>
              <button type="button" className="view-btn" onClick={saveCurrentView}>
                Save view
              </button>
              {activeView && (
                <button type="button" className="view-btn" onClick={deleteActiveView}>
                  Delete view
                </button>
              )}
            </div>
            {loading ? (
              <div className="empty">Loading sessions…</div>
            ) : selectedPath == null ? (
              <div className="empty">Select a folder on the left</div>
            ) : tableSessions.length === 0 && !columnFiltersActive ? (
              <div className="empty">
                {listFilter
                  ? `No sessions match ${monitorFilterLabel(listFilter)}`
                  : "No sessions in this folder"}
              </div>
            ) : (
              <SessionsTable
                sessions={tableSessions}
                columns={columns}
                sortKey={sortKey ?? defaultSortKey}
                sortDir={sortDir}
                onSort={onSort}
                contentHits={debouncedContent ? contentHits : undefined}
                onOpen={(id) => onOpenSession(id, debouncedContent || undefined)}
                onDelete={async (s) => {
                  const id = sessionOpenId(s);
                  try {
                    await deleteSession(id, root);
                    listGenRef.current += 1;
                    setSessions((prev) =>
                      prev.filter((x) => sessionOpenId(x) !== id),
                    );
                  } catch (err) {
                    window.alert(
                      err instanceof Error ? err.message : String(err),
                    );
                  }
                }}
                showTrial={showTrialCol}
                columnFilters={columnFilters}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function roleBadgeLabel(row: DisplayEvent): string {
  return roleLabel(row.role);
}

function nameBadgeLabel(row: DisplayEvent): string {
  if (isToolDisplay(row)) return row.title || "tool";
  if (row.role === "nika") return row.event?.replaceAll("_", " ") || row.title;
  if (row.role === "assistant") {
    if (row.event === "llm" || row.kind === "llm") {
      return row.attempt != null ? `llm attempt #${row.attempt}` : row.title;
    }
    return row.event || row.title;
  }
  // System / other: prefer human title (e.g. "warning") over raw event
  // names like "item.completed".
  return row.title || row.event || "—";
}

const OVERVIEW_LAYOUT_KEY = "nika-inspect-overview-layout";

function loadOverviewLayout(): OverviewLayoutMode {
  try {
    const raw = localStorage.getItem(OVERVIEW_LAYOUT_KEY);
    if (raw === "equal" || raw === "duration" || raw === "actual") return raw;
  } catch {
    /* ignore */
  }
  return "equal";
}

function overviewSpanTitle(span: {
  label: string;
  startMs: number;
  endMs: number;
}): string {
  const dur = Math.max(0, span.endMs - span.startMs);
  return `${span.label} · ${formatOverviewClock(span.startMs)} – ${formatOverviewClock(span.endMs)} · ${formatDuration(dur)}`;
}

/** Bars in the same lane whose visual slots overlap on x. */
function overlappingGroup(
  target: LaidOutOverviewSpan,
  laneItems: LaidOutOverviewSpan[],
): LaidOutOverviewSpan[] {
  const tL = target.leftPct;
  const tR = tL + Math.max(target.widthPct, 0);
  return laneItems
    .filter((it) => {
      const l = it.leftPct;
      const r = l + Math.max(it.widthPct, 0);
      return l < tR && r > tL;
    })
    .sort(
      (a, b) =>
        a.leftPct - b.leftPct ||
        a.widthPct - b.widthPct ||
        a.span.id.localeCompare(b.span.id),
    );
}

type OverlapPopup = {
  items: LaidOutOverviewSpan[];
  /** Fixed position near the click. */
  left: number;
  top: number;
};

/** Drag threshold before a press becomes a brush / pan (px). */
const OVERVIEW_DRAG_PX = 3;
/** Smallest zoom window: sequence units in equal mode, ms otherwise. */
const OVERVIEW_MIN_ZOOM_UNITS = 4;
const OVERVIEW_MIN_ZOOM_MS = 20;
/** Edge auto-pan while brushing a zoomed track (Harness defaults). */
const OVERVIEW_EDGE_ZONE_FRACTION = 0.08;
const OVERVIEW_EDGE_STEP_FRACTION = 0.025;
const OVERVIEW_MAX_EDGE_PX = 32;
/** Equal-mode chip inset per side, in sequence units (scales with zoom). */
const OVERVIEW_EQUAL_INSET = 0.08;

/** Range in the active overview domain (sequence units or ms). */
type DomainRange = { start: number; end: number };

function orderedRange(a: number, b: number): DomainRange {
  return a <= b ? { start: a, end: b } : { start: b, end: a };
}

function clampNumber(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function OverviewTimeline({
  events,
  selectedId,
  onSelect,
  brush,
  onBrushChange,
}: {
  events: CanonicalTraceEvent[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  brush: TimeBrush | null;
  onBrushChange: (brush: TimeBrush | null) => void;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const plotRef = useRef<HTMLDivElement>(null);
  const wheelCleanupRef = useRef<(() => void) | null>(null);
  const dragRef = useRef<{
    pointerId: number;
    anchorX: number;
    originX: number;
    originY: number;
    moved: boolean;
  } | null>(null);
  const panRef = useRef<{
    pointerId: number;
    anchorClientX: number;
    anchorStart: number;
    moved: boolean;
    pannable: boolean;
  } | null>(null);
  /** In-progress brush, in domain coordinates (survives edge auto-pan). */
  const [draft, setDraft] = useState<DomainRange | null>(null);
  /** Last committed brush with the exact domain range the user dragged. */
  const [committed, setCommitted] = useState<{
    range: DomainRange;
    brush: TimeBrush;
  } | null>(null);
  const [viewport, setViewport] = useState<DomainRange | null>(null);
  const [panning, setPanning] = useState(false);
  const [layoutMode, setLayoutMode] = useState<OverviewLayoutMode>(loadOverviewLayout);
  const [overlapPop, setOverlapPop] = useState<OverlapPopup | null>(null);

  const setLayout = (mode: OverviewLayoutMode) => {
    setLayoutMode(mode);
    setViewport(null);
    setDraft(null);
    setCommitted(null);
    setOverlapPop(null);
    try {
      localStorage.setItem(OVERVIEW_LAYOUT_KEY, mode);
    } catch {
      /* ignore */
    }
  };

  const spans = useMemo(() => buildOverviewSpans(events), [events]);

  // One domain per mode; zoom/pan/brush never switch the projection.
  const domain = useMemo(
    () => projectOverviewDomain(spans, layoutMode),
    [spans, layoutMode],
  );

  // Stack rows on the full domain so overlap layers stay put while zooming.
  const stackedFull = useMemo(() => {
    if (!domain) return [];
    const fullLen = domain.end - domain.start;
    return assignLaneStackRows(
      domain.items.map((it) => ({
        ...it,
        leftPct: ((it.x0 - domain.start) / fullLen) * 100,
        widthPct: ((it.x1 - it.x0) / fullLen) * 100,
      })),
    );
  }, [domain]);

  // Drop a stale viewport when the session domain changes.
  useEffect(() => {
    if (!domain || !viewport) return;
    if (viewport.end < domain.start || viewport.start > domain.end) {
      setViewport(null);
    }
  }, [domain, viewport]);

  const viewDomain = useMemo((): DomainRange | null => {
    if (!domain) return null;
    if (!viewport) return { start: domain.start, end: domain.end };
    const fullLen = domain.end - domain.start;
    const len = Math.min(fullLen, Math.max(1e-6, viewport.end - viewport.start));
    const start = clampNumber(viewport.start, domain.start, domain.end - len);
    return { start, end: start + len };
  }, [domain, viewport]);

  // Keep the selected event on screen while zoomed (Harness-style follow).
  useEffect(() => {
    if (!domain || !selectedId) return;
    const hit = domain.items.find((it) => it.span.eventId === selectedId);
    if (!hit) return;
    setViewport((current) => {
      if (!current) return current;
      if (hit.x1 > current.start && hit.x0 < current.end) return current;
      const len = current.end - current.start;
      const desired = hit.x1 <= current.start ? hit.x0 : hit.x1 - len;
      const start = clampNumber(desired, domain.start, domain.end - len);
      return start === current.start ? current : { start, end: start + len };
    });
  }, [domain, selectedId]);

  const laidOut = useMemo(() => {
    if (!viewDomain) return [];
    const viewLen = viewDomain.end - viewDomain.start;
    const inset = layoutMode === "equal" ? OVERVIEW_EQUAL_INSET : 0;
    return stackedFull
      .filter((it) => it.x1 >= viewDomain.start && it.x0 <= viewDomain.end)
      .map((it) => {
        const x0 = it.x0 + inset;
        const x1 = Math.max(x0, it.x1 - inset);
        return {
          ...it,
          leftPct: ((x0 - viewDomain.start) / viewLen) * 100,
          widthPct: ((x1 - x0) / viewLen) * 100,
        };
      });
  }, [stackedFull, viewDomain, layoutMode]);

  const laneHeights = useMemo(() => {
    // Fixed compact lanes — overlaps stay stacked with a slight height nudge.
    return LANES.map(() => overviewLaneHeightPx());
  }, []);

  const turnBoundaries = useMemo(
    () => overviewTurnBoundaryPcts(laidOut).filter((p) => p > 0 && p < 100),
    [laidOut],
  );

  const domainRef = useRef(domain);
  const viewDomainRef = useRef(viewDomain);
  const layoutModeRef = useRef(layoutMode);
  domainRef.current = domain;
  viewDomainRef.current = viewDomain;
  layoutModeRef.current = layoutMode;

  const onWheelZoom = useCallback((event: WheelEvent) => {
    event.preventDefault();
    event.stopPropagation();
    const track = trackRef.current;
    const d = domainRef.current;
    const visible = viewDomainRef.current;
    if (!track || !d || !visible) return;
    const rect = track.getBoundingClientRect();
    if (rect.width <= 0) return;
    // Normalize wheel/trackpad deltas (pixels / lines / pages).
    const unit =
      event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? rect.width : 1;
    const dx = event.deltaX * unit;
    const dy = event.deltaY * unit;
    const fullLen = d.end - d.start;
    const viewLen = visible.end - visible.start;
    let next: DomainRange | null;
    if (Math.abs(dx) > Math.abs(dy) || event.shiftKey) {
      // Horizontal wheel / shift+wheel pans a zoomed viewport.
      if (viewLen >= fullLen) return;
      const px = Math.abs(dx) > Math.abs(dy) ? dx : dy;
      const start = clampNumber(
        visible.start + (px / rect.width) * viewLen,
        d.start,
        d.end - viewLen,
      );
      next = { start, end: start + viewLen };
    } else {
      const minLen = Math.min(
        fullLen,
        layoutModeRef.current === "equal"
          ? OVERVIEW_MIN_ZOOM_UNITS
          : OVERVIEW_MIN_ZOOM_MS,
      );
      const step = clampNumber(dy, -300, 300) * 0.0015;
      const nextLen = clampNumber(viewLen * Math.exp(step), minLen, fullLen);
      if (nextLen >= fullLen * 0.999) {
        next = null;
      } else {
        const anchor = clampNumber((event.clientX - rect.left) / rect.width, 0, 1);
        const anchorX = visible.start + anchor * viewLen;
        const start = clampNumber(
          anchorX - anchor * nextLen,
          d.start,
          d.end - nextLen,
        );
        next = { start, end: start + nextLen };
      }
    }
    // Several wheel events can land before React re-renders; chain from the
    // latest viewport instead of the last rendered one.
    viewDomainRef.current = next ?? { start: d.start, end: d.end };
    setViewport(next);
  }, []);

  // Attach non-passive wheel on the track node itself (callback ref survives
  // domain object churn from polling).
  const setTrackNode = useCallback(
    (node: HTMLDivElement | null) => {
      wheelCleanupRef.current?.();
      wheelCleanupRef.current = null;
      trackRef.current = node;
      if (!node) return;
      node.addEventListener("wheel", onWheelZoom, { passive: false });
      wheelCleanupRef.current = () => {
        node.removeEventListener("wheel", onWheelZoom);
      };
    },
    [onWheelZoom],
  );

  useLayoutEffect(() => () => {
    wheelCleanupRef.current?.();
    wheelCleanupRef.current = null;
  }, []);

  const clearBrush = useCallback(() => {
    setDraft(null);
    setCommitted(null);
    onBrushChange(null);
  }, [onBrushChange]);

  // Escape closes overlap zoom, then clears the brush (Harness-style).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented) return;
      // Escape inside a field (path input, search) belongs to that field.
      const target = e.target as HTMLElement | null;
      if (target?.closest("input, textarea, select, [contenteditable]")) return;
      if (overlapPop) {
        setOverlapPop(null);
        return;
      }
      clearBrush();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [clearBrush, overlapPop]);

  const openOverlapPopup = (
    hit: LaidOutOverviewSpan,
    clientX: number,
    clientY: number,
  ) => {
    const laneItems = laidOut.filter((it) => it.span.lane === hit.span.lane);
    const group = overlappingGroup(hit, laneItems);
    // Same bar again clears selection (and any overlap picker).
    if (selectedId === hit.span.eventId) {
      onSelect(null);
      setOverlapPop(null);
      return;
    }
    onSelect(hit.span.eventId);
    if (group.length <= 1) {
      setOverlapPop(null);
      return;
    }
    const plot = plotRef.current;
    const rect = plot?.getBoundingClientRect();
    const left = rect
      ? Math.min(Math.max(8, clientX - rect.left - 20), Math.max(8, rect.width - 320))
      : clientX;
    const top = rect
      ? Math.min(Math.max(8, clientY - rect.top + 12), Math.max(8, rect.height - 40))
      : clientY;
    setOverlapPop({ items: group, left, top });
  };

  /** Pointer x as a 0..1 fraction of the track width. */
  const fractionAtClientX = (clientX: number): number | null => {
    const el = trackRef.current;
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) return null;
    return clampNumber((clientX - rect.left) / rect.width, 0, 1);
  };

  /** Prefer the bar under the cursor (lane + vertical stack), not the globally shortest. */
  const hitTestBar = (clientX: number, clientY: number) => {
    const el = trackRef.current;
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) return null;
    const pct = clampNumber(((clientX - rect.left) / rect.width) * 100, 0, 100);
    // Bars keep a 2px minimum on screen; widen the hit box to match.
    const minPct = (2 / rect.width) * 100;
    const y = clientY - rect.top;
    const gap = 4;
    let yCursor = 0;
    let laneIndex = -1;
    for (let i = 0; i < LANES.length; i++) {
      const h = laneHeights[i] ?? 14;
      if (y >= yCursor && y < yCursor + h) {
        laneIndex = i;
        break;
      }
      yCursor += h + gap;
    }
    if (laneIndex < 0) return null;
    const laneId = LANES[laneIndex].id;
    const laneH = laneHeights[laneIndex] ?? 14;
    const localY = y - yCursor;
    const laneItems = laidOut.filter((it) => it.span.lane === laneId);
    const layerCount =
      laneItems.reduce((m, x) => Math.max(m, x.stackRow), 0) + 1;
    const xHits = laneItems.filter(
      (it) =>
        pct >= it.leftPct && pct <= it.leftPct + Math.max(it.widthPct, minPct),
    );
    if (xHits.length === 0) return null;
    const scored = xHits.map((it) => {
      const g = overviewStackBarGeometry(it.stackRow, laneH, layerCount);
      const contains = localY >= g.top && localY <= g.top + g.height;
      const dist = contains
        ? 0
        : Math.min(
            Math.abs(localY - g.top),
            Math.abs(localY - (g.top + g.height)),
          );
      return { it, contains, dist, stackRow: it.stackRow, widthPct: it.widthPct };
    });
    scored.sort(
      (a, b) =>
        Number(b.contains) - Number(a.contains) ||
        a.dist - b.dist ||
        b.stackRow - a.stackRow ||
        a.widthPct - b.widthPct,
    );
    return scored[0]?.it ?? null;
  };

  /** Commit a domain range as a wall-clock brush over the spans it touches. */
  const commitRange = (range: DomainRange) => {
    if (!domain) return;
    const hits = domain.items.filter(
      (it) => it.x0 <= range.end && it.x1 >= range.start,
    );
    if (hits.length === 0) {
      clearBrush();
      return;
    }
    const next: TimeBrush = {
      startMs: Math.min(...hits.map((h) => h.span.startMs)),
      endMs: Math.max(...hits.map((h) => h.span.endMs)),
    };
    setCommitted({ range, brush: next });
    onBrushChange(next);
  };

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!viewDomain || !domain) return;
    setOverlapPop(null);

    if (e.button === 2) {
      panRef.current = {
        pointerId: e.pointerId,
        anchorClientX: e.clientX,
        anchorStart: viewDomain.start,
        moved: false,
        pannable: viewport != null,
      };
      setPanning(viewport != null);
      e.currentTarget.setPointerCapture(e.pointerId);
      return;
    }
    if (e.button !== 0) return;

    const f = fractionAtClientX(e.clientX);
    if (f == null) return;
    dragRef.current = {
      pointerId: e.pointerId,
      anchorX: viewDomain.start + f * (viewDomain.end - viewDomain.start),
      originX: e.clientX,
      originY: e.clientY,
      moved: false,
    };
    setDraft(null);
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!viewDomain || !domain) return;
    const rect = e.currentTarget.getBoundingClientRect();
    if (rect.width <= 0) return;
    const viewLen = viewDomain.end - viewDomain.start;

    const pan = panRef.current;
    if (pan && pan.pointerId === e.pointerId) {
      if (Math.abs(e.clientX - pan.anchorClientX) >= OVERVIEW_DRAG_PX) {
        pan.moved = true;
      }
      if (!pan.pannable) return;
      const delta = (e.clientX - pan.anchorClientX) / rect.width;
      const start = clampNumber(
        pan.anchorStart - delta * viewLen,
        domain.start,
        domain.end - viewLen,
      );
      setViewport({ start, end: start + viewLen });
      return;
    }

    const drag = dragRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    if (!drag.moved && Math.abs(e.clientX - drag.originX) < OVERVIEW_DRAG_PX) {
      return;
    }
    drag.moved = true;
    // Auto-pan when brushing into the edges of a zoomed track.
    let viewStart = viewDomain.start;
    if (viewport) {
      const localX = e.clientX - rect.left;
      const edge = Math.min(
        OVERVIEW_MAX_EDGE_PX,
        Math.max(1, rect.width * OVERVIEW_EDGE_ZONE_FRACTION),
      );
      const direction = localX < edge ? -1 : localX > rect.width - edge ? 1 : 0;
      if (direction !== 0) {
        const depth =
          direction < 0 ? edge - localX : localX - (rect.width - edge);
        const strength = Math.max(0.2, clampNumber(depth / edge, 0, 1));
        viewStart = clampNumber(
          viewStart + direction * viewLen * OVERVIEW_EDGE_STEP_FRACTION * strength,
          domain.start,
          domain.end - viewLen,
        );
        if (viewStart !== viewDomain.start) {
          setViewport({ start: viewStart, end: viewStart + viewLen });
        }
      }
    }
    const f = clampNumber((e.clientX - rect.left) / rect.width, 0, 1);
    setDraft(orderedRange(drag.anchorX, viewStart + f * viewLen));
  };

  const onPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const pan = panRef.current;
    if (pan && pan.pointerId === e.pointerId) {
      const moved =
        pan.moved || Math.abs(e.clientX - pan.anchorClientX) >= OVERVIEW_DRAG_PX;
      panRef.current = null;
      setPanning(false);
      try {
        e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        /* already released */
      }
      if (!moved) clearBrush();
      return;
    }

    const drag = dragRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    dragRef.current = null;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* already released */
    }

    if (drag.moved) {
      const range = draft;
      setDraft(null);
      if (range) commitRange(range);
      return;
    }

    setDraft(null);
    // Click without drag: lane + vertical stack aware (so long underlays stay selectable).
    const hit = hitTestBar(drag.originX, drag.originY);
    if (hit) openOverlapPopup(hit, drag.originX, drag.originY);
    else onSelect(null);
  };

  const onContextMenu = (e: ReactMouseEvent) => {
    e.preventDefault();
  };

  if (!viewDomain || !domain) {
    return <div className="overview empty-inline">No timed events</div>;
  }

  const byLane = (lane: OverviewLane) =>
    laidOut.filter(({ span }) => span.lane === lane);

  // Visible brush in domain coordinates: live draft, the range the user
  // dragged, or (after a mode switch) the envelope of spans in the ms brush.
  let shownRange: DomainRange | null = draft;
  if (!shownRange && brush) {
    if (committed && committed.brush === brush) {
      shownRange = committed.range;
    } else {
      const hits = domain.items.filter((it) =>
        overlapsBrush(it.span.startMs, it.span.endMs, brush),
      );
      if (hits.length > 0) {
        shownRange = {
          start: Math.min(...hits.map((h) => h.x0)),
          end: Math.max(...hits.map((h) => h.x1)),
        };
      }
    }
  }
  const viewLen = viewDomain.end - viewDomain.start;
  const brushLeftPct = shownRange
    ? clampNumber(((shownRange.start - viewDomain.start) / viewLen) * 100, 0, 100)
    : null;
  const brushRightPct = shownRange
    ? clampNumber(((shownRange.end - viewDomain.start) / viewLen) * 100, 0, 100)
    : null;
  const brushStyle: CSSProperties | undefined =
    brushLeftPct != null && brushRightPct != null && brushRightPct > brushLeftPct
      ? { left: `${brushLeftPct}%`, width: `${brushRightPct - brushLeftPct}%` }
      : undefined;

  const zoomed = viewport != null;
  const timedPaint = layoutMode !== "equal";

  return (
    <div className="overview">
      <div className="overview-toolbar">
        <span className="overview-hint">
          Scroll to zoom · drag to select · click overlap to zoom · Esc to clear
          {zoomed ? " · right-drag or shift+scroll to pan" : ""}
        </span>
        <div className="overview-mode-group" role="group" aria-label="Overview layout">
          {(
            [
              ["equal", "Equal", "Equal-width bars in chronological order"],
              [
                "duration",
                "Duration",
                "Real durations with idle gaps compressed (Harness-style)",
              ],
              ["actual", "Wall clock", "Real durations on the full wall-clock axis"],
            ] as const
          ).map(([id, label, tip]) => (
            <button
              key={id}
              type="button"
              className={`overview-duration-toggle${layoutMode === id ? " active" : ""}`}
              aria-pressed={layoutMode === id}
              title={tip}
              onClick={() => setLayout(id)}
            >
              {label}
            </button>
          ))}
        </div>
        {zoomed && (
          <button
            type="button"
            className="overview-reset-zoom"
            onClick={() => setViewport(null)}
          >
            Reset zoom
          </button>
        )}
      </div>
      <div className="overview-plot" ref={plotRef}>
        <div
          className="overview-labels"
          style={{ gridTemplateRows: laneHeights.map((h) => `${h}px`).join(" ") }}
        >
          {LANES.map((l) => (
            <div key={l.id} className="overview-label">
              {l.label}
            </div>
          ))}
        </div>
        <div
          ref={setTrackNode}
          className={`overview-track${draft ? " brushing" : ""}${panning ? " panning" : ""}${timedPaint ? " duration-mode" : ""}`}
          style={{ gridTemplateRows: laneHeights.map((h) => `${h}px`).join(" ") }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={() => {
            dragRef.current = null;
            panRef.current = null;
            setDraft(null);
            setPanning(false);
          }}
          onContextMenu={onContextMenu}
          onDoubleClick={(e) => {
            e.preventDefault();
            setViewport(null);
            clearBrush();
            setOverlapPop(null);
          }}
        >
          {turnBoundaries.map((pct) => (
            <div
              key={`turn-${pct}`}
              className="overview-turn-boundary"
              style={{ left: `${pct}%` }}
            />
          ))}
          {LANES.map((l, laneIndex) => {
            const laneH = laneHeights[laneIndex] ?? 16;
            const laneItems = byLane(l.id);
            const layerCount =
              laneItems.reduce((m, x) => Math.max(m, x.stackRow), 0) + 1;
            return (
              <div
                key={l.id}
                className="overview-lane"
                style={{ height: laneH }}
              >
                {laneItems.map(({ span, x0, x1, leftPct, widthPct, stackRow }) => {
                  const dimmedByBrush =
                    shownRange != null &&
                    !(x0 <= shownRange.end && x1 >= shownRange.start);
                  const dimmedBySelect =
                    selectedId != null && span.eventId !== selectedId;
                  const dimmed = dimmedBySelect || dimmedByBrush;
                  const { top, height, depth } = overviewStackBarGeometry(
                    stackRow,
                    laneH,
                    layerCount,
                  );
                  return (
                    <div
                      key={span.id}
                      title={overviewSpanTitle(span)}
                      className={[
                        "overview-span",
                        `role-${span.role}`,
                        span.error ? "error" : "",
                        selectedId === span.eventId ? "selected" : "",
                        dimmed ? "dimmed" : "",
                        depth > 0 ? "overlap" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      style={{
                        left: `${leftPct}%`,
                        width: `${Math.max(widthPct, 0)}%`,
                        top,
                        height,
                        minWidth: "2px",
                        ["--stack-depth" as string]: depth,
                        zIndex:
                          selectedId === span.eventId
                            ? 30
                            : 5 + stackRow,
                      }}
                    />
                  );
                })}
              </div>
            );
          })}
          {brushStyle && <div className="overview-brush" style={brushStyle} />}
        </div>
        {overlapPop && (
          <div
            className="overview-overlap-pop"
            style={{ left: overlapPop.left, top: overlapPop.top }}
            onPointerDown={(e) => e.stopPropagation()}
            role="dialog"
            aria-label="Overlapping events"
          >
            <div className="overview-overlap-pop-head">
              <span>{overlapPop.items.length} overlapping</span>
              <button
                type="button"
                className="overview-overlap-pop-close"
                onClick={() => setOverlapPop(null)}
                aria-label="Close"
              >
                ×
              </button>
            </div>
            <div
              className="overview-overlap-pop-track"
              style={{
                height: Math.max(48, overlapPop.items.length * 22 + 12),
              }}
            >
              {(() => {
                const clusterLeft = Math.min(
                  ...overlapPop.items.map((it) => it.leftPct),
                );
                const clusterRight = Math.max(
                  ...overlapPop.items.map(
                    (it) => it.leftPct + Math.max(it.widthPct, 0),
                  ),
                );
                const clusterW = Math.max(clusterRight - clusterLeft, 0.5);
                return overlapPop.items.map((it, i) => {
                  const left =
                    ((it.leftPct - clusterLeft) / clusterW) * 100;
                  const width = (Math.max(it.widthPct, 0) / clusterW) * 100;
                  return (
                    <button
                      key={it.span.id}
                      type="button"
                      title={overviewSpanTitle(it.span)}
                      className={[
                        "overview-overlap-pop-bar",
                        `role-${it.span.role}`,
                        it.span.error ? "error" : "",
                        selectedId === it.span.eventId ? "selected" : "",
                      ]
                        .filter(Boolean)
                        .join(" ")}
                      style={{
                        left: `${left}%`,
                        width: `${Math.max(width, 2)}%`,
                        top: 6 + i * 20,
                      }}
                      onClick={() => {
                        onSelect(
                          selectedId === it.span.eventId
                            ? null
                            : it.span.eventId,
                        );
                      }}
                    >
                      <span className="overview-overlap-pop-label">
                        {it.span.label}
                      </span>
                    </button>
                  );
                });
              })()}
            </div>
            <ul className="overview-overlap-pop-list">
              {overlapPop.items.map((it) => (
                <li key={`row-${it.span.id}`}>
                  <button
                    type="button"
                    className={
                      selectedId === it.span.eventId ? "selected" : undefined
                    }
                    onClick={() =>
                      onSelect(
                        selectedId === it.span.eventId
                          ? null
                          : it.span.eventId,
                      )
                    }
                  >
                    <span className={`dot role-${it.span.role}`} aria-hidden />
                    <span className="name">{it.span.label}</span>
                    <span className="dur">
                      {formatDuration(
                        Math.max(0, it.span.endMs - it.span.startMs),
                      )}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}

/** Wall clock that ticks once a second only while ``active`` (live spans). */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active]);
  return now;
}

/** Duration cell; only running spans re-render every second. */
function LiveDuration({ row }: { row: DisplayEvent }) {
  const now = useNow(isRunningSpan(row));
  return <>{formatDuration(displayDurationMs(row, now))}</>;
}

function Ledger({
  rows,
  selectedId,
  matchedIds,
  commentCounts,
  onSelect,
}: {
  rows: DisplayEvent[];
  selectedId: string | null;
  /** Rows hit by the trajectory find box. */
  matchedIds: Set<string>;
  commentCounts: Map<string, number>;
  onSelect: (row: DisplayEvent) => void;
}) {
  const rowRefs = useRef<Map<string, HTMLTableRowElement>>(new Map());
  const hasRows = rows.length > 0;

  useEffect(() => {
    if (!selectedId) return;
    rowRefs.current.get(selectedId)?.scrollIntoView({
      block: "nearest",
      behavior: "smooth",
    });
  }, [selectedId, hasRows]);

  if (rows.length === 0) {
    return <div className="empty">No events</div>;
  }

  return (
    <div className="ledger">
      <table className="traj-table">
        <thead>
          <tr>
            <th className="col-event">Role</th>
            <th className="col-name">Name</th>
            <th className="col-time">Time</th>
            <th className="col-dur">Dur</th>
            <th className="col-content">Content</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const selected = selectedId === row.id;
            return (
              <tr
                key={row.id}
                ref={(el) => {
                  if (el) rowRefs.current.set(row.id, el);
                  else rowRefs.current.delete(row.id);
                }}
                className={[
                  selected ? "selected" : "",
                  `role-${row.role}`,
                  row.error ? "error" : "",
                  matchedIds.has(row.id) ? "find-hit" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
                onClick={() => onSelect(row)}
              >
                <td className="event-cell">
                  <span className={`turn-rail role-${row.role}`} aria-hidden />
                  {selected && <span className="selection-rail" aria-hidden />}
                  <span className={`rail-dot role-${row.role}`} aria-hidden />
                  <span className={`kind-tag role-${row.role}`}>
                    <span className="kind-tag-label">{roleBadgeLabel(row)}</span>
                  </span>
                </td>
                <td className="name-cell">
                  <span className="name-label" title={nameBadgeLabel(row)}>
                    {nameBadgeLabel(row)}
                  </span>
                  {commentCounts.has(row.id) && (
                    <span
                      className="comment-mark"
                      title={`${commentCounts.get(row.id)} comment(s)`}
                    >
                      ✎{commentCounts.get(row.id)}
                    </span>
                  )}
                </td>
                <td className="time-cell">{formatTs(row.timestamp)}</td>
                <td className="dur-cell">
                  <LiveDuration row={row} />
                </td>
                <td className="content-cell">
                  <div className="content-inner">
                    {row.summary ? (
                      <span className="content-summary">{row.summary}</span>
                    ) : (
                      <span className="content-summary muted">—</span>
                    )}
                  </div>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function unescapeNewlines(text: string): string {
  return text
    .replace(/\\\\/g, "\u0000")
    .replace(/\\r\\n/g, "\n")
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "\t")
    .replace(/\\"/g, '"')
    .replace(/\\'/g, "'")
    .replace(/\u0000/g, "\\");
}

/** Cap recursive unwrap / pretty-print so provider dumps cannot freeze the UI. */
const DISPLAY_STR_LIMIT = 12_000;
const DISPLAY_PRETTY_LIMIT = 48_000;
const DISPLAY_ARRAY_LIMIT = 60;

function clipDisplayString(text: string, limit = DISPLAY_STR_LIMIT): string {
  if (text.length <= limit) return text;
  return `${text.slice(0, limit)}… [${text.length} chars total]`;
}

/** Unwrap LangChain tool blobs and nested JSON into a display value. */
function normalizeDisplayValue(value: unknown, depth = 0): unknown {
  if (value == null || depth > 6) return value;

  if (typeof value === "object") {
    if (Array.isArray(value)) {
      if (
        value.length === 1 &&
        value[0] &&
        typeof value[0] === "object" &&
        !Array.isArray(value[0]) &&
        typeof (value[0] as { text?: unknown }).text === "string"
      ) {
        return normalizeDisplayValue((value[0] as { text: string }).text, depth + 1);
      }
      const items = value.length > DISPLAY_ARRAY_LIMIT
        ? value.slice(0, DISPLAY_ARRAY_LIMIT)
        : value;
      const mapped = items.map((item) => normalizeDisplayValue(item, depth + 1));
      if (value.length > DISPLAY_ARRAY_LIMIT) {
        mapped.push(`… [${value.length - DISPLAY_ARRAY_LIMIT} more items]`);
      }
      return mapped;
    }
    const obj = value as Record<string, unknown>;
    if (typeof obj.content === "string" || Array.isArray(obj.content)) {
      const parsed = parseLlmMessageContent(obj);
      if (parsed.text.trim()) return normalizeDisplayValue(parsed.text, depth + 1);
    }
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(obj)) {
      out[k] = normalizeDisplayValue(v, depth + 1);
    }
    return out;
  }

  if (typeof value !== "string") return value;
  // Do not JSON-unwrap multi-MB provider error dumps that embed full transcripts.
  if (value.length > DISPLAY_STR_LIMIT) {
    return clipDisplayString(value);
  }
  const trimmed = value.trim();
  if (!trimmed) return value;

  if (trimmed.startsWith("content=")) {
    const parsed = parseLlmMessageContent(trimmed);
    if (parsed.text.trim() && parsed.text.trim() !== trimmed) {
      return normalizeDisplayValue(parsed.text, depth + 1);
    }
  }

  const asJson = tryParseJsonish(trimmed);
  if (asJson !== undefined) {
    return normalizeDisplayValue(asJson, depth + 1);
  }

  // Logged strings often keep literal \n sequences.
  if (trimmed.includes("\\n") || trimmed.includes("\\t")) {
    return unescapeNewlines(trimmed);
  }
  return value;
}

/** Pretty-print objects, JSON strings, and simple Python dict/list literals. */
function formatPretty(value: unknown): string {
  return formatNormalized(normalizeDisplayValue(value));
}

/** Format an already-normalized value (normalizing twice re-clips the text). */
function formatNormalized(normalized: unknown): string {
  if (normalized === null || normalized === undefined) return "—";
  if (typeof normalized === "number" || typeof normalized === "boolean") {
    return String(normalized);
  }
  if (typeof normalized === "string") {
    const text = normalized.trim() ? normalized : "—";
    return text === "—" ? text : clipDisplayString(text, DISPLAY_PRETTY_LIMIT);
  }
  try {
    return clipDisplayString(JSON.stringify(normalized, null, 2), DISPLAY_PRETTY_LIMIT);
  } catch {
    return clipDisplayString(String(normalized), DISPLAY_PRETTY_LIMIT);
  }
}

function tryParseJsonish(text: string): unknown | undefined {
  try {
    return JSON.parse(text);
  } catch {
    /* continue */
  }
  if (
    !(
      (text.startsWith("{") && text.endsWith("}")) ||
      (text.startsWith("[") && text.endsWith("]"))
    )
  ) {
    return undefined;
  }
  try {
    return JSON.parse(pythonLiteralToJson(text));
  } catch {
    return undefined;
  }
}

/** Convert a simple Python dict/list literal into JSON text. */
function pythonLiteralToJson(source: string): string {
  let out = "";
  let i = 0;
  let quote: "'" | '"' | null = null;
  let escape = false;

  while (i < source.length) {
    const ch = source[i]!;

    if (quote) {
      if (escape) {
        if (ch === "'" && quote === "'") out += "'";
        else if (ch === '"') out += '\\"';
        else out += `\\${ch}`;
        escape = false;
      } else if (ch === "\\") {
        escape = true;
      } else if (ch === quote) {
        out += '"';
        quote = null;
      } else if (ch === '"' && quote === "'") {
        out += '\\"';
      } else if (ch === "\n") {
        out += "\\n";
      } else if (ch === "\r") {
        out += "\\r";
      } else if (ch === "\t") {
        out += "\\t";
      } else {
        out += ch;
      }
      i += 1;
      continue;
    }

    if (ch === "'" || ch === '"') {
      quote = ch;
      out += '"';
      i += 1;
      continue;
    }

    if (source.startsWith("None", i) && isIdentBoundary(source, i, 4)) {
      out += "null";
      i += 4;
      continue;
    }
    if (source.startsWith("True", i) && isIdentBoundary(source, i, 4)) {
      out += "true";
      i += 4;
      continue;
    }
    if (source.startsWith("False", i) && isIdentBoundary(source, i, 5)) {
      out += "false";
      i += 5;
      continue;
    }

    out += ch;
    i += 1;
  }

  return out;
}

function isIdentBoundary(source: string, index: number, len: number): boolean {
  const before = index === 0 ? "" : source[index - 1]!;
  const after = source[index + len] ?? "";
  const edge = (c: string) => !c || !/[A-Za-z0-9_]/.test(c);
  return edge(before) && edge(after);
}

function highlightJson(text: string): ReactNode {
  // Regex tokenization on huge blobs freezes the inspector; plain text is enough.
  if (text.length > DISPLAY_PRETTY_LIMIT) {
    return clipDisplayString(text, DISPLAY_PRETTY_LIMIT);
  }
  const parts: ReactNode[] = [];
  const re =
    /("(?:\\.|[^"\\])*")\s*:|("(?:\\.|[^"\\])*")|(\btrue\b|\bfalse\b|\bnull\b)|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|([{}[\],])/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  while ((match = re.exec(text))) {
    if (match.index > last) {
      parts.push(text.slice(last, match.index));
    }
    if (match[1]) {
      parts.push(
        <span key={key++} className="json-key">
          {match[1]}
        </span>,
      );
      parts.push(": ");
    } else if (match[2]) {
      parts.push(
        <span key={key++} className="json-string">
          {match[2]}
        </span>,
      );
    } else if (match[3]) {
      parts.push(
        <span key={key++} className="json-literal">
          {match[3]}
        </span>,
      );
    } else if (match[4]) {
      parts.push(
        <span key={key++} className="json-number">
          {match[4]}
        </span>,
      );
    } else if (match[5]) {
      parts.push(
        <span key={key++} className="json-punct">
          {match[5]}
        </span>,
      );
    }
    last = match.index + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function shouldRenderHumanSections(value: unknown): value is Record<string, unknown> {
  if (!isPlainRecord(value)) return false;
  const entries = Object.entries(value);
  if (entries.length === 0) return false;
  // Only promote shell-style dumps (real newlines), not long single-line ids.
  const multiline = entries.filter(
    ([, v]) => typeof v === "string" && v.includes("\n"),
  ).length;
  return multiline >= 1 && entries.every(([, v]) => typeof v !== "object" || v == null);
}

/** Payload tab: full event as one pretty-printed JSON block (no field grid). */
function EventPayloadView({ raw }: { raw: Record<string, unknown> }) {
  const text = formatPretty(raw);
  const trimmed = text.trim();
  const looksJson =
    (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
    (trimmed.startsWith("[") && trimmed.endsWith("]"));
  return (
    <pre className={`pretty-block${looksJson ? " is-json" : ""}`}>
      {looksJson ? highlightJson(text) : text}
    </pre>
  );
}

function PrettyValue({ value }: { value: unknown }) {
  const normalized = normalizeDisplayValue(value);

  // Results / Parameters may still use section cards for multi-line dumps;
  // Payload tab never goes through this path.
  if (shouldRenderHumanSections(normalized)) {
    return (
      <div className="human-blocks">
        {Object.entries(normalized).map(([key, val]) => (
          <section key={key} className="human-block">
            <header className="human-block-head">{key}</header>
            <pre className="human-block-body">
              {val == null
                ? "—"
                : typeof val === "string"
                  ? val
                  : JSON.stringify(val, null, 2)}
            </pre>
          </section>
        ))}
      </div>
    );
  }

  const text = formatNormalized(normalized);
  const trimmed = text.trim();
  const looksJson =
    (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
    (trimmed.startsWith("[") && trimmed.endsWith("]"));
  return (
    <pre className={`pretty-block${looksJson ? " is-json" : ""}`}>
      {looksJson ? highlightJson(text) : text}
    </pre>
  );
}

const INSPECTOR_WIDTH_KEY = "nika-inspect-inspector-width";
const INSPECTOR_WIDTH_DEFAULT = 38;
const INSPECTOR_WIDTH_MIN = 22;
const INSPECTOR_WIDTH_MAX = 70;

function loadInspectorWidth(): number {
  try {
    const raw = localStorage.getItem(INSPECTOR_WIDTH_KEY);
    if (!raw) return INSPECTOR_WIDTH_DEFAULT;
    const n = Number(raw);
    if (!Number.isFinite(n)) return INSPECTOR_WIDTH_DEFAULT;
    return Math.min(INSPECTOR_WIDTH_MAX, Math.max(INSPECTOR_WIDTH_MIN, n));
  } catch {
    return INSPECTOR_WIDTH_DEFAULT;
  }
}

function SplitPanel({
  left,
  right,
}: {
  left: ReactNode;
  right: ReactNode | null;
}) {
  const [rightPct, setRightPct] = useState(loadInspectorWidth);
  const dragging = useRef(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const rightPctRef = useRef(rightPct);
  rightPctRef.current = rightPct;
  const open = right != null;

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!dragging.current || !panelRef.current) return;
      const rect = panelRef.current.getBoundingClientRect();
      if (rect.width <= 0) return;
      const fromRight = ((rect.right - e.clientX) / rect.width) * 100;
      setRightPct(
        Math.min(INSPECTOR_WIDTH_MAX, Math.max(INSPECTOR_WIDTH_MIN, fromRight)),
      );
    };
    const onUp = () => {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.classList.remove("is-resizing");
      try {
        localStorage.setItem(INSPECTOR_WIDTH_KEY, String(rightPctRef.current));
      } catch {
        /* ignore */
      }
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  if (!open) {
    return (
      <div className="panel panel-single" ref={panelRef}>
        <div className="panel-left">{left}</div>
      </div>
    );
  }

  return (
    <div
      className="panel"
      ref={panelRef}
      style={{ "--inspector-width": `${rightPct}%` } as CSSProperties}
    >
      <div className="panel-left">{left}</div>
      <div
        className="panel-resizer"
        role="separator"
        aria-orientation="vertical"
        aria-valuenow={Math.round(rightPct)}
        aria-valuemin={INSPECTOR_WIDTH_MIN}
        aria-valuemax={INSPECTOR_WIDTH_MAX}
        aria-label="Resize detail panel"
        onMouseDown={(e) => {
          e.preventDefault();
          dragging.current = true;
          document.body.classList.add("is-resizing");
        }}
        onDoubleClick={() => {
          setRightPct(INSPECTOR_WIDTH_DEFAULT);
          try {
            localStorage.setItem(
              INSPECTOR_WIDTH_KEY,
              String(INSPECTOR_WIDTH_DEFAULT),
            );
          } catch {
            /* ignore */
          }
        }}
      />
      <div className="panel-right">{right}</div>
    </div>
  );
}

function LlmTurnPreview({
  row,
  rows,
  sessionModel,
  onOpenTool,
}: {
  row: DisplayEvent;
  rows: DisplayEvent[];
  sessionModel?: string | null;
  onOpenTool: (id: string) => void;
}) {
  const turn = useMemo(
    () => buildLlmTurnDetail(row, rows, sessionModel),
    [row, rows, sessionModel],
  );
  const hasOutput = Boolean(turn.outputText.trim()) || turn.tools.length > 0;
  const running = isRunningSpan(row);
  const nowMs = useNow(running);
  const elapsed = running ? displayDurationMs(row, nowMs) : null;

  return (
    <div className="llm-turn">
      <section className="llm-section">
        <header className="llm-section-head">
          <h3>Input</h3>
          {turn.model && <span className="llm-section-meta">{turn.model}</span>}
        </header>
        {turn.input.trim() ? (
          <pre className="llm-block">{turn.input}</pre>
        ) : (
          <div className="llm-empty">No input message recorded for this call.</div>
        )}
      </section>

      {turn.thinking.trim() ? (
        <section className="llm-section">
          <header className="llm-section-head">
            <h3>Thinking</h3>
          </header>
          <pre className="llm-block thinking">{turn.thinking}</pre>
        </section>
      ) : null}

      <section className="llm-section">
        <header className="llm-section-head">
          <h3>Output</h3>
          {turn.finishReason && (
            <span className="llm-section-meta">finish: {turn.finishReason}</span>
          )}
          {running && elapsed != null && (
            <span className="llm-section-meta">{formatDuration(elapsed)}</span>
          )}
        </header>
        {!hasOutput && (
          <div className="llm-empty">
            {running
              ? `Running… ${formatDuration(elapsed)}`
              : "No text or tool calls recorded for this call."}
          </div>
        )}
        {turn.outputText.trim() ? (
          <pre className="llm-block">{turn.outputText}</pre>
        ) : null}
        {turn.tools.length > 0 && (
          <div className="llm-tools">
            <div className="llm-tools-label">
              Tool calls ({turn.tools.length})
            </div>
            {turn.tools.map((t) => (
              <button
                key={t.rowId}
                type="button"
                className="llm-tool-card"
                onClick={() => onOpenTool(t.rowId)}
              >
                <div className="llm-tool-card-top">
                  <span className="kind-tag role-tool">{t.name}</span>
                  {t.callId && <code className="llm-tool-id">{t.callId}</code>}
                </div>
                <div className="llm-tool-args">
                  {t.input == null || t.input === "" ? (
                    "—"
                  ) : (
                    <PrettyValue value={t.input} />
                  )}
                </div>
              </button>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}

function TagEditor({
  tags,
  knownTags,
  readOnly,
  onChange,
}: {
  tags: string[];
  knownTags: string[];
  readOnly: boolean;
  onChange: (tags: string[]) => void;
}) {
  const [draft, setDraft] = useState("");
  const add = () => {
    const tag = draft.trim();
    setDraft("");
    if (tag && !tags.includes(tag)) onChange([...tags, tag]);
  };
  return (
    <span className="tag-editor">
      {tags.map((t) => (
        <span key={t} className="chip tag-chip">
          {t}
          {!readOnly && (
            <button
              type="button"
              className="tag-remove"
              aria-label={`Remove tag ${t}`}
              onClick={() => onChange(tags.filter((x) => x !== t))}
            >
              ×
            </button>
          )}
        </span>
      ))}
      {!readOnly && (
        <>
          <input
            className="tag-input"
            list="nika-inspect-known-tags"
            placeholder="+ tag"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                add();
              }
            }}
            onBlur={add}
          />
          <datalist id="nika-inspect-known-tags">
            {knownTags
              .filter((t) => !tags.includes(t))
              .map((t) => (
                <option key={t} value={t} />
              ))}
          </datalist>
        </>
      )}
    </span>
  );
}

function CommentsPanel({
  comments,
  readOnly,
  onAdd,
  onDelete,
}: {
  comments: AnnotationComment[];
  readOnly: boolean;
  onAdd: (text: string) => void;
  onDelete: (id: string) => void;
}) {
  const [draft, setDraft] = useState("");
  const submit = () => {
    const text = draft.trim();
    if (!text) return;
    onAdd(text);
    setDraft("");
  };
  return (
    <section className="inspector-comments">
      <div className="inspector-pane-label">Comments ({comments.length})</div>
      {comments.map((c) => (
        <div key={c.id} className="comment">
          <div className="comment-text">{c.text}</div>
          <div className="comment-meta">
            {c.created_at ? formatTs(c.created_at) : ""}
            {!readOnly && (
              <button type="button" className="comment-delete" onClick={() => onDelete(c.id)}>
                Delete
              </button>
            )}
          </div>
        </div>
      ))}
      {readOnly ? (
        comments.length === 0 && (
          <div className="comment-meta">Comments are read-only while the session runs.</div>
        )
      ) : (
        <div className="comment-form">
          <textarea
            rows={2}
            placeholder="Add a review note for this event (Ctrl+Enter)"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <button type="button" disabled={!draft.trim()} onClick={submit}>
            Add
          </button>
        </div>
      )}
    </section>
  );
}

function Inspector({
  row,
  rows,
  sessionModel,
  onClose,
  onOpenEvent,
  comments,
  commentsReadOnly,
  onAddComment,
  onDeleteComment,
}: {
  row: DisplayEvent | null;
  rows: DisplayEvent[];
  sessionModel?: string | null;
  onClose: () => void;
  onOpenEvent: (id: string) => void;
  comments: AnnotationComment[];
  commentsReadOnly: boolean;
  onAddComment: (text: string) => void;
  onDeleteComment: (id: string) => void;
}) {
  const role: Role | null = row?.role ?? null;
  const isTool = row ? isToolDisplay(row) : false;
  const isAssistant = role === "assistant";
  const tabs = isTool
    ? (["call", "overview", "raw"] as const)
    : isAssistant
      ? (["overview", "messages", "raw"] as const)
      : (["overview", "payload", "raw"] as const);
  const defaultTab = isAssistant ? "messages" : isTool ? "call" : "overview";
  const [tab, setTab] = useState<string>(defaultTab);
  const nowMs = useNow(row ? isRunningSpan(row) : false);

  useEffect(() => {
    setTab(defaultTab);
  }, [row?.id, defaultTab]);

  if (!row || !role) {
    return null;
  }

  const start = row.start;
  const end = row.end;
  const running = isRunningSpan(row);
  const duration = displayDurationMs(row, nowMs);
  const tokens = isAssistant ? llmTokenUsage(row) : null;
  const modelName = isAssistant
    ? extractModelName(start.raw.model, end?.raw.model, start.raw, end?.raw, sessionModel)
    : null;
  const status = row.error
    ? "Error"
    : end || (!isTool && !isAssistant)
      ? "Completed"
      : row.stale
        ? "Incomplete"
        : "Running";
  const toolName = isTool
    ? start.tool?.name || end?.tool?.name || null
    : null;

  const codexItem = (ev: CanonicalTraceEvent | null | undefined) => {
    const item = (ev?.raw as { codex_event?: { item?: unknown } } | undefined)
      ?.codex_event?.item;
    return item && typeof item === "object" && !Array.isArray(item)
      ? (item as Record<string, unknown>)
      : null;
  };
  const toolParameters =
    start.tool?.input ??
    start.raw.input ??
    codexItem(start)?.arguments ??
    codexItem(start)?.input ??
    codexItem(end)?.arguments ??
    codexItem(end)?.input ??
    null;
  const toolResults =
    end?.tool?.output ??
    end?.tool?.error ??
    start.tool?.output ??
    start.tool?.error ??
    end?.raw.output ??
    start.raw.output ??
    codexItem(end)?.result ??
    codexItem(end)?.output ??
    codexItem(end)?.error ??
    codexItem(start)?.result ??
    null;

  return (
    <div className="inspector">
      <div className="inspector-header">
        <div className="inspector-title">
          <span className={`kind-tag role-${role}`}>{roleLabel(role)}</span>
          <span className="inspector-meta">
            {toolName ? (
              <>
                <span className="inspector-tool-name">{toolName}</span>
                {row.phase ? ` · ${row.phase}` : ""}
                {duration != null ? ` · ${formatDuration(duration)}` : ""}
              </>
            ) : (
              <>
                {row.phase ? `${row.phase} · ` : ""}
                {row.kind === "llm" ? nameBadgeLabel(row) : row.event || row.kind}
                {duration != null ? ` · ${formatDuration(duration)}` : ""}
              </>
            )}
          </span>
        </div>
        <button type="button" className="inspector-close" onClick={onClose}>
          ×
        </button>
      </div>

      <div className="inspector-tabs">
        {tabs.map((t) => (
          <button
            key={t}
            type="button"
            className={tab === t ? "active" : ""}
            onClick={() => setTab(t)}
          >
            {t.charAt(0).toUpperCase() + t.slice(1)}
          </button>
        ))}
      </div>

      <div className="inspector-body">
        {tab === "overview" && (
          <dl className="kv">
            <dt>Status</dt>
            <dd>{status}</dd>
            <dt>Source</dt>
            <dd>{start.source}</dd>
            <dt>Kind</dt>
            <dd>{row.kind}</dd>
            <dt>Event</dt>
            <dd>{row.event || "—"}</dd>
            {row.attempt != null && (
              <>
                <dt>Attempt</dt>
                <dd>#{row.attempt}</dd>
              </>
            )}
            <dt>Phase</dt>
            <dd>{row.phase || "—"}</dd>
            <dt>Start</dt>
            <dd className="mono">{row.timestamp || "—"}</dd>
            <dt>End</dt>
            <dd className="mono">{row.endTimestamp || "—"}</dd>
            <dt>Duration</dt>
            <dd>
              {formatDuration(duration)}
              {running ? " (elapsed)" : ""}
            </dd>
            <dt>Timing source</dt>
            <dd>
              {row.event === "llm" || row.kind === "llm"
                ? row.start.event === "turn.started"
                  ? "turn.started → turn.completed"
                  : running
                    ? "llm_start → (running)"
                    : row.stale
                      ? "llm_start → (no end logged)"
                      : "llm_start → llm_end"
                : isTool && running
                  ? "tool_start → (running)"
                  : isTool && row.stale
                    ? "tool_start → (no end logged)"
                  : isTool && end
                    ? "tool_start → tool_end"
                    : "Session timestamps"}
            </dd>
            <dt>Start epoch ms</dt>
            <dd className="mono">{parseTs(row.timestamp) ?? "—"}</dd>
            <dt>End epoch ms</dt>
            <dd className="mono">{parseTs(row.endTimestamp) ?? "—"}</dd>
            {isAssistant && (
              <>
                <dt>Model</dt>
                <dd>{modelName || "—"}</dd>
                <dt>Input tokens</dt>
                <dd className="mono">{formatTokenCount(tokens?.input ?? null)}</dd>
                <dt>Output tokens</dt>
                <dd className="mono">{formatTokenCount(tokens?.output ?? null)}</dd>
                <dt>Reasoning tokens</dt>
                <dd className="mono">{formatTokenCount(tokens?.reasoning ?? null)}</dd>
              </>
            )}
            {isTool && (
              <>
                <dt>Tool</dt>
                <dd>{toolName || "—"}</dd>
                <dt>Call ID</dt>
                <dd className="mono">
                  {start.tool?.tool_call_id || end?.tool?.tool_call_id || "—"}
                </dd>
              </>
            )}
          </dl>
        )}

        {tab === "call" && (
          <div className="inspector-call">
            <section className="inspector-pane">
              <div className="inspector-pane-label">Parameters</div>
              <PrettyValue value={toolParameters} />
            </section>
            <section className="inspector-pane">
              <div className="inspector-pane-label">Results</div>
              <PrettyValue value={toolResults ?? row.summary} />
            </section>
          </div>
        )}

        {tab === "messages" && isAssistant && (
          <LlmTurnPreview
            row={row}
            rows={rows}
            sessionModel={sessionModel}
            onOpenTool={onOpenEvent}
          />
        )}

        {tab === "messages" && !isAssistant && (
          <PrettyValue
            value={
              end?.raw.text ??
              start.raw.text ??
              end?.raw.messages ??
              start.raw.messages ??
              end?.raw.report ??
              row.summary ??
              row.title
            }
          />
        )}

        {tab === "payload" && <EventPayloadView raw={start.raw} />}

        {tab === "raw" && (
          <PrettyValue
            value={
              end || (row.interiors && row.interiors.length)
                ? {
                    start: start.raw,
                    ...(row.interiors?.length
                      ? { interiors: row.interiors.map((e) => e.raw) }
                      : {}),
                    ...(end ? { end: end.raw } : {}),
                  }
                : start.raw
            }
          />
        )}
      </div>
      <CommentsPanel
        comments={comments}
        readOnly={commentsReadOnly}
        onAdd={onAddComment}
        onDelete={onDeleteComment}
      />
    </div>
  );
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function formatMetric(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  if (value < 0) return "—";
  return value.toFixed(3);
}

type RootCausePair = { resourceId: string; faultType: string };

function parseRootCauses(payload: unknown): RootCausePair[] {
  const obj = asRecord(payload);
  const raw = obj?.root_causes;
  if (!Array.isArray(raw)) return [];
  const out: RootCausePair[] = [];
  for (const item of raw) {
    const row = asRecord(item);
    if (!row) continue;
    const resource = asRecord(row.resource);
    const resourceId =
      (typeof row.resource_id === "string" && row.resource_id) ||
      (typeof resource?.id === "string" && resource.id) ||
      "";
    const faultType = typeof row.fault_type === "string" ? row.fault_type : "";
    if (!resourceId || !faultType) continue;
    out.push({ resourceId, faultType });
  }
  return out;
}

function readAnomaly(payload: unknown): boolean | null {
  const obj = asRecord(payload);
  if (!obj || !("is_anomaly" in obj)) return null;
  const v = obj.is_anomaly;
  if (v === true || v === "True" || v === "true" || v === 1 || v === "1") return true;
  if (v === false || v === "False" || v === "false" || v === 0 || v === "0") return false;
  return null;
}

function ScoresPanel({ scores }: { scores: ScoresResponse | null }) {
  if (!scores) return <div className="empty">Loading scores…</div>;
  const metrics = (scores.eval_metrics || {}) as Record<string, unknown>;
  const gt = scores.ground_truth;
  const submission = scores.submission;
  const gtCauses = parseRootCauses(gt);
  const predCauses = parseRootCauses(submission);
  const gtAnomaly = readAnomaly(gt);
  const predAnomaly = readAnomaly(submission);
  const detectionMatch =
    gtAnomaly != null && predAnomaly != null ? gtAnomaly === predAnomaly : null;
  const diagnosisReport =
    typeof asRecord(submission)?.diagnosis_report === "string"
      ? String(asRecord(submission)!.diagnosis_report)
      : null;
  const failureDomain =
    typeof asRecord(gt)?.failure_domain === "string"
      ? String(asRecord(gt)!.failure_domain)
      : null;

  const metricGroups: { title: string; rows: [string, string][] }[] = [
    {
      title: "Detection",
      rows: [["detection_score", "detection_score"]],
    },
    {
      title: "RCA (resource + fault_type)",
      rows: [
        ["precision", "rca_precision"],
        ["recall", "rca_recall"],
        ["f1", "rca_f1"],
      ],
    },
    {
      title: "Localization (resource)",
      rows: [
        ["precision", "localization_precision"],
        ["recall", "localization_recall"],
        ["f1", "localization_f1"],
      ],
    },
    {
      title: "Fault type",
      rows: [
        ["precision", "fault_type_precision"],
        ["recall", "fault_type_recall"],
        ["f1", "fault_type_f1"],
      ],
    },
  ];

  const matchMark = (ok: boolean | null) => {
    if (ok == null) return <span className="verdict verdict-unknown">—</span>;
    return ok ? (
      <span className="verdict verdict-ok" aria-label="match">
        ✓
      </span>
    ) : (
      <span className="verdict verdict-bad" aria-label="mismatch">
        ✗
      </span>
    );
  };

  // Align GT/pred root causes for side-by-side rows (match on fault_type when unique).
  type AlignedCause = {
    gt: RootCausePair | null;
    pred: RootCausePair | null;
  };
  const aligned: AlignedCause[] = [];
  const predUsed = new Set<number>();
  for (const gt of gtCauses) {
    let predIdx = predCauses.findIndex(
      (p, i) => !predUsed.has(i) && p.faultType === gt.faultType && p.resourceId === gt.resourceId,
    );
    if (predIdx < 0) {
      predIdx = predCauses.findIndex(
        (p, i) => !predUsed.has(i) && p.faultType === gt.faultType,
      );
    }
    if (predIdx < 0) {
      predIdx = predCauses.findIndex((_p, i) => !predUsed.has(i));
    }
    if (predIdx >= 0) {
      predUsed.add(predIdx);
      aligned.push({ gt, pred: predCauses[predIdx]! });
    } else {
      aligned.push({ gt, pred: null });
    }
  }
  predCauses.forEach((pred, i) => {
    if (!predUsed.has(i)) aligned.push({ gt: null, pred });
  });

  const fieldRows: {
    key: string;
    field: string;
    expected: string;
    actual: string;
    ok: boolean | null;
  }[] = [
    {
      key: "is_anomaly",
      field: "is_anomaly",
      expected: gtAnomaly == null ? "—" : String(gtAnomaly),
      actual: predAnomaly == null ? "—" : String(predAnomaly),
      ok: detectionMatch,
    },
  ];
  aligned.forEach((row, idx) => {
    const suffix = aligned.length > 1 ? ` #${idx + 1}` : "";
    const resOk =
      row.gt && row.pred ? row.gt.resourceId === row.pred.resourceId : null;
    const typeOk =
      row.gt && row.pred ? row.gt.faultType === row.pred.faultType : null;
    fieldRows.push(
      {
        key: `res-${idx}`,
        field: `resource${suffix}`,
        expected: row.gt?.resourceId ?? "—",
        actual: row.pred?.resourceId ?? "—",
        ok: row.gt && row.pred ? resOk : false,
      },
      {
        key: `ft-${idx}`,
        field: `fault_type${suffix}`,
        expected: row.gt?.faultType ?? "—",
        actual: row.pred?.faultType ?? "—",
        ok: row.gt && row.pred ? typeOk : false,
      },
    );
  });

  return (
    <div className="scores">
      <section className="scores-section">
        <h3>Metrics</h3>
        <div className="metrics-boards">
          {metricGroups.map((group) => (
            <table key={group.title} className="metrics-table">
              <thead>
                <tr>
                  <th colSpan={2}>{group.title}</th>
                </tr>
              </thead>
              <tbody>
                {group.rows.map(([label, key]) => (
                  <tr key={key}>
                    <td>{label}</td>
                    <td className="mono metrics-value">{formatMetric(metrics[key])}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ))}
          <table className="metrics-table">
            <thead>
              <tr>
                <th colSpan={2}>Run</th>
              </tr>
            </thead>
            <tbody>
              {(
                [
                  ["steps", "steps"],
                  ["tool_calls", "tool_calls"],
                  ["in_tokens", "in_tokens"],
                  ["out_tokens", "out_tokens"],
                ] as const
              ).map(([label, key]) => (
                <tr key={key}>
                  <td>{label}</td>
                  <td className="mono metrics-value">
                    {typeof metrics[key] === "number" ? String(metrics[key]) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="scores-section">
        <div className="scores-section-head">
          <h3>Ground truth vs submission</h3>
          <div className="scores-folds">
            <details className="scores-fold">
              <summary>Ground truth</summary>
              {gt ? (
                <div className="scores-fold-body">
                  <PrettyValue value={gt} />
                </div>
              ) : (
                <div className="empty">No ground truth</div>
              )}
            </details>
            <details className="scores-fold">
              <summary>Submission</summary>
              {submission ? (
                <div className="scores-fold-body">
                  <PrettyValue
                    value={(() => {
                      const obj = asRecord(submission);
                      if (!obj) return submission;
                      const { diagnosis_report: _report, ...rest } = obj;
                      return Object.keys(rest).length ? rest : submission;
                    })()}
                  />
                </div>
              ) : (
                <div className="empty">No submission</div>
              )}
            </details>
          </div>
        </div>
        {failureDomain && (
          <div className="scores-meta">failure_domain · {failureDomain}</div>
        )}
        <table className="diff-table">
          <thead>
            <tr>
              <th className="diff-status"> </th>
              <th>Field</th>
              <th>Expected (Ground truth)</th>
              <th>Actual (Submission)</th>
            </tr>
          </thead>
          <tbody>
            {fieldRows.map((row) => (
              <tr
                key={row.key}
                className={
                  row.ok == null ? "diff-unknown" : row.ok ? "diff-ok" : "diff-bad"
                }
              >
                <td className="diff-status">{matchMark(row.ok)}</td>
                <td className="diff-field">{row.field}</td>
                <td className="mono diff-value">{row.expected}</td>
                <td className="mono diff-value">{row.actual}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <section className="scores-section">
        <h3>Diagnosis report</h3>
        {diagnosisReport ? (
          <div className="diagnosis-report">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>
              {diagnosisReport}
            </ReactMarkdown>
          </div>
        ) : submission ? (
          <PrettyValue value={submission} />
        ) : (
          <div className="empty">No submission</div>
        )}
      </section>
    </div>
  );
}

function SessionOverviewPanel({ detail }: { detail: SessionDetail | null }) {
  if (!detail) return <div className="empty">Loading overview…</div>;

  const injectEntries = Object.entries(detail.inject_params || {});
  const scenarioParams = asRecord(detail.run?.scenario_params);
  const scenarioParamRows = scenarioParams
    ? Object.entries(scenarioParams).filter(
        ([, v]) => v != null && v !== "" && typeof v !== "object",
      )
    : [];

  const rows: [string, ReactNode][] = [
    ["Failure", sessionTitle(detail)],
    ["Domain", detail.failure_domain || "—"],
    [
      "Location",
      injectEntries.length ? (
        <span className="session-overview-location">
          {injectEntries.map(([k, v]) => (
            <span key={k} className="session-overview-kv">
              <span className="session-overview-kv-k">{k}</span>
              <span className="session-overview-kv-v mono">{v}</span>
            </span>
          ))}
        </span>
      ) : (
        "—"
      ),
    ],
    [
      "Scenario",
      detail.scenario_name || "—",
    ],
    ["Size", detail.scenario_topo_size || "—"],
    ["Backend", detail.backend || "—"],
    ["Lab", detail.lab_name || "—"],
    [
      "Agent",
      [detail.agent_type, detail.model, detail.llm_provider]
        .filter(Boolean)
        .join(" · ") || "—",
    ],
    [
      "Timing",
      [
        detail.start_time ? formatTs(detail.start_time) : null,
        sessionDuration(detail) !== "—" ? sessionDuration(detail) : null,
      ]
        .filter(Boolean)
        .join(" · ") || "—",
    ],
    [
      "Scores",
      [
        detail.detection_score != null
          ? `det ${formatScore(detail.detection_score)}`
          : null,
        detail.rca_f1 != null ? `rca ${formatScore(detail.rca_f1)}` : null,
        detail.localization_f1 != null
          ? `loc ${formatScore(detail.localization_f1)}`
          : null,
      ]
        .filter(Boolean)
        .join(" · ") || "—",
    ],
    [
      "Usage",
      [
        detail.in_tokens != null || detail.out_tokens != null
          ? `${detail.in_tokens ?? "—"}/${detail.out_tokens ?? "—"} tok`
          : null,
        detail.steps != null ? `${detail.steps} steps` : null,
        detail.tool_calls != null ? `${detail.tool_calls} tools` : null,
      ]
        .filter(Boolean)
        .join(" · ") || "—",
    ],
  ];

  if (detail.is_benchmark) {
    if (detail.case_key) rows.push(["Case", detail.case_key]);
    if (detail.benchmark_run_id) {
      rows.push([
        "Run ID",
        <span className="mono">{detail.benchmark_run_id}</span>,
      ]);
    }
  }

  rows.push(
    ["Session ID", <span className="mono">{detail.session_id}</span>],
    [
      "Path",
      <span className="mono session-overview-path">{detail.session_dir}</span>,
    ],
  );
  if (detail.outcome) rows.push(["Outcome", detail.outcome]);

  return (
    <div className="session-overview">
      <section className="session-overview-block">
        <header className="session-overview-block-head">Session</header>
        <table className="session-overview-table">
          <tbody>
            {rows.map(([label, value]) => (
              <tr key={label}>
                <th>{label}</th>
                <td>{value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {scenarioParamRows.length > 0 && (
        <details className="session-overview-fold" open>
          <summary>Scenario params</summary>
          <table className="session-overview-table">
            <tbody>
              {scenarioParamRows.map(([k, v]) => (
                <tr key={k}>
                  <th>{k}</th>
                  <td className="mono">{String(v)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}

      {detail.task_description ? (
        <details className="session-overview-fold" open>
          <summary>Task</summary>
          <div className="session-overview-task">{detail.task_description}</div>
        </details>
      ) : null}
    </div>
  );
}

function RawPanel({
  sessionId,
  root,
}: {
  sessionId: string;
  root: string;
}) {
  const [filename, setFilename] = useState("run.json");
  const [data, setData] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchRaw(sessionId, filename, root)
      .then((res) => {
        if (!cancelled) {
          setData(res.data);
          setError(null);
        }
      })
      .catch((err: Error) => {
        if (!cancelled) {
          setError(err.message);
          setData(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, filename, root]);

  return (
    <div className="raw">
      <div className="raw-bar">
        <select value={filename} onChange={(e) => setFilename(e.target.value)}>
          {RAW_FILES.map((f) => (
            <option key={f} value={f}>
              {f}
            </option>
          ))}
        </select>
      </div>
      {error && <div className="empty">{error}</div>}
      {!error && <PrettyValue value={data} />}
    </div>
  );
}

function SessionView({
  sessionId,
  root,
  initialNav,
  knownTags,
  onNavChange,
  onBack,
}: {
  sessionId: string;
  root: string;
  /** Read once on mount; the parent remounts this view per session. */
  initialNav: SessionNav;
  knownTags: string[];
  onNavChange: (nav: SessionNav) => void;
  onBack: () => void;
}) {
  const [tab, setTab] = useState<Tab>(initialNav.tab ?? "timeline");
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [events, setEvents] = useState<CanonicalTraceEvent[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(initialNav.event);
  const [brush, setBrush] = useState<TimeBrush | null>(null);
  const [scores, setScores] = useState<ScoresResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [find, setFind] = useState(initialNav.find ?? "");
  const [debouncedFind, setDebouncedFind] = useState(find.trim());
  const [findHits, setFindHits] = useState<Set<string> | null>(null);
  const [annotations, setAnnotations] = useState<Annotations | null>(null);
  const findInputRef = useRef<HTMLInputElement>(null);
  /** Jump to the first hit once per query when nothing is selected yet. */
  const autoJumpedForRef = useRef<string | null>(null);
  // A failed session load blocks every tab; tab fetch errors stay per tab.
  const shownError = detailError ?? error;
  const isTraceTab = tab === "timeline" || tab === "agent" || tab === "nika";

  useEffect(() => {
    onNavChange({ tab, event: selectedId, find: find.trim() || null });
  }, [tab, selectedId, find, onNavChange]);

  useEffect(() => {
    const id = window.setTimeout(() => setDebouncedFind(find.trim()), 250);
    return () => window.clearTimeout(id);
  }, [find]);

  // Re-run when new log lines arrive so live sessions keep matching.
  useEffect(() => {
    if (!debouncedFind) {
      setFindHits(null);
      return;
    }
    let cancelled = false;
    fetchSessionSearch(sessionId, debouncedFind, root)
      .then((data) => {
        if (!cancelled) setFindHits(new Set(data.event_ids));
      })
      .catch(() => {
        if (!cancelled) setFindHits(new Set());
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, root, debouncedFind, events.length]);

  useEffect(() => {
    let cancelled = false;
    fetchAnnotations(sessionId, root)
      .then((data) => {
        if (!cancelled) setAnnotations(data);
      })
      .catch(() => {
        if (!cancelled) setAnnotations({ tags: [], comments: [] });
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, root]);

  const annotationsReadOnly = detail?.status === "running";
  const persistAnnotations = async (next: Annotations) => {
    try {
      setAnnotations(await saveAnnotations(sessionId, next, root));
    } catch (err) {
      window.alert(err instanceof Error ? err.message : String(err));
    }
  };
  const commentCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const c of annotations?.comments ?? []) {
      counts.set(c.event_id, (counts.get(c.event_id) ?? 0) + 1);
    }
    return counts;
  }, [annotations]);

  // Until the session detail loads, assume live so open spans keep ticking.
  const sessionLive = detail == null || detail.status === "running";
  const rows = useMemo(() => {
    const collapsed = collapsePairedEvents(events);
    const sorted = [...collapsed].sort((a, b) => {
      const ta = parseTs(a.timestamp);
      const tb = parseTs(b.timestamp);
      if (ta == null && tb == null) return a.id.localeCompare(b.id);
      if (ta == null) return 1;
      if (tb == null) return -1;
      if (ta !== tb) return ta - tb;
      const ea = parseTs(a.endTimestamp) ?? ta;
      const eb = parseTs(b.endTimestamp) ?? tb;
      if (ea !== eb) return ea - eb;
      return a.id.localeCompare(b.id);
    });
    const withSuperseded = closeSupersededOpenSpans(sorted);
    const withStale = sessionLive
      ? withSuperseded
      : withSuperseded.map((r) =>
          isRunningSpan(r)
            ? {
                ...r,
                stale: true,
                summary: r.summary === "in progress" ? "no end logged" : r.summary,
              }
            : r,
        );
    return annotateLlmRetryAttempts(withStale);
  }, [events, sessionLive]);
  const visibleRows = useMemo(
    () => (brush ? rows.filter((r) => rowInBrush(r, brush)) : rows),
    [rows, brush],
  );
  const selected = useMemo(
    () => visibleRows.find((r) => r.id === selectedId) ?? null,
    [visibleRows, selectedId],
  );

  useEffect(() => {
    setBrush(null);
  }, [sessionId, root]);

  useEffect(() => {
    // Rows not loaded yet: keep a deep-linked event until the timeline arrives.
    if (!selectedId || rows.length === 0) return;
    if (visibleRows.some((r) => r.id === selectedId)) return;
    // A new brush wins over an older pick outside it (picking clears the brush).
    setSelectedId(null);
  }, [rows.length, visibleRows, selectedId]);

  const matchedRows = useMemo(
    () =>
      findHits
        ? visibleRows.filter((r) => rowEventIds(r).some((id) => findHits.has(id)))
        : [],
    [visibleRows, findHits],
  );
  const matchedIds = useMemo(() => new Set(matchedRows.map((r) => r.id)), [matchedRows]);

  const moveSelection = (delta: 1 | -1) => {
    if (!visibleRows.length) return;
    const idx = visibleRows.findIndex((r) => r.id === selectedId);
    const next =
      idx < 0
        ? delta > 0
          ? 0
          : visibleRows.length - 1
        : Math.min(visibleRows.length - 1, Math.max(0, idx + delta));
    setSelectedId(visibleRows[next].id);
  };

  const stepMatch = (delta: 1 | -1) => {
    if (!matchedRows.length) return;
    const order = new Map(visibleRows.map((r, i) => [r.id, i]));
    const cur = selectedId != null ? (order.get(selectedId) ?? -1) : -1;
    const pos = (r: DisplayEvent) => order.get(r.id) ?? 0;
    const target =
      delta > 0
        ? (matchedRows.find((r) => pos(r) > cur) ?? matchedRows[0])
        : ([...matchedRows].reverse().find((r) => pos(r) < cur) ??
          matchedRows[matchedRows.length - 1]);
    setBrush(null);
    setSelectedId(target.id);
  };

  useEffect(() => {
    if (!debouncedFind || autoJumpedForRef.current === debouncedFind) return;
    if (!matchedRows.length) return;
    autoJumpedForRef.current = debouncedFind;
    if (selectedId == null) setSelectedId(matchedRows[0].id);
  }, [debouncedFind, matchedRows, selectedId]);

  const keyActionsRef = useRef({ moveSelection, isTraceTab });
  keyActionsRef.current = { moveSelection, isTraceTab };
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const actions = keyActionsRef.current;
      if (!actions.isTraceTab || e.defaultPrevented) return;
      if (e.metaKey || e.ctrlKey || e.altKey || isEditableTarget(e.target)) return;
      if (e.key === "j" || e.key === "k") {
        e.preventDefault();
        actions.moveSelection(e.key === "j" ? 1 : -1);
      } else if (e.key === "/") {
        e.preventDefault();
        findInputRef.current?.focus();
        findInputRef.current?.select();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const matchPos = selectedId != null ? matchedRows.findIndex((r) => r.id === selectedId) : -1;

  useEffect(() => {
    let cancelled = false;
    let finished = false;
    const load = (background: boolean) =>
      fetchSession(sessionId, root)
        .then((d) => {
          if (cancelled) return;
          setDetail(d);
          setDetailError(null);
          finished = d.status !== "running";
        })
        .catch((err: Error) => {
          if (!cancelled && !background) setDetailError(err.message);
        });
    void load(false);
    const poll = window.setInterval(() => {
      if (finished) {
        window.clearInterval(poll);
        return;
      }
      void load(true);
    }, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(poll);
    };
  }, [sessionId, root]);

  // Each tab reports its own fetch error; switching tabs starts clean.
  useEffect(() => {
    setError(null);
  }, [tab]);

  useEffect(() => {
    if (tab === "overview" || tab === "scores" || tab === "raw") return;
    let cancelled = false;
    const source =
      tab === "agent" ? "agent" : tab === "nika" ? "nika" : "merged";
    fetchTimeline(sessionId, source, root)
      .then((data) => {
        if (cancelled) return;
        setEvents(data.events);
        const collapsed = collapsePairedEvents(data.events);
        setSelectedId((prev) => {
          if (prev && collapsed.some((r) => r.id === prev)) return prev;
          return null;
        });
        setError(null);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, tab, root]);

  useEffect(() => {
    if (tab === "overview" || tab === "scores" || tab === "raw") return;
    const source =
      tab === "agent" ? "agent" : tab === "nika" ? "nika" : "merged";
    let cancelled = false;
    const tick = () =>
      fetchTimeline(sessionId, source, root)
        .then((data) => {
          if (cancelled) return;
          // Logs are append-only: same length means nothing new to lay out.
          setEvents((prev) =>
            prev.length === data.events.length ? prev : data.events,
          );
        })
        .catch(() => undefined);
    if (detail?.status === "finished" || detail?.status === "aborted" || detail?.status === "error") {
      void tick();
      return () => {
        cancelled = true;
      };
    }
    if (detail?.status !== "running") return;
    const poll = window.setInterval(tick, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(poll);
    };
  }, [sessionId, tab, detail?.status, root]);

  useEffect(() => {
    if (tab !== "scores") return;
    let cancelled = false;
    fetchScores(sessionId, root)
      .then((data) => {
        if (!cancelled) setScores(data);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, tab, root]);

  const stats = useMemo(() => {
    const nika = visibleRows.filter((r) => r.role === "nika").length;
    const tools = visibleRows.filter((r) => isToolDisplay(r)).length;
    const model = visibleRows.filter((r) => r.role === "assistant").length;
    return { nika, tools, model, total: visibleRows.length };
  }, [visibleRows]);

  return (
    <div className="detail">
      <div className="detail-header">
        <button className="back" onClick={onBack}>
          ← Sessions
        </button>
        <div className="detail-title" title={sessionId}>
          <h1>
            <span>{detail ? sessionTitle(detail) : sessionId}</span>
            {detail?.scenario_name && (
              <span className="detail-title-sep">
                {detail.scenario_topo_size
                  ? `${detail.scenario_name} (${detail.scenario_topo_size})`
                  : detail.scenario_name}
              </span>
            )}
            {detail?.agent_type && (
              <span className="detail-title-sep">{detail.agent_type}</span>
            )}
          </h1>
        </div>
        {detail && <span className={`chip ${detail.status}`}>{detail.status}</span>}
        {detail?.backend && (
          <span className="chip" title="Lab runtime backend">
            {detail.backend}
          </span>
        )}
        {detail?.is_benchmark && detail.benchmark_label && (
          <span className="chip" title={`benchmark ${detail.benchmark_label}`}>
            {detail.benchmark_label}
          </span>
        )}
        {detail?.is_benchmark && detail.trial_index != null && (
          <span className="chip">
            trial t{String(detail.trial_index).padStart(2, "0")}
          </span>
        )}
        {detail?.rca_f1 != null && (
          <span className="chip score">rca_f1 {formatScore(detail.rca_f1)}</span>
        )}
        {annotations && (
          <TagEditor
            tags={annotations.tags}
            knownTags={knownTags}
            readOnly={annotationsReadOnly}
            onChange={(tags) => void persistAnnotations({ ...annotations, tags })}
          />
        )}
        {isTraceTab && (
          <div className="stat-row">
            <span>{stats.total} events</span>
            <span className="role-nika">{stats.nika} nika</span>
            <span className="role-assistant">{stats.model} agent</span>
            <span className="role-tool">{stats.tools} tools</span>
            <span className="event-nav">
              <button type="button" title="Previous event (K)" onClick={() => moveSelection(-1)}>
                ↑ K
              </button>
              <button type="button" title="Next event (J)" onClick={() => moveSelection(1)}>
                ↓ J
              </button>
            </span>
            <span className="find-box">
              <input
                ref={findInputRef}
                type="search"
                placeholder="Find in trajectory (/)"
                value={find}
                onChange={(e) => setFind(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    stepMatch(e.shiftKey ? -1 : 1);
                  } else if (e.key === "Escape") {
                    e.preventDefault();
                    (e.target as HTMLInputElement).blur();
                  }
                }}
              />
              {findHits && (
                <span className="find-count">
                  {matchPos >= 0 ? matchPos + 1 : 0}/{matchedRows.length}
                </span>
              )}
            </span>
          </div>
        )}
      </div>
      <div className="tabs">
        {(
          [
            ["timeline", "Timeline"],
            ["agent", "Agent"],
            ["nika", "NIKA"],
            ["scores", "Scores"],
            ["overview", "Overview"],
            ["raw", "Raw"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            className={tab === id ? "active" : ""}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </div>
      {shownError && <div className="empty">{shownError}</div>}
      {!shownError && tab === "overview" && <SessionOverviewPanel detail={detail} />}
      {!shownError && isTraceTab && (
        <div className="trace-layout">
          {tab === "timeline" && (
            <OverviewTimeline
              events={events}
              selectedId={selectedId}
              onSelect={(id) => {
                setSelectedId(id);
                if (id != null) setBrush(null);
              }}
              brush={brush}
              onBrushChange={setBrush}
            />
          )}
          <SplitPanel
            left={
              <Ledger
                rows={visibleRows}
                selectedId={selectedId}
                matchedIds={matchedIds}
                commentCounts={commentCounts}
                onSelect={(row) =>
                  setSelectedId((prev) => (prev === row.id ? null : row.id))
                }
              />
            }
            right={
              selected ? (
                <Inspector
                  row={selected}
                  rows={rows}
                  sessionModel={detail?.model}
                  onClose={() => setSelectedId(null)}
                  onOpenEvent={setSelectedId}
                  comments={(annotations?.comments ?? []).filter(
                    (c) => c.event_id === selected.id,
                  )}
                  commentsReadOnly={annotations == null || annotationsReadOnly}
                  onAddComment={(text) =>
                    annotations &&
                    void persistAnnotations({
                      ...annotations,
                      comments: [
                        ...annotations.comments,
                        {
                          id: `c${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`,
                          event_id: selected.id,
                          text,
                          created_at: new Date().toISOString(),
                        },
                      ],
                    })
                  }
                  onDeleteComment={(id) =>
                    annotations &&
                    void persistAnnotations({
                      ...annotations,
                      comments: annotations.comments.filter((c) => c.id !== id),
                    })
                  }
                />
              ) : null
            }
          />
        </div>
      )}
      {!shownError && tab === "scores" && <ScoresPanel scores={scores} />}
      {!shownError && tab === "raw" && (
        <RawPanel sessionId={sessionId} root={root} />
      )}
    </div>
  );
}

const RESULTS_ROOT_KEY = "nika-inspect-results-root";

function loadCachedResultsRoot(): string | null {
  try {
    const raw = localStorage.getItem(RESULTS_ROOT_KEY)?.trim();
    return raw || null;
  } catch {
    return null;
  }
}

function saveCachedResultsRoot(path: string) {
  try {
    const trimmed = path.trim();
    if (!trimmed) localStorage.removeItem(RESULTS_ROOT_KEY);
    else localStorage.setItem(RESULTS_ROOT_KEY, trimmed);
  } catch {
    /* ignore quota / private mode */
  }
}

function clearCachedResultsRoot() {
  try {
    localStorage.removeItem(RESULTS_ROOT_KEY);
  } catch {
    /* ignore */
  }
}

/** Map a validated roots response into the App root selection state. */
function selectionFromRoots(
  requested: string,
  data: { base_root: string; results_root: string },
): { selectedRoot: string; activePath: string } {
  const trimmed = requested.trim();
  const selectedRoot =
    trimmed === data.base_root ||
    trimmed === `${data.base_root}/` ||
    trimmed === "."
      ? "."
      : trimmed;
  return { selectedRoot, activePath: data.results_root };
}

export default function App() {
  const [initialUrl] = useState(() => parseUrlState(window.location.search));
  const [sessionId, setSessionId] = useState<string | null>(initialUrl.session);
  const [sessionNav, setSessionNav] = useState<SessionNav>(() => navFromUrl(initialUrl));
  const [booted, setBooted] = useState(false);
  /** Next URL write adds a history entry (session open/close, folder change). */
  const pushHistoryRef = useRef(false);
  const [selectedRoot, setSelectedRoot] = useState(".");
  const [baseRoot, setBaseRoot] = useState("");
  const [activePath, setActivePath] = useState("");
  const [draftPath, setDraftPath] = useState("");
  const [rootError, setRootError] = useState<string | null>(null);
  const [rootBusy, setRootBusy] = useState(false);
  const [browserOpen, setBrowserOpen] = useState(false);
  const [browsePath, setBrowsePath] = useState("");
  const [browseParent, setBrowseParent] = useState<string | null>(null);
  const [browseEntries, setBrowseEntries] = useState<BrowseEntry[]>([]);
  const [browseError, setBrowseError] = useState<string | null>(null);
  const [browseLoading, setBrowseLoading] = useState(false);
  const rootPickerRef = useRef<HTMLDivElement>(null);
  const rootInputRef = useRef<HTMLInputElement>(null);
  const selectedRootRef = useRef(selectedRoot);
  selectedRootRef.current = selectedRoot;

  /** Validate ``raw`` with the server and make it the active results folder. */
  const applyRoot = useCallback(async (raw: string) => {
    const data = await fetchRoots(raw);
    const { selectedRoot: next, activePath: path } = selectionFromRoots(raw, data);
    setBaseRoot(data.base_root);
    setSelectedRoot(next);
    selectedRootRef.current = next;
    setActivePath(path);
    setDraftPath(path);
    saveCachedResultsRoot(path);
  }, []);

  useEffect(() => {
    let cancelled = false;
    const boot = async () => {
      // A deep link's folder wins over the folder remembered in this browser.
      const cached = initialUrl.root ?? loadCachedResultsRoot();
      if (cached) {
        try {
          await applyRoot(cached);
          return;
        } catch {
          // Cached folder gone or outside allowed roots — fall back.
          if (!initialUrl.root) clearCachedResultsRoot();
        }
      }
      try {
        const data = await fetchRoots();
        if (cancelled) return;
        setBaseRoot(data.base_root);
        setActivePath(data.results_root);
        setDraftPath(data.results_root);
      } catch {
        /* ignore boot failure; UI shows empty until user picks a path */
      }
    };
    void boot().finally(() => {
      if (!cancelled) setBooted(true);
    });
    return () => {
      cancelled = true;
    };
  }, [applyRoot, initialUrl]);

  useEffect(() => {
    if (!booted) return;
    const search = buildUrlSearch({
      root: selectedRoot,
      session: sessionId,
      ...(sessionId ? sessionNav : EMPTY_NAV),
    });
    const push = pushHistoryRef.current;
    pushHistoryRef.current = false;
    if (search === window.location.search) return;
    const url = `${window.location.pathname}${search}${window.location.hash}`;
    if (push) window.history.pushState(null, "", url);
    else window.history.replaceState(null, "", url);
  }, [booted, selectedRoot, sessionId, sessionNav]);

  useEffect(() => {
    const onPop = () => {
      const next = parseUrlState(window.location.search);
      setSessionId(next.session);
      setSessionNav(navFromUrl(next));
      const root = next.root ?? ".";
      if (root !== selectedRootRef.current) void applyRoot(root).catch(() => undefined);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [applyRoot]);

  const openSession = useCallback((id: string, find?: string) => {
    pushHistoryRef.current = true;
    setSessionNav({ ...EMPTY_NAV, find: find || null });
    setSessionId(id);
  }, []);

  const clearSession = useCallback(() => {
    pushHistoryRef.current = true;
    setSessionNav(EMPTY_NAV);
    setSessionId(null);
  }, []);

  useEffect(() => {
    if (!browserOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (!rootPickerRef.current?.contains(e.target as Node)) {
        setBrowserOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      setBrowserOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [browserOpen]);

  const browseSeqRef = useRef(0);
  const loadBrowse = (path: string) => {
    // Fast folder clicks: only the latest listing may land.
    const seq = ++browseSeqRef.current;
    const current = () => seq === browseSeqRef.current;
    setBrowseLoading(true);
    setBrowseError(null);
    fetchBrowse(path || undefined)
      .then((data) => {
        if (!current()) return;
        setBrowsePath(data.path);
        setBrowseParent(data.parent ?? null);
        setBrowseEntries(data.entries);
        if (!baseRoot) setBaseRoot(data.base_root);
      })
      .catch((err: Error) => {
        if (!current()) return;
        setBrowseError(err.message);
        setBrowseEntries([]);
      })
      .finally(() => {
        if (current()) setBrowseLoading(false);
      });
  };

  const openBrowser = () => {
    const start = draftPath.trim() || activePath || baseRoot || ".";
    setBrowserOpen(true);
    loadBrowse(start);
  };

  const commitRoot = async (raw: string) => {
    const trimmed = raw.trim();
    if (!trimmed || rootBusy) return;
    setRootBusy(true);
    try {
      await applyRoot(trimmed);
      clearSession();
      setRootError(null);
      setBrowserOpen(false);
    } catch (err) {
      setRootError(err instanceof Error ? err.message : String(err));
    } finally {
      setRootBusy(false);
    }
  };

  const submitRoot = () => {
    void commitRoot(rootInputRef.current?.value ?? draftPath);
  };

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          NIKA <span>Inspect</span>
        </div>
        <div className="topbar-meta" ref={rootPickerRef}>
          <div className="root-picker">
            <input
              ref={rootInputRef}
              className="topbar-root topbar-root-input"
              aria-label="Results folder"
              placeholder="Results path…"
              spellCheck={false}
              value={draftPath}
              onChange={(e) => {
                setDraftPath(e.target.value);
                if (rootError) setRootError(null);
              }}
              onFocus={() => {
                if (!browserOpen) openBrowser();
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  submitRoot();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  setDraftPath(activePath);
                  setRootError(null);
                  setBrowserOpen(false);
                  (e.target as HTMLInputElement).blur();
                } else if (e.key === "ArrowDown") {
                  e.preventDefault();
                  openBrowser();
                }
              }}
            />
            <button
              type="button"
              className="root-picker-toggle"
              aria-label="Browse results folders"
              aria-expanded={browserOpen}
              onClick={() => (browserOpen ? setBrowserOpen(false) : openBrowser())}
            >
              ▾
            </button>
            {browserOpen && (
              <div className="root-browser" role="dialog" aria-label="Results folder browser">
                <div className="root-browser-bar">
                  <button
                    type="button"
                    className="root-browser-up"
                    disabled={!browseParent || browseLoading}
                    onClick={() => browseParent && loadBrowse(browseParent)}
                  >
                    ↑ Up
                  </button>
                  <div className="root-browser-path" title={browsePath}>
                    {browsePath || "…"}
                  </div>
                  <button
                    type="button"
                    className="root-browser-select"
                    disabled={!browsePath || browseLoading || rootBusy}
                    onClick={() => void commitRoot(browsePath)}
                  >
                    Open
                  </button>
                </div>
                {browseError && (
                  <div className="root-browser-empty">{browseError}</div>
                )}
                {!browseError && browseLoading && browseEntries.length === 0 && (
                  <div className="root-browser-empty">Loading…</div>
                )}
                {!browseError && !browseLoading && browseEntries.length === 0 && (
                  <div className="root-browser-empty">No subfolders</div>
                )}
                {!browseError && browseEntries.length > 0 && (
                  <ul className={`root-browser-list${browseLoading ? " is-loading" : ""}`}>
                    {browseEntries.map((entry) => (
                      <li key={entry.path}>
                        <button
                          type="button"
                          className="root-browser-row"
                          onClick={() => loadBrowse(entry.path)}
                          onDoubleClick={() => void commitRoot(entry.path)}
                          title={entry.path}
                        >
                          <span className="root-browser-folder" aria-hidden>
                            <FolderGlyph />
                          </span>
                          <span className="root-browser-name">{entry.name}</span>
                          {entry.has_sessions && (
                            <span className="root-browser-badge">sessions</span>
                          )}
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                <div className="root-browser-hint">
                  Click a folder to enter · Double-click or Open to select
                </div>
              </div>
            )}
          </div>
          <button
            type="button"
            className="topbar-root-go"
            disabled={rootBusy}
            onClick={submitRoot}
          >
            Go
          </button>
          {rootError && (
            <span className="topbar-root-error" title={rootError}>
              {rootError}
            </span>
          )}
        </div>
      </header>
      <div className="app-body">
        {booted && (
          <ViewShell
            key={selectedRoot}
            root={selectedRoot}
            sessionId={sessionId}
            sessionNav={sessionNav}
            onOpenSession={openSession}
            onClearSession={clearSession}
            onSessionNavChange={setSessionNav}
          />
        )}
      </div>
      <footer className="app-footer">
        <span>
          <a
            href="https://github.com/sands-lab/nika"
            target="_blank"
            rel="noreferrer"
          >
            NIKA
          </a>
          {" · "}
          <a
            href="https://sands-lab.github.io/nika/"
            target="_blank"
            rel="noreferrer"
          >
            Website
          </a>
          {" · "}
          <a
            href="https://arxiv.org/abs/2512.16381"
            target="_blank"
            rel="noreferrer"
          >
            Paper
          </a>
          {" · "}
          <a
            href="https://sands-lab.github.io/nika/leaderboard/"
            target="_blank"
            rel="noreferrer"
          >
            Leaderboard
          </a>
        </span>
        <span className="footer-ack">
          Trajectory UI inspired by{" "}
          <a
            href="https://github.com/deepseek-ai/deepseek-harness"
            target="_blank"
            rel="noreferrer"
          >
            DeepSeek Harness
          </a>
        </span>
      </footer>
    </div>
  );
}
