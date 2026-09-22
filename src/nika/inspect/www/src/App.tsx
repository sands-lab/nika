import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  CanonicalTraceEvent,
  BrowseEntry,
  formatScore,
  formatTs,
  fetchBrowse,
  fetchRaw,
  fetchRoots,
  fetchScores,
  fetchSession,
  fetchSessions,
  fetchTimeline,
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
  displayDurationMs,
  formatDuration,
  formatOverviewClock,
  formatTokenCount,
  buildLlmTurnDetail,
  extractModelName,
  layoutOverviewSpans,
  llmTokenUsage,
  OverviewLayoutMode,
  overviewLaneHeightPx,
  overviewStackBarGeometry,
  overviewTurnBoundaryPcts,
  assignLaneStackRows,
  type LaidOutOverviewSpan,
  parseLlmMessageContent,
  parseTs,
  projectOverviewSpans,
  roleLabel,
  Role,
  isToolDisplay,
} from "./roles";

type Tab = "timeline" | "agent" | "nika" | "scores" | "raw";

const RAW_FILES = [
  "run.json",
  "messages.jsonl",
  "nika.jsonl",
  "ground_truth.json",
  "submission.json",
  "eval_metrics.json",
  "llm_judge.json",
];

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


/** Inject / localization params as "host_name=dns_pod0 · intf_name=eth1". */
function sessionLocation(s: Pick<SessionSummary, "inject_params">): string | null {
  const params = s.inject_params;
  if (!params) return null;
  const entries = Object.entries(params);
  if (!entries.length) return null;
  return entries.map(([k, v]) => `${k}=${v}`).join(" · ");
}

function sessionDuration(s: SessionSummary): string {
  const start = parseTs(s.start_time);
  const end = parseTs(s.end_time);
  if (start == null || end == null || end < start) return "—";
  return formatDuration(end - start);
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
  | "location"
  | "agent"
  | "model"
  | "rca_f1"
  | "time"
  | "duration";

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
    case "location":
      return (sessionLocation(s) || "").toLowerCase();
    case "agent":
      return (s.agent_type || "").toLowerCase();
    case "model":
      return (s.model || "").toLowerCase();
    case "rca_f1":
      return s.rca_f1 ?? null;
    case "time":
      return parseTs(s.start_time);
    case "duration": {
      const start = parseTs(s.start_time);
      const end = parseTs(s.end_time);
      if (start == null || end == null || end < start) return null;
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
  onStatus: (v: string) => void;
  onTrial: (v: string) => void;
  onScenario: (v: string) => void;
  onProblem: (v: string) => void;
  onTopoSize: (v: string) => void;
  onAgent: (v: string) => void;
  onModel: (v: string) => void;
  facets: SessionFacets;
};

function SessionsTable({
  sessions,
  onOpen,
  showTrial,
  columnFilters,
}: {
  sessions: SessionSummary[];
  onOpen: (id: string) => void;
  showTrial?: boolean;
  columnFilters?: SessionColumnFilters;
}) {
  const [sortKey, setSortKey] = useState<SessionSortKey>(
    showTrial ? "trial" : "failure",
  );
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");

  const sorted = useMemo(() => {
    return [...sessions].sort((a, b) => compareSessions(a, b, sortKey, sortDir));
  }, [sessions, sortKey, sortDir]);

  const onSort = (key: SessionSortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
      return;
    }
    setSortKey(key);
    setSortDir(
      key === "rca_f1" || key === "duration" || key === "time" ? "desc" : "asc",
    );
  };

  const statusOptions = columnFilters?.facets.statuses.length
    ? columnFilters.facets.statuses
    : ["running", "finished", "aborted", "error"];

  const sortTh = (key: SessionSortKey, label: string) => {
    const active = sortKey === key;
    return (
      <th>
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

  const filterCell = (node: ReactNode) => <th className="th-filter-cell">{node}</th>;
  const filterEmpty = () => <th className="th-filter-cell" />;

  return (
    <div className="table-wrap">
      <table className="sessions">
        <thead>
          <tr className="th-sort-row">
            {sortTh("status", "Status")}
            {showTrial && sortTh("trial", "Trial")}
            {sortTh("failure", "Failure")}
            {sortTh("scenario", "Scenario")}
            {sortTh("size", "Size")}
            {sortTh("agent", "Agent")}
            {sortTh("model", "Model")}
            {sortTh("rca_f1", "RCA F1")}
            {sortTh("time", "Time")}
            {sortTh("duration", "Duration")}
            {sortTh("location", "Location")}
          </tr>
          {columnFilters ? (
            <tr className="th-filter-row">
              {filterCell(
                <select
                  className="th-filter"
                  value={columnFilters.status}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => columnFilters.onStatus(e.target.value)}
                >
                  <option value="all">All</option>
                  {statusOptions.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>,
              )}
              {showTrial &&
                filterCell(
                  <select
                    className="th-filter"
                    value={columnFilters.trial}
                    onClick={(e) => e.stopPropagation()}
                    onChange={(e) => columnFilters.onTrial(e.target.value)}
                  >
                    <option value="">All</option>
                    {(columnFilters.facets.trial_indices || []).map((idx) => (
                      <option key={idx} value={String(idx)}>
                        t{String(idx).padStart(2, "0")}
                      </option>
                    ))}
                  </select>,
                )}
              {filterCell(
                <select
                  className="th-filter"
                  value={columnFilters.problem}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => columnFilters.onProblem(e.target.value)}
                >
                  <option value="">All</option>
                  {columnFilters.facets.problems.map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>,
              )}
              {filterCell(
                <select
                  className="th-filter"
                  value={columnFilters.scenario}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => columnFilters.onScenario(e.target.value)}
                >
                  <option value="">All</option>
                  {columnFilters.facets.scenarios.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>,
              )}
              {filterCell(
                <select
                  className="th-filter"
                  value={columnFilters.topoSize}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => columnFilters.onTopoSize(e.target.value)}
                >
                  <option value="">All</option>
                  {columnFilters.facets.topo_sizes.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>,
              )}
              {filterCell(
                <select
                  className="th-filter"
                  value={columnFilters.agent}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => columnFilters.onAgent(e.target.value)}
                >
                  <option value="">All</option>
                  {columnFilters.facets.agents.map((a) => (
                    <option key={a} value={a}>
                      {a}
                    </option>
                  ))}
                </select>,
              )}
              {filterCell(
                <select
                  className="th-filter"
                  value={columnFilters.model}
                  onClick={(e) => e.stopPropagation()}
                  onChange={(e) => columnFilters.onModel(e.target.value)}
                >
                  <option value="">All</option>
                  {columnFilters.facets.models.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>,
              )}
              {filterEmpty()}
              {filterEmpty()}
              {filterEmpty()}
              {filterEmpty()}
            </tr>
          ) : null}
        </thead>
        <tbody>
          {sorted.map((s) => {
            const location = sessionLocation(s);
            return (
              <tr
                key={sessionOpenId(s)}
                onClick={() => onOpen(sessionOpenId(s))}
              >
                <td>
                  <span className={`chip ${s.status}`}>{s.status}</span>
                </td>
                {showTrial && (
                  <td>
                    {s.trial_index != null ? (
                      <span className="chip">
                        t{String(s.trial_index).padStart(2, "0")}
                      </span>
                    ) : (
                      "—"
                    )}
                  </td>
                )}
                <td>
                  <div className="session-title" title={s.session_id}>
                    {sessionTitle(s)}
                  </div>
                  {s.failure_domain ? (
                    <div className="session-sub">{s.failure_domain}</div>
                  ) : null}
                </td>
                <td>{s.scenario_name || "—"}</td>
                <td>{s.scenario_topo_size || "—"}</td>
                <td>{s.agent_type || "—"}</td>
                <td>{s.model || "—"}</td>
                <td>
                  {s.rca_f1 != null ? (
                    <span className="chip score">{formatScore(s.rca_f1)}</span>
                  ) : (
                    "—"
                  )}
                </td>
                <td title={s.start_time || undefined}>
                  <span className="session-time">{sessionShortTime(s)}</span>
                </td>
                <td>{sessionDuration(s)}</td>
                <td>
                  {location ? (
                    <span className="session-location" title={location}>
                      {location}
                    </span>
                  ) : (
                    "—"
                  )}
                </td>
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

const SIDEBAR_WIDTH_KEY = "nika-inspect-sidebar-width-v2";
const SIDEBAR_WIDTH_DEFAULT = 220;
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
  root,
  onOpenSession,
  onClearSession,
}: {
  sessionId: string | null;
  root: string;
  onOpenSession: (id: string) => void;
  onClearSession: () => void;
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
  const [status, setStatus] = useState("all");
  const [trial, setTrial] = useState("");
  const [scenario, setScenario] = useState("");
  const [agent, setAgent] = useState("");
  const [model, setModel] = useState("");
  const [problem, setProblem] = useState("");
  const [topoSize, setTopoSize] = useState("");
  const [q, setQ] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(loadTreeExpanded);
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
    let cancelled = false;
    const params = new URLSearchParams({ status });
    if (scenario) params.set("scenario", scenario);
    if (agent) params.set("agent", agent);
    if (model) params.set("model", model);
    if (problem) params.set("problem", problem);
    if (topoSize) params.set("topo_size", topoSize);
    if (trial) params.set("trial_index", trial);
    if (q.trim()) params.set("q", q.trim());
    const load = (background: boolean) => {
      if (!background) setLoading(true);
      fetchSessions(params, root)
        .then((data) => {
          if (cancelled) return;
          setSessions(data.sessions);
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
    const poll = window.setInterval(() => load(true), 3000);
    return () => {
      cancelled = true;
      window.clearInterval(poll);
    };
  }, [root, status, trial, scenario, agent, model, problem, topoSize, q]);

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
    onStatus: setStatus,
    onTrial: setTrial,
    onScenario: setScenario,
    onProblem: setProblem,
    onTopoSize: setTopoSize,
    onAgent: setAgent,
    onModel: setModel,
    facets,
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
            sessionId={sessionId}
            root={root}
            onBack={onClearSession}
          />
        ) : (
          <div className="main pane-list">
            <div className="home-toolbar">
              <div className="home-breadcrumb" title={breadcrumb}>
                {breadcrumb || "Select a folder"}
              </div>
              <input
                className="home-search"
                type="search"
                placeholder="Search sessions…"
                value={q}
                onChange={(e) => setQ(e.target.value)}
              />
            </div>
            {loading ? (
              <div className="empty">Loading sessions…</div>
            ) : selectedPath == null ? (
              <div className="empty">Select a folder on the left</div>
            ) : visibleSessions.length === 0 ? (
              <div className="empty">No sessions in this folder</div>
            ) : (
              <SessionsTable
                sessions={visibleSessions}
                onOpen={onOpenSession}
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
    return row.event === "llm" || row.kind === "llm" ? "llm" : row.event || row.title;
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

function OverviewTimeline({
  events,
  selectedId,
  onSelect,
  brush,
  onBrushChange,
}: {
  events: CanonicalTraceEvent[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  brush: TimeBrush | null;
  onBrushChange: (brush: TimeBrush | null) => void;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const plotRef = useRef<HTMLDivElement>(null);
  const wheelCleanupRef = useRef<(() => void) | null>(null);
  const dragRef = useRef<{
    pointerId: number;
    originX: number;
    originY: number;
    originPct: number;
    moved: boolean;
  } | null>(null);
  const panRef = useRef<{
    pointerId: number;
    anchorClientX: number;
    anchorStart: number;
    moved: boolean;
    pannable: boolean;
  } | null>(null);
  /** Visual brush draft in track % (equal and duration). */
  const [draftPct, setDraftPct] = useState<{ a: number; b: number } | null>(null);
  const [viewport, setViewport] = useState<TimeBrush | null>(null);
  const [panning, setPanning] = useState(false);
  const [layoutMode, setLayoutMode] = useState<OverviewLayoutMode>(loadOverviewLayout);
  const [overlapPop, setOverlapPop] = useState<OverlapPopup | null>(null);

  const setLayout = (mode: OverviewLayoutMode) => {
    setLayoutMode(mode);
    setViewport(null);
    setOverlapPop(null);
    try {
      localStorage.setItem(OVERVIEW_LAYOUT_KEY, mode);
    } catch {
      /* ignore */
    }
  };

  const spans = useMemo(() => buildOverviewSpans(events), [events]);

  // Zoom forces a timed projection; equal → actual wall-clock while zoomed.
  const paintMode: OverviewLayoutMode = viewport
    ? layoutMode === "equal"
      ? "actual"
      : layoutMode
    : layoutMode;

  // Domain spans: duration mode uses idle-compressed coordinates for zoom/pan.
  const domainSpans = useMemo(
    () => projectOverviewSpans(spans, paintMode),
    [spans, paintMode],
  );

  const fullDomain = useMemo(() => {
    if (domainSpans.length === 0) return null;
    const start = Math.min(...domainSpans.map((s) => s.startMs));
    const end = Math.max(...domainSpans.map((s) => s.endMs));
    return { start, end: Math.max(end, start + 1) };
  }, [domainSpans]);

  // Drop a stale viewport when the session domain changes.
  useEffect(() => {
    if (!fullDomain || !viewport) return;
    if (viewport.endMs < fullDomain.start || viewport.startMs > fullDomain.end) {
      setViewport(null);
    }
  }, [fullDomain, viewport]);

  const viewDomain = useMemo(() => {
    if (!fullDomain) return null;
    if (!viewport) return fullDomain;
    const fullMs = fullDomain.end - fullDomain.start;
    const dur = Math.min(fullMs, Math.max(1, viewport.endMs - viewport.startMs));
    const start = Math.min(
      Math.max(viewport.startMs, fullDomain.start),
      fullDomain.end - dur,
    );
    return { start, end: start + dur };
  }, [fullDomain, viewport]);

  const laidOut = useMemo(() => {
    if (!viewDomain) return [];
    return layoutOverviewSpans(spans, viewDomain.start, viewDomain.end, paintMode);
  }, [spans, viewDomain, paintMode]);

  const stacked = useMemo(() => assignLaneStackRows(laidOut), [laidOut]);

  const laneHeights = useMemo(() => {
    // Fixed compact lanes — overlaps stay stacked with a slight height nudge.
    return LANES.map(() => overviewLaneHeightPx());
  }, []);

  const turnBoundaries = useMemo(
    () => overviewTurnBoundaryPcts(laidOut),
    [laidOut],
  );

  const viewDomainRef = useRef(viewDomain);
  const fullDomainRef = useRef(fullDomain);
  viewDomainRef.current = viewDomain;
  fullDomainRef.current = fullDomain;

  const onWheelZoom = useCallback((event: WheelEvent) => {
    event.preventDefault();
    event.stopPropagation();
    const track = trackRef.current;
    const domain = fullDomainRef.current;
    const visible = viewDomainRef.current;
    if (!track || !domain || !visible) return;
    const fullMs = domain.end - domain.start;
    const currentMs = visible.end - visible.start;
    const rect = track.getBoundingClientRect();
    if (rect.width <= 0) return;
    const anchorFraction = Math.min(
      1,
      Math.max(0, (event.clientX - rect.left) / rect.width),
    );
    // Normalize wheel/trackpad deltas (pixels / lines / pages).
    let dy = event.deltaY;
    if (event.deltaMode === 1) dy *= 16;
    else if (event.deltaMode === 2) dy *= rect.height;
    const clamped = Math.max(-160, Math.min(160, dy));
    // Discrete-enough steps so pixel-mode trackpads still feel responsive.
    const intensity = Math.max(0.1, Math.min(1, Math.abs(clamped) / 90));
    const direction = Math.sign(clamped) || 1;
    const minMs = Math.min(fullMs, Math.max(50, fullMs * 0.002));
    const nextMs = Math.min(
      fullMs,
      Math.max(minMs, currentMs * Math.exp(direction * intensity * 0.55)),
    );
    if (nextMs >= fullMs * 0.999) {
      setViewport(null);
      return;
    }
    const anchorTime = visible.start + anchorFraction * currentMs;
    const nextStart = Math.min(
      Math.max(anchorTime - anchorFraction * nextMs, domain.start),
      domain.end - nextMs,
    );
    setViewport({ startMs: nextStart, endMs: nextStart + nextMs });
  }, []);

  // Attach non-passive wheel on the track node itself (callback ref survives
  // fullDomain object churn from polling).
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

  // Escape closes overlap zoom, then clears the brush (Harness-style).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      if (overlapPop) {
        setOverlapPop(null);
        return;
      }
      setDraftPct(null);
      onBrushChange(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onBrushChange, overlapPop]);

  const openOverlapPopup = (
    hit: LaidOutOverviewSpan,
    clientX: number,
    clientY: number,
  ) => {
    const laneItems = stacked.filter((it) => it.span.lane === hit.span.lane);
    const group = overlappingGroup(hit, laneItems);
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

  const pctAtClientX = (clientX: number): number | null => {
    const el = trackRef.current;
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) return null;
    return Math.min(100, Math.max(0, ((clientX - rect.left) / rect.width) * 100));
  };

  /** Prefer the bar under the cursor (lane + vertical stack), not the globally shortest. */
  const hitTestBar = (clientX: number, clientY: number) => {
    const el = trackRef.current;
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) return null;
    const pct = Math.min(
      100,
      Math.max(0, ((clientX - rect.left) / rect.width) * 100),
    );
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
    const laneItems = stacked.filter((it) => it.span.lane === laneId);
    const layerCount =
      laneItems.reduce((m, x) => Math.max(m, x.stackRow), 0) + 1;
    const xHits = laneItems.filter(
      (it) => pct >= it.leftPct && pct <= it.leftPct + it.widthPct,
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

  const brushFromVisualPct = (a: number, b: number): TimeBrush | null => {
    const left = Math.min(a, b);
    const right = Math.max(a, b);
    const hits = stacked.filter(
      ({ leftPct, widthPct }) => leftPct < right && leftPct + widthPct > left,
    );
    if (hits.length === 0) return null;
    return {
      startMs: Math.min(...hits.map((h) => h.span.startMs)),
      endMs: Math.max(...hits.map((h) => h.span.endMs)),
    };
  };

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!viewDomain || !fullDomain) return;

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

    const pct = pctAtClientX(e.clientX);
    if (pct == null) return;
    dragRef.current = {
      pointerId: e.pointerId,
      originX: e.clientX,
      originY: e.clientY,
      originPct: pct,
      moved: false,
    };
    setDraftPct(null);
    e.currentTarget.setPointerCapture(e.pointerId);
  };

  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (!viewDomain || !fullDomain) return;
    const rect = e.currentTarget.getBoundingClientRect();

    const pan = panRef.current;
    if (pan && pan.pointerId === e.pointerId) {
      if (Math.abs(e.clientX - pan.anchorClientX) >= 4) pan.moved = true;
      if (!pan.pannable || rect.width <= 0) return;
      const viewMs = viewDomain.end - viewDomain.start;
      const delta = (e.clientX - pan.anchorClientX) / rect.width;
      const nextStart = Math.min(
        Math.max(pan.anchorStart - delta * viewMs, fullDomain.start),
        fullDomain.end - viewMs,
      );
      setViewport({ startMs: nextStart, endMs: nextStart + viewMs });
      return;
    }

    const drag = dragRef.current;
    if (!drag || drag.pointerId !== e.pointerId) return;
    const pct = pctAtClientX(e.clientX);
    if (pct == null) return;
    if (!drag.moved && Math.abs(e.clientX - drag.originX) < 4) return;
    drag.moved = true;
    setOverlapPop(null);
    setDraftPct({ a: drag.originPct, b: pct });
  };

  const onPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const pan = panRef.current;
    if (pan && pan.pointerId === e.pointerId) {
      const moved =
        pan.moved || Math.abs(e.clientX - pan.anchorClientX) >= 4;
      panRef.current = null;
      setPanning(false);
      try {
        e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        /* already released */
      }
      if (!moved) {
        setDraftPct(null);
        onBrushChange(null);
      }
      return;
    }

    const drag = dragRef.current;
    if (!drag || drag.pointerId !== e.pointerId || !viewDomain) return;
    dragRef.current = null;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* already released */
    }

    if (drag.moved) {
      const pct = pctAtClientX(e.clientX) ?? drag.originPct;
      const left = Math.min(drag.originPct, pct);
      const right = Math.max(drag.originPct, pct);
      if (right - left >= 0.8) {
        const next = brushFromVisualPct(left, right);
        if (next) onBrushChange(next);
      }
      setDraftPct(null);
      return;
    }

    setDraftPct(null);
    // Click without drag: lane + vertical stack aware (so long underlays stay selectable).
    const hit = hitTestBar(drag.originX, drag.originY);
    if (hit) openOverlapPopup(hit, drag.originX, drag.originY);
    else setOverlapPop(null);
  };

  const onContextMenu = (e: ReactMouseEvent) => {
    e.preventDefault();
  };

  if (!viewDomain || !fullDomain) {
    return <div className="overview empty-inline">No timed events</div>;
  }

  const byLane = (lane: OverviewLane) =>
    stacked.filter(({ span }) => span.lane === lane);

  let brushLeftPct: number | null = null;
  let brushRightPct: number | null = null;
  if (draftPct) {
    brushLeftPct = Math.min(draftPct.a, draftPct.b);
    brushRightPct = Math.max(draftPct.a, draftPct.b);
  } else if (brush) {
    const hits = stacked.filter(({ span }) =>
      overlapsBrush(span.startMs, span.endMs, brush),
    );
    if (hits.length > 0) {
      brushLeftPct = Math.min(...hits.map((h) => h.leftPct));
      brushRightPct = Math.max(...hits.map((h) => h.leftPct + h.widthPct));
    }
  }
  const brushStyle: CSSProperties | undefined =
    brushLeftPct != null && brushRightPct != null
      ? {
          left: `${Math.max(0, brushLeftPct)}%`,
          width: `${Math.max(0, Math.min(100, brushRightPct) - Math.max(0, brushLeftPct))}%`,
        }
      : undefined;

  const zoomed = viewport != null;
  const timedPaint = paintMode === "duration" || paintMode === "actual";

  return (
    <div className="overview">
      <div className="overview-toolbar">
        <span className="overview-hint">
          Scroll to zoom · drag to select · click overlap to zoom · Esc to clear
          {zoomed ? " · right-drag to pan" : ""}
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
          className={`overview-track${draftPct ? " brushing" : ""}${panning ? " panning" : ""}${timedPaint ? " duration-mode" : ""}`}
          style={{ gridTemplateRows: laneHeights.map((h) => `${h}px`).join(" ") }}
          onPointerDown={(e) => {
            setOverlapPop(null);
            onPointerDown(e);
          }}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={() => {
            dragRef.current = null;
            panRef.current = null;
            setDraftPct(null);
            setPanning(false);
          }}
          onContextMenu={onContextMenu}
          onDoubleClick={(e) => {
            e.preventDefault();
            setViewport(null);
            onBrushChange(null);
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
                {laneItems.map(({ span, leftPct, widthPct, stackRow }) => {
                  const spanRightPct = leftPct + Math.max(widthPct, 0);
                  const dimmedByBrush =
                    brushLeftPct != null &&
                    brushRightPct != null &&
                    !(leftPct < brushRightPct && spanRightPct > brushLeftPct);
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
                        minWidth: widthPct > 0 ? "1px" : 0,
                        ["--stack-depth" as string]: depth,
                        zIndex:
                          selectedId === span.eventId
                            ? 30
                            : 5 + stackRow,
                      }}
                      onPointerDown={(ev) => {
                        // Select / open overlap zoom; don't start a track brush.
                        if (ev.button !== 0) return;
                        ev.stopPropagation();
                        openOverlapPopup(
                          { span, leftPct, widthPct, stackRow },
                          ev.clientX,
                          ev.clientY,
                        );
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
                        onSelect(it.span.eventId);
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
                    onClick={() => onSelect(it.span.eventId)}
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

function Ledger({
  rows,
  selectedId,
  onSelect,
}: {
  rows: DisplayEvent[];
  selectedId: string | null;
  onSelect: (row: DisplayEvent) => void;
}) {
  const rowRefs = useRef<Map<string, HTMLTableRowElement>>(new Map());

  useEffect(() => {
    if (!selectedId) return;
    rowRefs.current.get(selectedId)?.scrollIntoView({
      block: "nearest",
      behavior: "smooth",
    });
  }, [selectedId]);

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
                </td>
                <td className="time-cell">{formatTs(row.timestamp)}</td>
                <td className="dur-cell">
                  {row.durationMs != null ? formatDuration(row.durationMs) : "—"}
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

function stringify(value: unknown): string {
  return formatPretty(value);
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
  const normalized = normalizeDisplayValue(value);
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

  const text = formatPretty(normalized);
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
        </header>
        {!hasOutput && (
          <div className="llm-empty">No text or tool calls recorded for this call.</div>
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

function Inspector({
  row,
  rows,
  sessionModel,
  onClose,
  onOpenEvent,
}: {
  row: DisplayEvent | null;
  rows: DisplayEvent[];
  sessionModel?: string | null;
  onClose: () => void;
  onOpenEvent: (id: string) => void;
}) {
  const role: Role | null = row?.role ?? null;
  const isTool = row ? isToolDisplay(row) : false;
  const isAssistant = role === "assistant";
  const tabs = isTool
    ? (["overview", "parameters", "results", "raw", "timing"] as const)
    : isAssistant
      ? (["overview", "preview", "raw", "timing"] as const)
      : (["overview", "payload", "raw", "timing"] as const);
  const [tab, setTab] = useState<string>(isAssistant ? "preview" : "overview");

  useEffect(() => {
    setTab(isAssistant ? "preview" : "overview");
  }, [row?.id, isAssistant]);

  if (!row || !role) {
    return null;
  }

  const start = row.start;
  const end = row.end;
  const duration = displayDurationMs(row);
  const tokens = isAssistant ? llmTokenUsage(row) : null;
  const modelName = isAssistant
    ? extractModelName(start.raw.model, end?.raw.model, start.raw, end?.raw, sessionModel)
    : null;
  const status = row.error ? "Error" : end || !isTool && !isAssistant ? "Completed" : end ? "Completed" : "Running";
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
                {row.event || row.kind}
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
            <dt>Phase</dt>
            <dd>{row.phase || "—"}</dd>
            <dt>Start</dt>
            <dd className="mono">{row.timestamp || "—"}</dd>
            <dt>End</dt>
            <dd className="mono">{row.endTimestamp || "—"}</dd>
            <dt>Duration</dt>
            <dd>{formatDuration(duration)}</dd>
            {isAssistant && (
              <>
                <dt>Model</dt>
                <dd>{modelName || "—"}</dd>
                <dt>Input tokens</dt>
                <dd className="mono">{formatTokenCount(tokens?.input ?? null)}</dd>
                <dt>Output tokens</dt>
                <dd className="mono">{formatTokenCount(tokens?.output ?? null)}</dd>
                <dt>Total tokens</dt>
                <dd className="mono">{formatTokenCount(tokens?.total ?? null)}</dd>
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

        {tab === "parameters" && <PrettyValue value={toolParameters} />}

        {tab === "results" && (
          <div className="inspector-pane">
            <div className="inspector-pane-label">
              Tool <code>{toolName || "—"}</code>
            </div>
            <PrettyValue value={toolResults ?? row.summary} />
          </div>
        )}

        {tab === "preview" && isAssistant && (
          <LlmTurnPreview
            row={row}
            rows={rows}
            sessionModel={sessionModel}
            onOpenTool={onOpenEvent}
          />
        )}

        {tab === "preview" && !isAssistant && (
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

        {tab === "timing" && (
          <dl className="kv">
            <dt>Start time</dt>
            <dd className="mono">{row.timestamp || "—"}</dd>
            <dt>End time</dt>
            <dd className="mono">{row.endTimestamp || "—"}</dd>
            <dt>Duration</dt>
            <dd>{formatDuration(duration)}</dd>
            <dt>Timing source</dt>
            <dd>
              {row.event === "llm" || row.kind === "llm"
                ? row.start.event === "turn.started"
                  ? "turn.started → turn.completed"
                  : "llm_start → llm_end"
                : isTool && end
                  ? "tool_start → tool_end"
                  : "Session timestamps"}
            </dd>
            <dt>Start epoch ms</dt>
            <dd className="mono">{parseTs(row.timestamp) ?? "—"}</dd>
            <dt>End epoch ms</dt>
            <dd className="mono">{parseTs(row.endTimestamp) ?? "—"}</dd>
          </dl>
        )}
      </div>
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

function pairKey(p: RootCausePair): string {
  return `${p.resourceId}\0${p.faultType}`;
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
    const pairOk =
      row.gt && row.pred
        ? pairKey(row.gt) === pairKey(row.pred)
        : false;
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
      {
        key: `pair-${idx}`,
        field: `rca_pair${suffix}`,
        expected: row.gt ? `${row.gt.resourceId} + ${row.gt.faultType}` : "—",
        actual: row.pred
          ? `${row.pred.resourceId} + ${row.pred.faultType}`
          : "—",
        ok: pairOk,
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
  onBack,
}: {
  sessionId: string;
  root: string;
  onBack: () => void;
}) {
  const [tab, setTab] = useState<Tab>("timeline");
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [events, setEvents] = useState<CanonicalTraceEvent[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [brush, setBrush] = useState<TimeBrush | null>(null);
  const [scores, setScores] = useState<ScoresResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const rows = useMemo(() => {
    const collapsed = collapsePairedEvents(events);
    return [...collapsed].sort((a, b) => {
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
  }, [events]);
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
    if (!selectedId) return;
    if (visibleRows.some((r) => r.id === selectedId)) return;
    // Keep overview/ledger selection reachable: drop brush instead of the pick.
    if (brush && rows.some((r) => r.id === selectedId)) {
      setBrush(null);
      return;
    }
    setSelectedId(null);
  }, [visibleRows, selectedId, brush, rows]);

  useEffect(() => {
    let cancelled = false;
    let finished = false;
    const load = (background: boolean) =>
      fetchSession(sessionId, root)
        .then((d) => {
          if (cancelled) return;
          setDetail(d);
          finished = d.status !== "running";
        })
        .catch((err: Error) => {
          if (!cancelled && !background) setError(err.message);
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

  useEffect(() => {
    if (tab === "scores" || tab === "raw") return;
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
    if (tab === "scores" || tab === "raw") return;
    const source =
      tab === "agent" ? "agent" : tab === "nika" ? "nika" : "merged";
    let cancelled = false;
    const tick = () =>
      fetchTimeline(sessionId, source, root)
        .then((data) => {
          if (!cancelled) setEvents(data.events);
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
          {detail && sessionLocation(detail) && (
            <div className="detail-sub">{sessionLocation(detail)}</div>
          )}
        </div>
        {detail && <span className={`chip ${detail.status}`}>{detail.status}</span>}
        {detail?.is_benchmark && detail.benchmark_label && (
          <span className="chip">benchmark {detail.benchmark_label}</span>
        )}
        {detail?.is_benchmark && detail.trial_index != null && (
          <span className="chip">
            trial t{String(detail.trial_index).padStart(2, "0")}
          </span>
        )}
        {detail?.rca_f1 != null && (
          <span className="chip score">rca_f1 {formatScore(detail.rca_f1)}</span>
        )}
        {(tab === "timeline" || tab === "agent" || tab === "nika") && (
          <div className="stat-row">
            <span>{stats.total} events</span>
            <span className="role-nika">{stats.nika} nika</span>
            <span className="role-assistant">{stats.model} agent</span>
            <span className="role-tool">{stats.tools} tools</span>
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
      {error && <div className="empty">{error}</div>}
      {!error && (tab === "timeline" || tab === "agent" || tab === "nika") && (
        <div className="trace-layout">
          {tab === "timeline" && (
            <OverviewTimeline
              events={events}
              selectedId={selectedId}
              onSelect={(id) => {
                setSelectedId(id);
                setBrush(null);
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
                />
              ) : null
            }
          />
        </div>
      )}
      {!error && tab === "scores" && <ScoresPanel scores={scores} />}
      {!error && tab === "raw" && (
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
  const [sessionId, setSessionId] = useState<string | null>(null);
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

  useEffect(() => {
    let cancelled = false;
    const boot = async () => {
      const cached = loadCachedResultsRoot();
      if (cached) {
        try {
          const data = await fetchRoots(cached);
          if (cancelled) return;
          const { selectedRoot: next, activePath: path } = selectionFromRoots(
            cached,
            data,
          );
          setBaseRoot(data.base_root);
          setSelectedRoot(next);
          selectedRootRef.current = next;
          setActivePath(path);
          setDraftPath(path);
          saveCachedResultsRoot(path);
          return;
        } catch {
          // Cached folder gone or outside allowed roots — fall back.
          clearCachedResultsRoot();
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
    void boot();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!browserOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (!rootPickerRef.current?.contains(e.target as Node)) {
        setBrowserOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setBrowserOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [browserOpen]);

  const loadBrowse = (path: string) => {
    setBrowseLoading(true);
    setBrowseError(null);
    fetchBrowse(path || undefined)
      .then((data) => {
        setBrowsePath(data.path);
        setBrowseParent(data.parent ?? null);
        setBrowseEntries(data.entries);
        if (!baseRoot) setBaseRoot(data.base_root);
      })
      .catch((err: Error) => {
        setBrowseError(err.message);
        setBrowseEntries([]);
      })
      .finally(() => setBrowseLoading(false));
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
      const data = await fetchRoots(trimmed);
      const { selectedRoot: next, activePath: path } = selectionFromRoots(
        trimmed,
        data,
      );
      setBaseRoot(data.base_root);
      setSelectedRoot(next);
      selectedRootRef.current = next;
      setSessionId(null);
      setRootError(null);
      setActivePath(path);
      setDraftPath(path);
      saveCachedResultsRoot(path);
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
        <ViewShell
          key={selectedRoot}
          root={selectedRoot}
          sessionId={sessionId}
          onOpenSession={setSessionId}
          onClearSession={() => setSessionId(null)}
        />
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
