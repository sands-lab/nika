/**
 * Session-level timing analysis for the Overview tab.
 *
 * Everything here derives from the merged timeline (``nika.jsonl`` +
 * ``messages.jsonl``) already loaded for the Timeline tab, so no extra API
 * surface is needed and live sessions update as the logs grow.
 */
import type { CanonicalTraceEvent } from "./api";
import {
  buildOverviewSpans,
  parseTs,
  type DisplayEvent,
  type OverviewSpan,
} from "./roles.ts";

export interface Interval {
  start: number;
  end: number;
}

/** Merge overlapping intervals, optionally clipped to ``clip``. */
export function mergeIntervals(intervals: Interval[], clip?: Interval): Interval[] {
  const clipped = intervals
    .map((iv) =>
      clip
        ? { start: Math.max(iv.start, clip.start), end: Math.min(iv.end, clip.end) }
        : iv,
    )
    .filter((iv) => Number.isFinite(iv.start) && Number.isFinite(iv.end) && iv.end > iv.start)
    .sort((a, b) => a.start - b.start);
  const out: Interval[] = [];
  for (const iv of clipped) {
    const last = out[out.length - 1];
    if (last && iv.start <= last.end) last.end = Math.max(last.end, iv.end);
    else out.push({ ...iv });
  }
  return out;
}

/** Total covered length of the union of ``intervals`` (ms). */
export function unionMs(intervals: Interval[], clip?: Interval): number {
  return mergeIntervals(intervals, clip).reduce((sum, iv) => sum + (iv.end - iv.start), 0);
}

export type SessionShareKey = "setup" | "inject" | "agent" | "teardown" | "other";
export type AgentShareKey = "llm" | "tools" | "sandbox" | "overhead";

export interface TimeShare<K extends string = string> {
  key: K;
  label: string;
  ms: number;
  /** Share of the bar total, 0–1. */
  pct: number;
  hint: string;
}

export interface SessionTimeBreakdown {
  /** Session wall clock (first known activity → end or now). */
  totalMs: number;
  live: boolean;
  session: TimeShare<SessionShareKey>[];
  /** ``agent_start`` → ``agent_end`` window; ``null`` when the agent never started. */
  agentMs: number | null;
  agent: TimeShare<AgentShareKey>[];
}

const SESSION_SHARES: Record<SessionShareKey, { label: string; hint: string }> = {
  setup: {
    label: "Lab setup",
    hint: "env_start: deploy the lab and wait for verification",
  },
  inject: {
    label: "Fault inject",
    hint: "failure_inject_complete: inject and verify the failure",
  },
  agent: { label: "Agent run", hint: "agent_start → agent_end" },
  teardown: { label: "Lab teardown", hint: "env_stop: undeploy the lab" },
  other: {
    label: "Other",
    hint: "Gaps between phases: ground truth, rechecks, scoring",
  },
};

const AGENT_SHARES: Record<AgentShareKey, { label: string; hint: string }> = {
  llm: {
    label: "LLM requests",
    hint: "Model request time outside tool calls (Codex turns include their tool calls)",
  },
  tools: { label: "Tool execution", hint: "Tool call → result wall clock" },
  sandbox: {
    label: "Sandbox setup",
    hint: "sandbox_start: create the agent sandbox container",
  },
  overhead: {
    label: "Agent overhead",
    hint: "Everything else inside the agent run: CLI startup, MCP attach, parsing, idle",
  },
};

function isFinite_(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Logged operation interval: NIKA timestamps mark the end, ``duration_ms`` backdates. */
function nikaInterval(ev: CanonicalTraceEvent | null): Interval | null {
  const end = parseTs(ev?.timestamp);
  if (end == null) return null;
  const dur = ev?.duration_ms;
  if (!isFinite_(dur) || dur <= 0) return null;
  return { start: end - dur, end };
}

function firstNika(events: CanonicalTraceEvent[], ...names: string[]): CanonicalTraceEvent | null {
  for (const name of names) {
    const hit = events.find((ev) => ev.source === "nika" && ev.event === name);
    if (hit) return hit;
  }
  return null;
}

function lastNika(events: CanonicalTraceEvent[], ...names: string[]): CanonicalTraceEvent | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const ev = events[i]!;
    if (ev.source === "nika" && names.includes(ev.event ?? "")) return ev;
  }
  return null;
}

function shares<K extends string>(
  parts: { key: K; ms: number; label: string; hint: string }[],
  totalMs: number,
): TimeShare<K>[] {
  return parts
    .map((p) => ({ ...p, ms: Math.max(0, p.ms) }))
    .filter((p) => p.ms > 0)
    .map((p) => ({ ...p, pct: totalMs > 0 ? p.ms / totalMs : 0 }));
}

/**
 * Where the session's wall clock went, at two levels:
 *
 * - session: lab setup / fault inject / agent run / teardown / other
 * - agent run: LLM requests / tool execution / sandbox setup / overhead
 *
 * The agent-level parts are disjoint by construction (tools win over LLM,
 * LLM over sandbox), so each bar sums to its total.
 */
export function sessionTimeBreakdown(
  events: CanonicalTraceEvent[],
  opts: {
    startMs?: number | null;
    endMs?: number | null;
    live: boolean;
    nowMs?: number;
    spans?: OverviewSpan[];
  },
): SessionTimeBreakdown | null {
  const nowMs = opts.nowMs ?? Date.now();
  const spans = opts.spans ?? buildOverviewSpans(events);
  const setup = nikaInterval(firstNika(events, "env_start"));
  const inject = nikaInterval(
    firstNika(events, "failure_inject_complete", "failure_injected"),
  );
  const teardown = nikaInterval(lastNika(events, "env_stop"));
  const sandbox = nikaInterval(firstNika(events, "sandbox_start"));

  // Agent window: NIKA bookends, else the agent log's own extent.
  const agentStartEv = firstNika(events, "agent_start");
  const agentEndEv = lastNika(events, "agent_end", "agent_error");
  const agentEvents = events.filter((ev) => ev.source === "agent");
  let agentStart = parseTs(agentStartEv?.timestamp);
  if (agentStart == null && agentEvents.length) {
    agentStart = Math.min(
      ...agentEvents.map((ev) => parseTs(ev.timestamp)).filter(isFinite_),
    );
  }
  let agentEnd = parseTs(agentEndEv?.timestamp);
  if (agentEnd == null && agentStart != null) {
    if (opts.live) agentEnd = nowMs;
    else {
      const ends = spans
        .filter((s) => s.lane !== "nika")
        .map((s) => s.endMs)
        .concat(agentEvents.map((ev) => parseTs(ev.timestamp)).filter(isFinite_));
      agentEnd = ends.length ? Math.max(agentStart, ...ends) : agentStart;
    }
  }
  const agentWindow: Interval | null =
    agentStart != null && agentEnd != null && agentEnd > agentStart
      ? { start: agentStart, end: agentEnd }
      : null;

  const candidatesStart = [
    opts.startMs,
    setup?.start,
    inject?.start,
    agentWindow?.start,
    ...events.map((ev) => parseTs(ev.timestamp)),
  ].filter(isFinite_);
  const candidatesEnd = [
    opts.endMs,
    teardown?.end,
    agentWindow?.end,
    ...events.map((ev) => parseTs(ev.timestamp)),
  ].filter(isFinite_);
  if (!candidatesStart.length || !candidatesEnd.length) return null;
  const sessionStart = Math.min(...candidatesStart);
  const sessionEnd = opts.live ? Math.max(nowMs, ...candidatesEnd) : Math.max(...candidatesEnd);
  const totalMs = Math.max(0, sessionEnd - sessionStart);
  if (totalMs <= 0) return null;
  const window: Interval = { start: sessionStart, end: sessionEnd };

  const setupMs = setup ? unionMs([setup], window) : 0;
  const injectMs = inject ? unionMs([inject], window) : 0;
  const agentMs = agentWindow ? unionMs([agentWindow], window) : 0;
  const teardownMs = teardown ? unionMs([teardown], window) : 0;
  const known = unionMs(
    [setup, inject, agentWindow, teardown].filter((iv): iv is Interval => iv != null),
    window,
  );
  const sessionParts = shares<SessionShareKey>(
    [
      { key: "setup", ms: setupMs, ...SESSION_SHARES.setup },
      { key: "inject", ms: injectMs, ...SESSION_SHARES.inject },
      { key: "agent", ms: agentMs, ...SESSION_SHARES.agent },
      { key: "teardown", ms: teardownMs, ...SESSION_SHARES.teardown },
      { key: "other", ms: totalMs - known, ...SESSION_SHARES.other },
    ],
    totalMs,
  );

  let agentParts: TimeShare<AgentShareKey>[] = [];
  if (agentWindow) {
    const llm: Interval[] = [];
    const tools: Interval[] = [];
    for (const span of spans) {
      const end = opts.live && span.id.endsWith("-open") ? nowMs : span.endMs;
      const iv = { start: span.startMs, end };
      if (span.lane === "model") llm.push(iv);
      else if (span.lane === "tools") tools.push(iv);
    }
    const toolsMs = unionMs(tools, agentWindow);
    const llmToolsMs = unionMs([...llm, ...tools], agentWindow);
    const allMs = unionMs(
      [...llm, ...tools, ...(sandbox ? [sandbox] : [])],
      agentWindow,
    );
    agentParts = shares<AgentShareKey>(
      [
        { key: "llm", ms: llmToolsMs - toolsMs, ...AGENT_SHARES.llm },
        { key: "tools", ms: toolsMs, ...AGENT_SHARES.tools },
        { key: "sandbox", ms: allMs - llmToolsMs, ...AGENT_SHARES.sandbox },
        { key: "overhead", ms: agentMs - allMs, ...AGENT_SHARES.overhead },
      ],
      agentMs,
    );
  }

  return {
    totalMs,
    live: opts.live,
    session: sessionParts,
    agentMs: agentWindow ? agentMs : null,
    agent: agentParts,
  };
}

export interface ToolUsage {
  name: string;
  calls: number;
  errors: number;
  totalMs: number;
  avgMs: number | null;
  maxMs: number | null;
}

/** Per-tool call counts and time, busiest first. */
export function toolUsage(rows: DisplayEvent[]): ToolUsage[] {
  const byName = new Map<string, ToolUsage & { timed: number }>();
  for (const row of rows) {
    if (row.role !== "tool") continue;
    const name = row.title || "tool";
    const entry = byName.get(name) ?? {
      name,
      calls: 0,
      errors: 0,
      totalMs: 0,
      avgMs: null,
      maxMs: null,
      timed: 0,
    };
    entry.calls += 1;
    if (row.error) entry.errors += 1;
    if (row.durationMs != null) {
      entry.timed += 1;
      entry.totalMs += row.durationMs;
      entry.maxMs = Math.max(entry.maxMs ?? 0, row.durationMs);
    }
    byName.set(name, entry);
  }
  return [...byName.values()]
    .map(({ timed, ...entry }) => ({
      ...entry,
      avgMs: timed > 0 ? entry.totalMs / timed : null,
    }))
    .sort((a, b) => b.totalMs - a.totalMs || b.calls - a.calls || a.name.localeCompare(b.name));
}

export interface SlowOperation {
  /** Ledger row id — select it on the Timeline tab. */
  rowId: string;
  role: DisplayEvent["role"];
  label: string;
  summary: string;
  durationMs: number;
  timestamp: string | null;
  error: boolean;
}

/** Longest finished operations (LLM requests, tool calls, NIKA steps). */
export function slowestOperations(rows: DisplayEvent[], limit = 6): SlowOperation[] {
  const out: SlowOperation[] = [];
  for (const row of rows) {
    if (row.durationMs == null || row.durationMs <= 0) continue;
    if (row.role === "score" || row.role === "other") continue;
    // Run-length bookends measure the whole run, not one operation.
    if (row.role === "nika" && /^(agent_end|agent_error|sandbox_end)$/.test(row.event ?? "")) {
      continue;
    }
    out.push({
      rowId: row.id,
      role: row.role,
      label:
        row.role === "nika"
          ? row.event?.replaceAll("_", " ") || row.title
          : row.title,
      summary: row.summary,
      durationMs: row.durationMs,
      timestamp: row.timestamp,
      error: row.error,
    });
  }
  return out.sort((a, b) => b.durationMs - a.durationMs).slice(0, limit);
}
