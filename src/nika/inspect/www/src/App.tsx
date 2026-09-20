import { useEffect, useMemo, useRef, useState, type CSSProperties, type MouseEvent as ReactMouseEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  CanonicalTraceEvent,
  formatScore,
  formatTs,
  fetchRaw,
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
  formatTokenCount,
  buildLlmTurnDetail,
  extractModelName,
  layoutOverviewSpans,
  llmTokenUsage,
  OverviewLayoutMode,
  parseLlmMessageContent,
  parseTs,
  roleLabel,
  Role,
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
  return startMs < brush.endMs && end > brush.startMs;
}

function rowInBrush(row: DisplayEvent, brush: TimeBrush): boolean {
  return overlapsBrush(parseTs(row.timestamp), parseTs(row.endTimestamp), brush);
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
    : ["running", "finished"];

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

const SIDEBAR_WIDTH_KEY = "nika-inspect-sidebar-width";
const SIDEBAR_WIDTH_DEFAULT = 300;
const SIDEBAR_WIDTH_MIN = 180;
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
  onOpenSession,
  onClearSession,
}: {
  sessionId: string | null;
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
  const [selectedMembers, setSelectedMembers] = useState<string[] | null>(null);
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
    setLoading(true);
    fetchSessions(params)
      .then((data) => {
        if (cancelled) return;
        setSessions(data.sessions);
        if (data.facets) setFacets(data.facets);
        setResultsRoot(data.results_root);
        setError(null);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [status, trial, scenario, agent, model, problem, topoSize, q]);

  const tree = useMemo(() => buildPathTree(sessions), [sessions]);

  useEffect(() => {
    if (selectedPath != null || loading || !tree.length) return;
    setSelectedPath(tree[0].path);
    setSelectedMembers(tree[0].memberKeys ?? null);
    setExpanded((prev) => {
      const next = new Set(prev);
      next.add(tree[0].path);
      saveTreeExpanded(next);
      return next;
    });
  }, [selectedPath, loading, tree]);

  const visibleSessions = useMemo(
    () =>
      selectedPath == null
        ? []
        : sessionsUnderSelection(sessions, selectedPath, selectedMembers),
    [sessions, selectedPath, selectedMembers],
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
    setSelectedMembers(node.memberKeys ?? null);
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
  if (row.role === "tool") return row.title || "tool";
  if (row.role === "nika") return row.event?.replaceAll("_", " ") || row.title;
  if (row.role === "assistant") {
    return row.event === "llm" || row.kind === "llm" ? "llm" : row.event || row.title;
  }
  return row.event || row.title || "—";
}

const OVERVIEW_LAYOUT_KEY = "nika-inspect-overview-layout";

function loadOverviewLayout(): OverviewLayoutMode {
  try {
    const raw = localStorage.getItem(OVERVIEW_LAYOUT_KEY);
    if (raw === "equal" || raw === "duration") return raw;
  } catch {
    /* ignore */
  }
  return "equal";
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
  onSelect: (id: string) => void;
  brush: TimeBrush | null;
  onBrushChange: (brush: TimeBrush | null) => void;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{
    pointerId: number;
    originX: number;
    originMs: number;
    moved: boolean;
  } | null>(null);
  const panRef = useRef<{
    pointerId: number;
    anchorClientX: number;
    anchorStart: number;
    moved: boolean;
    pannable: boolean;
  } | null>(null);
  const [draft, setDraft] = useState<TimeBrush | null>(null);
  const [viewport, setViewport] = useState<TimeBrush | null>(null);
  const [panning, setPanning] = useState(false);
  const [layoutMode, setLayoutMode] = useState<OverviewLayoutMode>(loadOverviewLayout);

  const setLayout = (mode: OverviewLayoutMode) => {
    setLayoutMode(mode);
    try {
      localStorage.setItem(OVERVIEW_LAYOUT_KEY, mode);
    } catch {
      /* ignore */
    }
  };

  const spans = useMemo(() => buildOverviewSpans(events), [events]);
  const fullDomain = useMemo(() => {
    if (spans.length === 0) return null;
    const start = Math.min(...spans.map((s) => s.startMs));
    const end = Math.max(...spans.map((s) => s.endMs));
    return { start, end: Math.max(end, start + 1) };
  }, [spans]);

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
    // Zoom/pan is a time-domain transform (DeepSeek-style). Force duration
    // layout while zoomed so equal-mode doesn't reshuffle chip order.
    const mode: OverviewLayoutMode = viewport ? "duration" : layoutMode;
    return layoutOverviewSpans(spans, viewDomain.start, viewDomain.end, mode);
  }, [spans, viewDomain, layoutMode, viewport]);

  const viewDomainRef = useRef(viewDomain);
  const fullDomainRef = useRef(fullDomain);
  viewDomainRef.current = viewDomain;
  fullDomainRef.current = fullDomain;

  // Non-passive wheel zoom (DeepSeek Harness-style), anchored under the cursor.
  // Depend on fullDomain so we attach after the track mounts (events load async).
  useEffect(() => {
    const track = trackRef.current;
    if (!track || !fullDomain) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      event.stopPropagation();
      const domain = fullDomainRef.current;
      const visible = viewDomainRef.current;
      if (!domain || !visible) return;
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
      const clamped = Math.max(-120, Math.min(120, dy));
      const minMs = Math.min(fullMs, Math.max(50, fullMs * 0.002));
      const nextMs = Math.min(
        fullMs,
        Math.max(minMs, currentMs * Math.exp(clamped * 0.008)),
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
    };
    track.addEventListener("wheel", onWheel, { passive: false });
    return () => track.removeEventListener("wheel", onWheel);
  }, [fullDomain]);

  const pctAtClientX = (clientX: number): number | null => {
    const el = trackRef.current;
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0) return null;
    return Math.min(100, Math.max(0, ((clientX - rect.left) / rect.width) * 100));
  };

  const msAtClientX = (clientX: number): number | null => {
    if (!viewDomain) return null;
    const pct = pctAtClientX(clientX);
    if (pct == null) return null;
    return viewDomain.start + (pct / 100) * (viewDomain.end - viewDomain.start);
  };

  const brushFromPts = (a: number, b: number): TimeBrush => {
    const startMs = Math.min(a, b);
    const endMs = Math.max(a, b);
    return { startMs, endMs: Math.max(endMs, startMs + 1) };
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

    const ms = msAtClientX(e.clientX);
    if (ms == null) return;
    dragRef.current = {
      pointerId: e.pointerId,
      originX: e.clientX,
      originMs: ms,
      moved: false,
    };
    setDraft(null);
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
    const ms = msAtClientX(e.clientX);
    if (ms == null) return;
    if (!drag.moved && Math.abs(e.clientX - drag.originX) < 4) return;
    drag.moved = true;
    setDraft(brushFromPts(drag.originMs, ms));
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
        setDraft(null);
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
      const ms = msAtClientX(e.clientX) ?? drag.originMs;
      const next = brushFromPts(drag.originMs, ms);
      const minMs = (viewDomain.end - viewDomain.start) * 0.008;
      if (next.endMs - next.startMs >= minMs) {
        onBrushChange(next);
      }
      setDraft(null);
      return;
    }

    setDraft(null);
    // Click without drag: hit painted bars (equal mode uses visual slots).
    if (layoutMode === "equal") {
      const el = trackRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const pct = ((drag.originX - rect.left) / rect.width) * 100;
      const hit = [...laidOut]
        .filter(({ leftPct, widthPct }) => pct >= leftPct && pct <= leftPct + widthPct)
        .sort((a, b) => a.widthPct - b.widthPct)[0];
      if (hit) onSelect(hit.span.eventId);
      return;
    }
    const ms = drag.originMs;
    const hits = spans
      .filter(
        (s) =>
          s.startMs <= ms &&
          ms <= s.endMs &&
          s.endMs > viewDomain.start &&
          s.startMs < viewDomain.end,
      )
      .sort(
        (a, b) => a.endMs - a.startMs - (b.endMs - b.startMs) || a.id.localeCompare(b.id),
      );
    if (hits[0]) onSelect(hits[0].eventId);
  };

  const onContextMenu = (e: ReactMouseEvent) => {
    e.preventDefault();
  };

  if (!viewDomain || !fullDomain) {
    return <div className="overview empty-inline">No timed events</div>;
  }

  const domainMs = viewDomain.end - viewDomain.start;
  const byLane = (lane: OverviewLane) =>
    laidOut.filter(({ span }) => span.lane === lane);

  const activeBrush = draft ?? brush;
  let brushLeftPct: number | null = null;
  let brushRightPct: number | null = null;
  if (activeBrush) {
    if (layoutMode === "equal") {
      const hits = laidOut.filter(({ span }) =>
        overlapsBrush(span.startMs, span.endMs, activeBrush),
      );
      if (hits.length > 0) {
        brushLeftPct = Math.min(...hits.map((h) => h.leftPct));
        brushRightPct = Math.max(...hits.map((h) => h.leftPct + h.widthPct));
      }
    } else {
      brushLeftPct = ((activeBrush.startMs - viewDomain.start) / domainMs) * 100;
      brushRightPct = ((activeBrush.endMs - viewDomain.start) / domainMs) * 100;
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

  return (
    <div className="overview">
      <div className="overview-toolbar">
        <span className="overview-hint">
          {zoomed
            ? "Scroll to zoom · drag to select · right-drag to pan · right-click to clear"
            : "Scroll to zoom · drag to select · right-click to clear"}
        </span>
        {zoomed && (
          <button
            type="button"
            className="overview-reset-zoom"
            onClick={() => setViewport(null)}
          >
            Reset zoom
          </button>
        )}
        <button
          type="button"
          className={`overview-duration-toggle${layoutMode === "duration" ? " active" : ""}`}
          aria-pressed={layoutMode === "duration"}
          title={
            layoutMode === "duration"
              ? "Showing true execution duration — click for equal width"
              : "Equal-width bars in chronological order — click for true duration"
          }
          onClick={() =>
            setLayout(layoutMode === "duration" ? "equal" : "duration")
          }
        >
          By duration
        </button>
      </div>
      <div className="overview-plot">
        <div className="overview-labels">
          {LANES.map((l) => (
            <div key={l.id} className="overview-label">
              {l.label}
            </div>
          ))}
        </div>
        <div
          ref={trackRef}
          className={`overview-track${draft ? " brushing" : ""}${panning ? " panning" : ""}${layoutMode === "duration" ? " duration-mode" : ""}`}
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
            onBrushChange(null);
          }}
        >
          {LANES.map((l) => (
            <div key={l.id} className="overview-lane">
              {byLane(l.id).map(({ span, leftPct, widthPct }, index) => {
                const spanMs = Math.max(1, span.endMs - span.startMs);
                const spanRightPct = leftPct + Math.max(widthPct, 0);
                const dimmed =
                  brushLeftPct != null &&
                  brushRightPct != null &&
                  !(leftPct < brushRightPct && spanRightPct > brushLeftPct);
                return (
                  <div
                    key={span.id}
                    title={`${span.label} · ${formatDuration(spanMs)}`}
                    className={[
                      "overview-span",
                      `role-${span.role}`,
                      span.error ? "error" : "",
                      selectedId === span.eventId ? "selected" : "",
                      dimmed ? "dimmed" : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                    style={{
                      left: `${leftPct}%`,
                      width: `${Math.max(widthPct, 0)}%`,
                      minWidth: widthPct > 0 ? "1px" : 0,
                      zIndex: selectedId === span.eventId ? 5 : index + 1,
                    }}
                  />
                );
              })}
            </div>
          ))}
          {brushStyle && <div className="overview-brush" style={brushStyle} />}
        </div>
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
      return value.map((item) => normalizeDisplayValue(item, depth + 1));
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
    return normalized.trim() ? normalized : "—";
  }
  try {
    return JSON.stringify(normalized, null, 2);
  } catch {
    return String(normalized);
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

function PrettyValue({ value }: { value: unknown }) {
  const normalized = normalizeDisplayValue(value);

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
  const isTool = role === "tool";
  const isAssistant = role === "assistant";
  const tabs = isTool
    ? (["overview", "parameters", "results", "raw", "timing"] as const)
    : isAssistant
      ? (["overview", "preview", "raw", "timing"] as const)
      : (["overview", "data", "raw", "timing"] as const);
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

        {tab === "data" && (
          <PrettyValue value={start.raw.data ?? start.raw.message ?? start.raw} />
        )}

        {tab === "raw" && (
          <PrettyValue
            value={end ? { start: start.raw, end: end.raw } : start.raw}
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
                ? "llm_start → llm_end"
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

function RawPanel({ sessionId }: { sessionId: string }) {
  const [filename, setFilename] = useState("run.json");
  const [data, setData] = useState<unknown>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchRaw(sessionId, filename)
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
  }, [sessionId, filename]);

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
  onBack,
}: {
  sessionId: string;
  onBack: () => void;
}) {
  const [tab, setTab] = useState<Tab>("timeline");
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [events, setEvents] = useState<CanonicalTraceEvent[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [brush, setBrush] = useState<TimeBrush | null>(null);
  const [scores, setScores] = useState<ScoresResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const rows = useMemo(() => collapsePairedEvents(events), [events]);
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
  }, [sessionId]);

  useEffect(() => {
    if (!selectedId) return;
    if (visibleRows.some((r) => r.id === selectedId)) return;
    setSelectedId(null);
  }, [visibleRows, selectedId]);

  useEffect(() => {
    let cancelled = false;
    fetchSession(sessionId)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  useEffect(() => {
    if (tab === "scores" || tab === "raw") return;
    let cancelled = false;
    const source =
      tab === "agent" ? "agent" : tab === "nika" ? "nika" : "merged";
    fetchTimeline(sessionId, source)
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
  }, [sessionId, tab]);

  useEffect(() => {
    if (tab === "scores" || tab === "raw") return;
    if (detail?.status !== "running") return;
    const source =
      tab === "agent" ? "agent" : tab === "nika" ? "nika" : "merged";
    const poll = window.setInterval(() => {
      fetchTimeline(sessionId, source).then((data) =>
        setEvents(data.events),
      );
    }, 3000);
    return () => window.clearInterval(poll);
  }, [sessionId, tab, detail?.status]);

  useEffect(() => {
    if (tab !== "scores") return;
    let cancelled = false;
    fetchScores(sessionId)
      .then((data) => {
        if (!cancelled) setScores(data);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, tab]);

  const stats = useMemo(() => {
    const nika = visibleRows.filter((r) => r.role === "nika").length;
    const tools = visibleRows.filter((r) => r.role === "tool").length;
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
              onSelect={setSelectedId}
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
        <RawPanel sessionId={sessionId} />
      )}
    </div>
  );
}

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [resultsHint, setResultsHint] = useState("");

  useEffect(() => {
    fetchSessions(new URLSearchParams({ status: "all" }))
      .then((data) => setResultsHint(data.results_root))
      .catch(() => undefined);
  }, []);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          NIKA <span>View</span>
        </div>
        <div className="topbar-meta" title={resultsHint || undefined}>
          {resultsHint || "results/"}
        </div>
      </header>
      <div className="app-body">
        <ViewShell
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
