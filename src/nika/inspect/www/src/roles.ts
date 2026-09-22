import type { CanonicalTraceEvent, EventKind } from "./api";

/** Visual role for DeepSeek-style color coding (English labels). */
export type Role = "nika" | "assistant" | "tool" | "score" | "system" | "other";

export function eventRole(ev: CanonicalTraceEvent): Role {
  if (ev.source === "nika") {
    if (ev.kind === "score") return "score";
    return "nika";
  }
  switch (ev.kind as EventKind) {
    case "tool_call":
    case "tool_result":
    case "tool_error":
      return "tool";
    case "llm":
    case "phase":
      return "assistant";
    case "submission":
    case "score":
      return "score";
    case "system":
      return "system";
    case "lifecycle":
      return "nika";
    default:
      return "other";
  }
}

/** True when a ledger row is tool I/O (call / result / error). */
export function isToolDisplay(row: {
  role: Role;
  kind: string;
  event: string | null;
}): boolean {
  return (
    row.role === "tool" ||
    row.kind === "tool_call" ||
    row.kind === "tool_result" ||
    row.kind === "tool_error" ||
    row.event === "tool" ||
    row.event === "tool_error"
  );
}

export function roleLabel(role: Role): string {
  switch (role) {
    case "nika":
      return "NIKA";
    case "assistant":
      return "Agent";
    case "tool":
      return "Tool";
    case "score":
      return "Score";
    case "system":
      return "System";
    default:
      return "Other";
  }
}

export type OverviewLane = "nika" | "model" | "tools";

export function eventLane(ev: CanonicalTraceEvent): OverviewLane {
  const role = eventRole(ev);
  if (role === "tool") return "tools";
  if (role === "assistant") return "model";
  return "nika";
}

export function parseTs(value?: string | null): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

export interface OverviewSpan {
  id: string;
  eventId: string;
  lane: OverviewLane;
  role: Role;
  label: string;
  startMs: number;
  endMs: number;
  error?: boolean;
}

const DEFAULT_MARK_MS = 250;

/** Logged wall duration; NIKA timestamps are the end of the operation. */
function eventDurationMs(ev: CanonicalTraceEvent): number | null {
  if (typeof ev.duration_ms === "number" && Number.isFinite(ev.duration_ms) && ev.duration_ms > 0) {
    return ev.duration_ms;
  }
  const raw = ev.raw?.duration_ms;
  if (typeof raw === "number" && Number.isFinite(raw) && raw > 0) return raw;
  const data = ev.raw?.data;
  if (data && typeof data === "object" && !Array.isArray(data)) {
    const nested = (data as Record<string, unknown>).duration_ms;
    if (typeof nested === "number" && Number.isFinite(nested) && nested > 0) return nested;
  }
  return null;
}

function msToIso(ms: number): string {
  return new Date(ms).toISOString();
}

/** Clock label for overview hover (local wall time). */
export function formatOverviewClock(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

function isLlmStart(ev: CanonicalTraceEvent): boolean {
  return ev.event === "llm_start" || ev.event === "turn.started";
}

function isLlmEnd(ev: CanonicalTraceEvent): boolean {
  return (
    ev.event === "llm_end" ||
    ev.event === "llm_end_error" ||
    ev.event === "turn.completed" ||
    ev.event === "turn.failed"
  );
}

function isLlmEndError(ev: CanonicalTraceEvent | null | undefined): boolean {
  if (!ev) return false;
  return ev.event === "llm_end_error" || ev.event === "turn.failed";
}

/** Codex turn interiors: agent_message items + stream reconnect errors. */
function isCodexTurnInterior(ev: CanonicalTraceEvent): boolean {
  if (ev.source !== "agent") return false;
  if (ev.event === "error") return true;
  if (ev.event === "item.completed" || ev.event === "item.started") {
    return ev.kind === "llm";
  }
  return false;
}

/** Low-signal Codex bookkeeping — keep out of the default ledger. */
function isAgentNoiseEvent(ev: CanonicalTraceEvent): boolean {
  if (ev.source !== "agent") return false;
  return (
    ev.event === "mcp_config" ||
    ev.event === "subprocess_start" ||
    ev.event === "thread.started"
  );
}

/**
 * Phase bookends in messages.jsonl (agent source).
 * Contract (see agent.utils.loggers): agent_start → agent_done | agent_error.
 * Session bookends live in nika.jsonl as agent_start / agent_end | agent_error
 * (kept as separate events). Drop phase markers on the merged timeline.
 */
function isAgentPhaseMarker(ev: CanonicalTraceEvent): boolean {
  return (
    ev.source === "agent" &&
    (ev.event === "agent_start" ||
      ev.event === "agent_done" ||
      ev.event === "agent_error")
  );
}

/**
 * NIKA run-length *end* bookends (nika.jsonl): duration_ms is total elapsed
 * across the whole run (perf_counter), not an operation that ended at the
 * log timestamp. Keep these as marks at log time; Dur still shows the
 * measured value. Counterpart *start* ops (e.g. sandbox_start after create)
 * carry real setup elapsed and use normal duration backdate.
 */
function isNikaRunLengthBookend(ev: CanonicalTraceEvent): boolean {
  if (ev.source !== "nika") return false;
  return (
    ev.event === "agent_end" ||
    ev.event === "agent_error" ||
    ev.event === "sandbox_end"
  );
}

function usesDurationBackdate(ev: CanonicalTraceEvent): boolean {
  if (isNikaRunLengthBookend(ev)) return false;
  return eventDurationMs(ev) != null;
}

/** Build overview spans; pair llm_start→llm_end and tool_call→tool_result. */
export function buildOverviewSpans(events: CanonicalTraceEvent[]): OverviewSpan[] {
  const dated = events
    .map((ev) => ({ ev, ms: parseTs(ev.timestamp) }))
    .filter((x): x is { ev: CanonicalTraceEvent; ms: number } => x.ms != null);
  if (dated.length === 0) return [];

  const spans: OverviewSpan[] = [];
  const pendingToolsById = new Map<string, { ev: CanonicalTraceEvent; ms: number }>();
  const pendingToolsByName = new Map<string, { ev: CanonicalTraceEvent; ms: number }[]>();
  const pendingLlms: { ev: CanonicalTraceEvent; ms: number }[] = [];

  const pushPendingTool = (ev: CanonicalTraceEvent, ms: number) => {
    const callId = ev.tool?.tool_call_id;
    if (callId) {
      pendingToolsById.set(String(callId), { ev, ms });
      return;
    }
    const name = ev.tool?.name || "tool";
    const q = pendingToolsByName.get(name) || [];
    q.push({ ev, ms });
    pendingToolsByName.set(name, q);
  };

  const popPendingTool = (ev: CanonicalTraceEvent) => {
    const callId = ev.tool?.tool_call_id;
    if (callId && pendingToolsById.has(String(callId))) {
      const start = pendingToolsById.get(String(callId))!;
      pendingToolsById.delete(String(callId));
      return start;
    }
    const name = ev.tool?.name || "tool";
    const q = pendingToolsByName.get(name);
    if (q && q.length > 0) return q.shift()!;
    return null;
  };

  for (const { ev, ms } of dated) {
    if (isAgentPhaseMarker(ev)) continue;

    const role = eventRole(ev);
    const lane = eventLane(ev);

    if (ev.kind === "tool_call") {
      pushPendingTool(ev, ms);
      continue;
    }

    if (ev.kind === "tool_result" || ev.kind === "tool_error") {
      const start = popPendingTool(ev);
      const startMs = start?.ms ?? ms;
      spans.push({
        id: `span-${start?.ev.id ?? ev.id}`,
        eventId: start?.ev.id ?? ev.id,
        lane: "tools",
        role: "tool",
        label: ev.tool?.name || start?.ev.tool?.name || "tool",
        startMs,
        endMs: Math.max(ms, startMs + DEFAULT_MARK_MS),
        error: ev.kind === "tool_error",
      });
      continue;
    }

    if (isLlmStart(ev)) {
      pendingLlms.push({ ev, ms });
      continue;
    }

    if (isLlmEnd(ev)) {
      const start = pendingLlms.shift();
      const startMs = start?.ms ?? ms;
      spans.push({
        id: `span-${start?.ev.id ?? ev.id}`,
        eventId: start?.ev.id ?? ev.id,
        lane: "model",
        role: "assistant",
        label: "llm",
        startMs,
        endMs: Math.max(ms, startMs + DEFAULT_MARK_MS),
        error: isLlmEndError(ev),
      });
      continue;
    }

    // Codex reconnect / empty agent_message chatter is folded into the turn.
    if (isCodexTurnInterior(ev) || isAgentNoiseEvent(ev)) {
      continue;
    }

    // Logger timestamp is the end of the operation when duration_ms is an
    // operation elapsed (env_start, inject, …). Run-length bookends
    // (agent_*/sandbox_*) stay as marks at the log timestamp.
    const dur = usesDurationBackdate(ev) ? eventDurationMs(ev) : null;
    const startMs = dur != null ? Math.max(0, ms - dur) : ms;
    const endMs = dur != null ? Math.max(ms, startMs + 1) : ms + DEFAULT_MARK_MS;
    spans.push({
      id: `span-${ev.id}`,
      eventId: ev.id,
      lane,
      role,
      label:
        role === "nika"
          ? ev.event?.replaceAll("_", " ") || "nika"
          : role === "assistant"
            ? ev.event || "model"
            : roleLabel(role),
      startMs,
      endMs,
      error: ev.event === "agent_error",
    });
  }

  for (const { ev, ms } of pendingToolsById.values()) {
    spans.push({
      id: `span-${ev.id}-open`,
      eventId: ev.id,
      lane: "tools",
      role: "tool",
      label: ev.tool?.name || "tool",
      startMs: ms,
      endMs: ms + DEFAULT_MARK_MS,
    });
  }
  for (const q of pendingToolsByName.values()) {
    for (const { ev, ms } of q) {
      spans.push({
        id: `span-${ev.id}-open`,
        eventId: ev.id,
        lane: "tools",
        role: "tool",
        label: ev.tool?.name || "tool",
        startMs: ms,
        endMs: ms + DEFAULT_MARK_MS,
      });
    }
  }

  for (const { ev, ms } of pendingLlms) {
    spans.push({
      id: `span-${ev.id}-open`,
      eventId: ev.id,
      lane: "model",
      role: "assistant",
      label: "llm",
      startMs: ms,
      endMs: ms + DEFAULT_MARK_MS,
    });
  }

  return spans;
}

/**
 * Overview projection (DeepSeek Harness-aligned):
 * - equal: equal-width chips in global chronological order
 * - duration: real bar widths with idle gaps compressed
 * - actual: real bar widths on the full wall-clock axis
 */
export type OverviewLayoutMode = "equal" | "duration" | "actual";

/**
 * Collapse idle gaps between the global coverage envelope and the next span
 * (Harness `compressIdle`). Overlapping/parallel spans stay overlapping.
 */
export function compressIdleSpans(spans: OverviewSpan[]): OverviewSpan[] {
  if (spans.length === 0) return [];
  const sorted = [...spans].sort(
    (a, b) => a.startMs - b.startMs || a.endMs - b.endMs || a.id.localeCompare(b.id),
  );
  const offsetById = new Map<string, number>();
  let removedIdle = 0;
  let coveredUntil: number | null = null;
  for (const span of sorted) {
    if (coveredUntil !== null && span.startMs > coveredUntil) {
      removedIdle += span.startMs - coveredUntil;
    }
    offsetById.set(span.id, removedIdle);
    coveredUntil =
      coveredUntil === null ? span.endMs : Math.max(coveredUntil, span.endMs);
  }
  return spans.map((span) => {
    const offset = offsetById.get(span.id) ?? 0;
    return {
      ...span,
      startMs: span.startMs - offset,
      endMs: span.endMs - offset,
    };
  });
}

/** Project spans into the active overview mode's time domain. */
export function projectOverviewSpans(
  spans: OverviewSpan[],
  mode: OverviewLayoutMode,
): OverviewSpan[] {
  if (mode === "duration") return compressIdleSpans(spans);
  return spans;
}

/**
 * Lay out overview bars. For `duration`, idle is compressed for geometry only;
 * returned `span` objects keep original wall-clock start/end for brush/ledger.
 */
export function layoutOverviewSpans(
  spans: OverviewSpan[],
  domainStart: number,
  domainEnd: number,
  mode: OverviewLayoutMode = "equal",
): { span: OverviewSpan; leftPct: number; widthPct: number }[] {
  const domainMs = Math.max(1, domainEnd - domainStart);
  const sorted = [...spans].sort(
    (a, b) => a.startMs - b.startMs || a.endMs - b.endMs || a.id.localeCompare(b.id),
  );
  if (sorted.length === 0) return [];

  if (mode === "actual") {
    return sorted.map((span) => {
      const startMs = span.startMs;
      const endMs = Math.max(span.endMs, startMs + 1);
      const leftPct = ((startMs - domainStart) / domainMs) * 100;
      const widthPct = Math.max(0.12, ((endMs - startMs) / domainMs) * 100);
      return { span, leftPct, widthPct };
    });
  }

  if (mode === "duration") {
    const compressed = compressIdleSpans(sorted);
    const byId = new Map(compressed.map((s) => [s.id, s]));
    return sorted.map((span) => {
      const c = byId.get(span.id)!;
      const startMs = c.startMs;
      const endMs = Math.max(c.endMs, startMs + 1);
      const leftPct = ((startMs - domainStart) / domainMs) * 100;
      const widthPct = Math.max(0.12, ((endMs - startMs) / domainMs) * 100);
      return { span, leftPct, widthPct };
    });
  }

  // Equal width in true chronological order across every lane.
  const n = sorted.length;
  const gapPct = Math.min(0.55, 6 / Math.max(n, 1));
  const gapMs = (gapPct / 100) * domainMs;
  const totalGap = gapMs * Math.max(0, n - 1);
  const chipMs = Math.max(domainMs * 0.0015, domainMs - totalGap) / n;

  return sorted.map((span, i) => {
    const startMs = domainStart + i * (chipMs + gapMs);
    const endMs = startMs + chipMs;
    const leftPct = ((startMs - domainStart) / domainMs) * 100;
    const widthPct = Math.max(0.1, ((endMs - startMs) / domainMs) * 100);
    return { span, leftPct, widthPct };
  });
}

/** Vertical markers at assistant (LLM) span starts, in layout %. */
export function overviewTurnBoundaryPcts(
  laidOut: { span: OverviewSpan; leftPct: number; widthPct: number }[],
): number[] {
  const marks = laidOut
    .filter(({ span }) => span.role === "assistant")
    .map(({ leftPct }) => leftPct);
  return [...new Set(marks.map((p) => Math.round(p * 100) / 100))].sort(
    (a, b) => a - b,
  );
}

export type LaidOutOverviewSpan = {
  span: OverviewSpan;
  leftPct: number;
  widthPct: number;
  /** Overlap layer within the lane (0 = bottom / lightest). */
  stackRow: number;
};

/** Soft cap so a single lane cannot grow without bound. */
const MAX_LANE_STACK = 12;

/**
 * Assign overlap layers per lane. Concurrent bars share x-range but get
 * different stackRow for a slight height nudge (still stacked together).
 */
export function assignLaneStackRows(
  items: { span: OverviewSpan; leftPct: number; widthPct: number }[],
  maxStack: number = MAX_LANE_STACK,
): LaidOutOverviewSpan[] {
  const byLane = new Map<string, typeof items>();
  for (const it of items) {
    const list = byLane.get(it.span.lane) || [];
    list.push(it);
    byLane.set(it.span.lane, list);
  }
  const out: LaidOutOverviewSpan[] = [];
  for (const laneItems of byLane.values()) {
    const sorted = [...laneItems].sort(
      (a, b) =>
        a.leftPct - b.leftPct ||
        b.widthPct - a.widthPct ||
        a.span.id.localeCompare(b.span.id),
    );
    const rowEnds: number[] = [];
    for (const it of sorted) {
      const left = it.leftPct;
      const right = it.leftPct + Math.max(it.widthPct, 0);
      let row = -1;
      for (let r = 0; r < rowEnds.length; r++) {
        if (rowEnds[r] <= left + 0.05) {
          row = r;
          break;
        }
      }
      if (row < 0) {
        if (rowEnds.length < maxStack) {
          row = rowEnds.length;
          rowEnds.push(right);
        } else {
          row = 0;
          for (let r = 1; r < maxStack; r++) {
            if (rowEnds[r] < rowEnds[row]) row = r;
          }
          rowEnds[row] = Math.max(rowEnds[row], right);
        }
      } else {
        rowEnds[row] = right;
      }
      out.push({ ...it, stackRow: row });
    }
  }
  return out;
}

/** Compact fixed lane height — overlaps stay stacked, not expanded. */
export function overviewLaneHeightPx(_maxStackRow?: number): number {
  return 16;
}

/**
 * Slight height/top nudge so concurrent bars remain mostly stacked but
 * distinguishable. Click-to-zoom popup shows the full overlap cluster.
 */
export function overviewStackBarGeometry(
  stackRow: number,
  laneHeight: number,
  layerCount: number,
): { top: number; height: number; depth: number } {
  const layers = Math.max(1, layerCount);
  const baseH = Math.min(10, Math.max(7, laneHeight - 5));
  const height = Math.max(5, baseH - Math.min(stackRow, 5) * 0.7);
  const top = Math.min(laneHeight - height - 1, 3 + Math.min(stackRow, 6) * 1.1);
  const depth = layers <= 1 ? 0 : stackRow / (layers - 1);
  return { top, height, depth };
}

/** Ledger row: paired llm_start/end (and tool call/result) as one timed span. */
export interface DisplayEvent {
  id: string;
  role: Role;
  title: string;
  summary: string;
  timestamp: string | null;
  endTimestamp: string | null;
  durationMs: number | null;
  phase: string | null;
  event: string | null;
  kind: string;
  start: CanonicalTraceEvent;
  end: CanonicalTraceEvent | null;
  /** Codex turn interiors folded into this span (agent_message, reconnect errors). */
  interiors?: CanonicalTraceEvent[];
  error: boolean;
}

/** Claude CLI stream-json nests message content under ``claude_event``. */
function claudeMessageContent(raw: Record<string, unknown> | undefined): unknown {
  if (!raw) return null;
  const ce = raw.claude_event;
  if (!ce || typeof ce !== "object" || Array.isArray(ce)) return null;
  const message = (ce as Record<string, unknown>).message;
  if (!message || typeof message !== "object" || Array.isArray(message)) return null;
  return (message as Record<string, unknown>).content ?? null;
}

function llmMessageSource(raw: Record<string, unknown> | undefined): unknown {
  if (!raw) return null;
  return raw.messages ?? raw.text ?? raw.content ?? claudeMessageContent(raw);
}

function llmPreview(ev: CanonicalTraceEvent): string {
  const parsed = parseLlmMessageContent(llmMessageSource(ev.raw));
  if (parsed.text.trim()) return parsed.text.slice(0, 160);
  const text = ev.raw.text ?? ev.raw.messages ?? ev.summary;
  if (typeof text === "string") return text.slice(0, 160);
  if (text != null) {
    try {
      return JSON.stringify(text).slice(0, 160);
    } catch {
      return String(text).slice(0, 160);
    }
  }
  return ev.summary || "";
}

function formatToolPreview(value: unknown, limit = 160): string {
  if (value == null) return "";
  if (typeof value === "string") return value.trim().slice(0, limit);
  if (typeof value === "object" && !Array.isArray(value)) {
    const obj = value as Record<string, unknown>;
    // Prefer the command / primary arg humans scan for in the ledger.
    for (const key of ["command", "cmd", "query", "path", "file_path", "name"]) {
      const v = obj[key];
      if (typeof v === "string" && v.trim()) return v.trim().slice(0, limit);
    }
  }
  try {
    return JSON.stringify(value).slice(0, limit);
  } catch {
    return String(value).slice(0, limit);
  }
}

/** Ledger Content for a tool row: result text, else parameters/input. */
function toolLedgerSummary(
  start: CanonicalTraceEvent,
  end: CanonicalTraceEvent | null,
): string {
  if (end) {
    const err = end.tool?.error ?? (typeof end.raw.error === "string" ? end.raw.error : null);
    if (typeof err === "string" && err.trim()) return err.trim().slice(0, 160);
    const out = end.tool?.output;
    if (typeof out === "string" && out.trim()) return out.trim().slice(0, 160);
    if (out != null && !(typeof out === "string")) {
      const formatted = formatToolPreview(out);
      if (formatted) return formatted;
    }
    if (end.summary?.trim()) return end.summary.trim().slice(0, 160);
  }
  const input =
    start.tool?.input ??
    start.raw.input ??
    start.raw.arguments ??
    null;
  const fromInput = formatToolPreview(input);
  if (fromInput) return fromInput;
  if (start.summary?.trim()) return start.summary.trim().slice(0, 160);
  return end ? "" : "in progress";
}

/** Collapse llm_start↔llm_end (and tool_call↔result) into span rows for the ledger. */
export function collapsePairedEvents(
  events: CanonicalTraceEvent[],
): DisplayEvent[] {
  const out: DisplayEvent[] = [];
  const used = new Set<string>();

  const findLlmEnd = (from: number, phase: string | null | undefined) => {
    for (let j = from; j < events.length; j++) {
      const e = events[j];
      if (used.has(e.id)) continue;
      if (!isLlmEnd(e)) {
        // Stop at next llm_start so we don't skip across calls.
        if (isLlmStart(e)) return null;
        continue;
      }
      if (phase && e.phase && e.phase !== phase) continue;
      return { end: e, index: j };
    }
    return null;
  };

  const findToolEnd = (from: number, start: CanonicalTraceEvent) => {
    const callId = start.tool?.tool_call_id;
    const name = start.tool?.name;
    for (let j = from; j < events.length; j++) {
      const e = events[j];
      if (used.has(e.id)) continue;
      // Parallel tool_start bursts are common — keep scanning past other
      // tool_calls until the matching tool_end / tool_error.
      if (e.kind !== "tool_result" && e.kind !== "tool_error") continue;
      if (callId) {
        if (e.tool?.tool_call_id === callId) return { end: e, index: j };
        // Claude sometimes omits tool_use_id on string-message results.
        if (e.tool?.tool_call_id) continue;
        if (name && e.tool?.name && e.tool.name !== name) continue;
        return { end: e, index: j };
      }
      if (name && e.tool?.name === name) return { end: e, index: j };
      if (!e.tool?.name) return { end: e, index: j };
    }
    return null;
  };

  for (let i = 0; i < events.length; i++) {
    const ev = events[i];
    if (used.has(ev.id)) continue;
    if (isAgentPhaseMarker(ev) || isAgentNoiseEvent(ev)) {
      used.add(ev.id);
      continue;
    }

    if (isLlmStart(ev)) {
      const matched = findLlmEnd(i + 1, ev.phase);
      const end = matched?.end ?? null;
      if (end) used.add(end.id);
      used.add(ev.id);
      // Fold Codex turn interiors (agent_message + reconnect errors) into the span.
      const interiors: CanonicalTraceEvent[] = [];
      const interiorSummaries: string[] = [];
      if (matched) {
        for (let j = i + 1; j < matched.index; j++) {
          const mid = events[j]!;
          if (used.has(mid.id)) continue;
          if (!isCodexTurnInterior(mid)) continue;
          used.add(mid.id);
          interiors.push(mid);
          if (mid.summary?.trim()) interiorSummaries.push(mid.summary.trim());
        }
      }
      const startMs = parseTs(ev.timestamp);
      const endMs = parseTs(end?.timestamp);
      const inputPreview = parseLlmMessageContent(llmMessageSource(ev.raw)).text;
      const finish =
        end?.raw.generation_info &&
        typeof end.raw.generation_info === "object" &&
        !Array.isArray(end.raw.generation_info)
          ? (end.raw.generation_info as Record<string, unknown>).finish_reason
          : null;
      const interiorPreview = [...interiorSummaries]
        .reverse()
        .find((s) => s && !s.toLowerCase().startsWith("reconnecting"))
        ?? interiorSummaries[interiorSummaries.length - 1]
        ?? "";
      let summary = inputPreview.trim()
        ? inputPreview.slice(0, 160)
        : interiorPreview
          ? interiorPreview.slice(0, 160)
          : end
            ? (end.summary || llmPreview(end)).slice(0, 160)
            : "in progress";
      if (typeof finish === "string" && finish === "tool_calls") {
        summary = summary ? `${summary}` : "→ tool calls";
      }
      out.push({
        id: ev.id,
        role: "assistant",
        title: "llm",
        summary,
        timestamp: ev.timestamp ?? null,
        endTimestamp: end?.timestamp ?? null,
        durationMs:
          startMs != null && endMs != null ? Math.max(0, endMs - startMs) : null,
        phase: ev.phase ?? null,
        event: "llm",
        kind: "llm",
        start: ev,
        end,
        interiors: interiors.length ? interiors : undefined,
        error: isLlmEndError(end),
      });
      continue;
    }

    if (isLlmEnd(ev)) {
      // Orphan end without a start we already consumed.
      used.add(ev.id);
      out.push({
        id: ev.id,
        role: "assistant",
        title: "llm end",
        summary: ev.summary || llmPreview(ev),
        timestamp: ev.timestamp ?? null,
        endTimestamp: ev.timestamp ?? null,
        durationMs: null,
        phase: ev.phase ?? null,
        event: ev.event,
        kind: "llm",
        start: ev,
        end: null,
        error: isLlmEndError(ev),
      });
      continue;
    }

    // Orphan Codex agent_message with no text — skip empty ledger noise.
    if (
      ev.kind === "llm" &&
      (ev.event === "item.completed" || ev.event === "item.started") &&
      !ev.summary?.trim()
    ) {
      used.add(ev.id);
      continue;
    }

    if (ev.kind === "tool_call") {
      const matched = findToolEnd(i + 1, ev);
      const end = matched?.end ?? null;
      if (end) used.add(end.id);
      used.add(ev.id);
      const startMs = parseTs(ev.timestamp);
      const endMs = parseTs(end?.timestamp);
      out.push({
        id: ev.id,
        role: "tool",
        title: ev.tool?.name || end?.tool?.name || "tool",
        summary: toolLedgerSummary(ev, end),
        timestamp: ev.timestamp ?? null,
        endTimestamp: end?.timestamp ?? null,
        durationMs:
          startMs != null && endMs != null ? Math.max(0, endMs - startMs) : null,
        phase: ev.phase ?? null,
        event: end?.kind === "tool_error" ? "tool_error" : "tool",
        kind: end?.kind === "tool_error" ? "tool_error" : "tool_call",
        start: ev,
        end,
        error: end?.kind === "tool_error",
      });
      continue;
    }

    if (ev.kind === "tool_result" || ev.kind === "tool_error") {
      // Orphan end — start was missing or already paired.
      if (used.has(ev.id)) continue;
      used.add(ev.id);
      out.push({
        id: ev.id,
        role: "tool",
        title: ev.tool?.name || "tool",
        summary: toolLedgerSummary(ev, ev),
        timestamp: ev.timestamp ?? null,
        endTimestamp: ev.timestamp ?? null,
        durationMs: null,
        phase: ev.phase ?? null,
        event: "tool",
        kind: ev.kind,
        start: ev,
        end: null,
        error: ev.kind === "tool_error",
      });
      continue;
    }

    used.add(ev.id);
    const role = eventRole(ev);
    const measured = eventDurationMs(ev);
    const backdate = usesDurationBackdate(ev);
    const logMs = parseTs(ev.timestamp);
    // Operation elapsed (env / inject): Time = [ts−dur, ts].
    // Run-length bookends (agent_*/sandbox_*): Time stays at log ts; Dur still
    // shows measured duration_ms on the end event.
    const startIso =
      backdate && measured != null && logMs != null
        ? msToIso(logMs - measured)
        : ev.timestamp ?? null;
    const endIso =
      backdate && measured != null && logMs != null
        ? (ev.timestamp ?? msToIso(logMs))
        : null;
    const displayEvent =
      ev.kind === "llm"
        ? "llm"
        : ev.kind === "system" && ev.title
          ? ev.title
          : (ev.event ?? null);
    out.push({
      id: ev.id,
      role,
      title: ev.kind === "llm" ? "llm" : ev.title,
      summary:
        ev.summary ||
        (ev.kind === "llm" ? llmPreview(ev) : ev.summary) ||
        "",
      timestamp: startIso,
      endTimestamp: endIso,
      durationMs: measured,
      phase: ev.phase ?? null,
      event: displayEvent,
      kind: ev.kind,
      start: ev,
      end: null,
      error:
        ev.event === "agent_error" ||
        ev.event === "subprocess_error" ||
        (ev.kind === "system" && ev.title === "error"),
    });
  }

  return out;
}

export function formatDuration(ms: number | null): string {
  if (ms == null || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)} s`;
  const mins = Math.floor(ms / 60_000);
  const secs = ((ms % 60_000) / 1000).toFixed(1);
  return `${mins}m ${secs}s`;
}

export function displayDurationMs(row: DisplayEvent): number | null {
  return row.durationMs;
}

export interface LlmTokenUsage {
  input: number | null;
  output: number | null;
  total: number | null;
}

function asFiniteNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function readUsageRecord(raw: Record<string, unknown> | null | undefined): LlmTokenUsage | null {
  if (!raw) return null;
  const candidates: unknown[] = [
    raw.usage_metadata,
    raw.usage,
    raw.token_usage,
    raw.tokens,
  ];
  for (const nest of ["response", "message", "data", "info", "generation_info"]) {
    const obj = raw[nest];
    if (obj && typeof obj === "object" && !Array.isArray(obj)) {
      const rec = obj as Record<string, unknown>;
      candidates.push(rec.usage_metadata, rec.usage, rec.token_usage);
    }
  }

  for (const c of candidates) {
    if (!c || typeof c !== "object" || Array.isArray(c)) continue;
    const u = c as Record<string, unknown>;
    const input =
      asFiniteNumber(u.input_tokens) ??
      asFiniteNumber(u.prompt_tokens) ??
      asFiniteNumber(u.in_tokens) ??
      asFiniteNumber(u.input);
    const output =
      asFiniteNumber(u.output_tokens) ??
      asFiniteNumber(u.completion_tokens) ??
      asFiniteNumber(u.out_tokens) ??
      asFiniteNumber(u.output);
    const total =
      asFiniteNumber(u.total_tokens) ??
      asFiniteNumber(u.total) ??
      (input != null || output != null ? (input ?? 0) + (output ?? 0) : null);
    if (input != null || output != null || total != null) {
      return { input, output, total };
    }
  }
  return null;
}

/** Token usage for a paired llm_start/llm_end row (prefers end event). */
export function llmTokenUsage(row: DisplayEvent): LlmTokenUsage | null {
  return readUsageRecord(row.end?.raw) ?? readUsageRecord(row.start.raw);
}

/** Resolve a displayable model id from llm event payloads or session metadata. */
export function extractModelName(
  ...sources: Array<unknown>
): string | null {
  for (const src of sources) {
    if (src == null) continue;
    if (typeof src === "string" && src.trim()) return src.trim();
    if (typeof src !== "object" || Array.isArray(src)) continue;
    const obj = src as Record<string, unknown>;

    // Direct fields on the event raw blob.
    for (const key of ["model_name", "model", "model_id", "llm_model"]) {
      const v = obj[key];
      if (typeof v === "string" && v.trim()) return v.trim();
    }

    // LangChain serialized ChatOpenAI (and similar) constructors.
    const kwargs = obj.kwargs;
    if (kwargs && typeof kwargs === "object" && !Array.isArray(kwargs)) {
      const kw = kwargs as Record<string, unknown>;
      for (const key of ["model_name", "model", "model_id"]) {
        const v = kw[key];
        if (typeof v === "string" && v.trim()) return v.trim();
      }
    }

    // Nested raw.model object on the event.
    if (obj.model && typeof obj.model === "object" && !Array.isArray(obj.model)) {
      const nested = extractModelName(obj.model);
      if (nested) return nested;
    }

    // Claude CLI: model lives on claude_event.message.model.
    const ce = obj.claude_event;
    if (ce && typeof ce === "object" && !Array.isArray(ce)) {
      const nested = extractModelName(ce);
      if (nested) return nested;
      const message = (ce as Record<string, unknown>).message;
      if (message && typeof message === "object" && !Array.isArray(message)) {
        const fromMsg = extractModelName(message);
        if (fromMsg) return fromMsg;
      }
    }
  }
  return null;
}

export function formatTokenCount(n: number | null): string {
  if (n == null) return "—";
  return n.toLocaleString("en-US");
}

function unescapePyString(s: string): string {
  // Replace \\ first so sequences like \\n in nested JSON stay as \n (two chars),
  // while bare \n from the Python string layer becomes a real newline.
  return s
    .replace(/\\\\/g, "\u0000")
    .replace(/\\n/g, "\n")
    .replace(/\\t/g, "\t")
    .replace(/\\r/g, "\r")
    .replace(/\\'/g, "'")
    .replace(/\\"/g, '"')
    .replace(/\u0000/g, "\\");
}

function textsFromPyContentList(blob: string): { text: string; thinking: string } {
  const texts: string[] = [];
  const thinking: string[] = [];
  const re =
    /\{\s*'type'\s*:\s*'([^']+)'\s*,\s*'text'\s*:\s*'((?:\\.|[^'\\])*)'/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(blob))) {
    const kind = m[1]!;
    const body = unescapePyString(m[2]!);
    if (kind === "thinking" || kind === "reasoning") thinking.push(body);
    else texts.push(body);
  }
  // Double-quoted variant
  const re2 =
    /\{\s*"type"\s*:\s*"([^"]+)"\s*,\s*"text"\s*:\s*"((?:\\.|[^"\\])*)"/g;
  while ((m = re2.exec(blob))) {
    const kind = m[1]!;
    const body = unescapePyString(m[2]!);
    if (kind === "thinking" || kind === "reasoning") thinking.push(body);
    else if (!texts.includes(body)) texts.push(body);
  }
  return { text: texts.join("\n\n"), thinking: thinking.join("\n\n") };
}

/** Parse LangChain-style message blobs logged on llm_start. */
export function parseLlmMessageContent(raw: unknown): {
  text: string;
  thinking: string;
} {
  if (raw == null) return { text: "", thinking: "" };
  if (typeof raw === "object") {
    if (Array.isArray(raw)) {
      const texts: string[] = [];
      const thinking: string[] = [];
      for (const block of raw) {
        if (!block || typeof block !== "object") continue;
        const b = block as Record<string, unknown>;
        const body =
          typeof b.text === "string"
            ? b.text
            : typeof b.thinking === "string"
              ? b.thinking
              : typeof b.content === "string"
                ? b.content
                : "";
        if (!body) continue;
        if (b.type === "thinking" || b.type === "reasoning") thinking.push(body);
        else texts.push(body);
      }
      return { text: texts.join("\n\n"), thinking: thinking.join("\n\n") };
    }
    const obj = raw as Record<string, unknown>;
    if (typeof obj.content === "string") {
      return { text: obj.content, thinking: typeof obj.reasoning_content === "string" ? obj.reasoning_content : "" };
    }
    if (Array.isArray(obj.content)) return parseLlmMessageContent(obj.content);
  }
  if (typeof raw !== "string") {
    try {
      return { text: JSON.stringify(raw, null, 2), thinking: "" };
    } catch {
      return { text: String(raw), thinking: "" };
    }
  }

  const s = raw.trim();
  let thinking = "";
  const reason = s.match(/reasoning_content=["']((?:\\.|[^"\\'])*)["']/);
  if (reason) thinking = unescapePyString(reason[1]!);

  const dq = s.match(/^content="((?:\\.|[^"\\])*)"\s/);
  if (dq) return { text: unescapePyString(dq[1]!), thinking };

  const sq = s.match(/^content='((?:\\.|[^'\\])*)'\s/);
  if (sq) return { text: unescapePyString(sq[1]!), thinking };

  // content=[...] followed by kwargs (additional_kwargs / name / tool_call_id / id=…)
  const list = s.match(
    /^content=(\[[\s\S]*\])\s+(?:additional_kwargs|response_metadata|id=|name=|tool_call_id=)/,
  );
  if (list) {
    const parsed = textsFromPyContentList(list[1]!);
    return { text: parsed.text, thinking: thinking || parsed.thinking };
  }

  // Balanced extract when trailing kwargs use unexpected keys.
  if (s.startsWith("content=[")) {
    const lit = extractBalancedPythonList(s.slice("content=".length));
    if (lit) {
      const parsed = textsFromPyContentList(lit);
      if (parsed.text.trim()) {
        return { text: parsed.text, thinking: thinking || parsed.thinking };
      }
    }
  }

  // Fallback: strip trailing langchain kwargs noise when present.
  const cut = s.search(/\s+(?:additional_kwargs|response_metadata|name|tool_call_id)=/);
  if (s.startsWith("content=") && cut > 0) {
    return { text: s.slice("content=".length, cut).replace(/^["']|["']$/g, ""), thinking };
  }
  return { text: s, thinking };
}

/** Extract a top-level `[...]` Python/JSON list literal from the start of `source`. */
export function extractBalancedPythonList(source: string): string | null {
  if (!source.startsWith("[")) return null;
  let depth = 0;
  let quote: "'" | '"' | null = null;
  let escape = false;
  for (let i = 0; i < source.length; i++) {
    const ch = source[i]!;
    if (quote) {
      if (escape) {
        escape = false;
      } else if (ch === "\\") {
        escape = true;
      } else if (ch === quote) {
        quote = null;
      }
      continue;
    }
    if (ch === "'" || ch === '"') {
      quote = ch;
      continue;
    }
    if (ch === "[") depth += 1;
    else if (ch === "]") {
      depth -= 1;
      if (depth === 0) return source.slice(0, i + 1);
    }
  }
  return null;
}

export interface LlmToolLink {
  rowId: string;
  name: string;
  callId: string | null;
  input: unknown;
}

export interface LlmTurnDetail {
  input: string;
  thinking: string;
  outputText: string;
  finishReason: string | null;
  model: string | null;
  tools: LlmToolLink[];
}

/** Build structured LLM turn detail for the inspector. */
export function buildLlmTurnDetail(
  row: DisplayEvent,
  rows: DisplayEvent[],
  sessionModel?: string | null,
): LlmTurnDetail {
  const start = row.start;
  const end = row.end;
  const interiors = row.interiors ?? [];
  const parsed = parseLlmMessageContent(llmMessageSource(start.raw));
  const endParsed = parseLlmMessageContent(
    end
      ? (end.raw.text ??
          end.raw.content ??
          end.raw.messages ??
          end.raw.report ??
          claudeMessageContent(end.raw))
      : null,
  );

  // Codex: assistant text lives on folded item.completed / agent_message interiors,
  // not on turn.started / turn.completed bookends.
  const interiorTexts: string[] = [];
  const interiorThinking: string[] = [];
  for (const mid of interiors) {
    const fromRaw = parseLlmMessageContent(llmMessageSource(mid.raw));
    const codexItem = (mid.raw.codex_event as { item?: { text?: unknown } } | undefined)
      ?.item;
    const codexText =
      codexItem && typeof codexItem.text === "string" ? codexItem.text : "";
    const text = fromRaw.text.trim() || mid.summary?.trim() || codexText.trim();
    if (text && !text.toLowerCase().startsWith("reconnecting")) {
      interiorTexts.push(text);
    }
    if (fromRaw.thinking.trim()) interiorThinking.push(fromRaw.thinking);
  }

  const thinking =
    parsed.thinking ||
    endParsed.thinking ||
    interiorThinking.join("\n\n") ||
    (typeof end?.raw.reasoning_content === "string" ? end.raw.reasoning_content : "") ||
    (typeof start.raw.reasoning_content === "string" ? start.raw.reasoning_content : "");

  const gen = end?.raw.generation_info;
  let finishReason: string | null = null;
  if (gen && typeof gen === "object" && !Array.isArray(gen)) {
    const fr = (gen as Record<string, unknown>).finish_reason;
    if (typeof fr === "string" && fr.trim()) finishReason = fr;
  }

  const model = extractModelName(start.raw.model, end?.raw.model, start.raw, end?.raw, sessionModel);

  const tools: LlmToolLink[] = [];
  const idx = rows.findIndex((r) => r.id === row.id);
  if (idx >= 0) {
    for (let i = idx + 1; i < rows.length; i++) {
      const next = rows[i]!;
      if (next.role === "assistant") break;
      if (!isToolDisplay(next)) continue;
      tools.push({
        rowId: next.id,
        name: next.start.tool?.name || next.title || "tool",
        callId: next.start.tool?.tool_call_id || next.end?.tool?.tool_call_id || null,
        input: next.start.tool?.input ?? next.start.raw.input,
      });
    }
  }

  // Prefer end-event text (LangChain), then Codex interiors, then unpaired start.
  const outputText = endParsed.text.trim()
    ? endParsed.text
    : interiorTexts.length
      ? interiorTexts.join("\n\n")
      : end
        ? ""
        : parsed.text;

  return {
    input: end ? parsed.text : "",
    thinking,
    outputText,
    finishReason,
    model,
    tools,
  };
}
