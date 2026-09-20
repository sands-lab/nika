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

function isLlmStart(ev: CanonicalTraceEvent): boolean {
  return ev.event === "llm_start";
}

function isLlmEnd(ev: CanonicalTraceEvent): boolean {
  return ev.event === "llm_end" || ev.event === "llm_end_error";
}

/** Per-phase agent_start/agent_done in messages.jsonl — NIKA already logs lifecycle. */
function isAgentPhaseMarker(ev: CanonicalTraceEvent): boolean {
  return (
    ev.source === "agent" &&
    (ev.event === "agent_done" || ev.event === "agent_start")
  );
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
        error: ev.event === "llm_end_error",
      });
      continue;
    }

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
      startMs: ms,
      endMs: ms + DEFAULT_MARK_MS,
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

export type OverviewLayoutMode = "equal" | "duration";

/**
 * Lay out overview bars for all lanes together.
 * - equal: global chronological order, each event gets the same visual width
 * - duration: each bar uses its true start/end on the time axis
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

  if (mode === "duration") {
    return sorted.map((span) => {
      const startMs = span.startMs;
      const endMs = Math.max(span.endMs, startMs + 1);
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
  error: boolean;
}

function llmPreview(ev: CanonicalTraceEvent): string {
  const parsed = parseLlmMessageContent(ev.raw.messages ?? ev.raw.text ?? ev.raw.content);
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
        continue;
      }
      if (name && e.tool?.name === name) return { end: e, index: j };
    }
    return null;
  };

  for (let i = 0; i < events.length; i++) {
    const ev = events[i];
    if (used.has(ev.id)) continue;
    if (isAgentPhaseMarker(ev)) {
      used.add(ev.id);
      continue;
    }

    if (isLlmStart(ev)) {
      const matched = findLlmEnd(i + 1, ev.phase);
      const end = matched?.end ?? null;
      if (end) used.add(end.id);
      used.add(ev.id);
      const startMs = parseTs(ev.timestamp);
      const endMs = parseTs(end?.timestamp);
      const inputPreview = parseLlmMessageContent(
        ev.raw.messages ?? ev.raw.text ?? ev.raw.content,
      ).text;
      const finish =
        end?.raw.generation_info &&
        typeof end.raw.generation_info === "object" &&
        !Array.isArray(end.raw.generation_info)
          ? (end.raw.generation_info as Record<string, unknown>).finish_reason
          : null;
      let summary = inputPreview.trim()
        ? inputPreview.slice(0, 160)
        : end
          ? llmPreview(end)
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
        error: end?.event === "llm_end_error",
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
        summary: llmPreview(ev),
        timestamp: ev.timestamp ?? null,
        endTimestamp: ev.timestamp ?? null,
        durationMs: null,
        phase: ev.phase ?? null,
        event: ev.event,
        kind: "llm",
        start: ev,
        end: null,
        error: ev.event === "llm_end_error",
      });
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
        title: ev.tool?.name || "tool",
        summary: end
          ? String(
              typeof end.tool?.output === "string"
                ? end.tool.output
                : end.summary || end.title,
            ).slice(0, 160)
          : ev.summary || "in progress",
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
        summary: ev.summary || ev.title,
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
    const loggedDuration =
      typeof ev.duration_ms === "number" && Number.isFinite(ev.duration_ms)
        ? ev.duration_ms
        : null;
    const rawDuration = (() => {
      const raw = ev.raw?.duration_ms;
      if (typeof raw === "number" && Number.isFinite(raw)) return raw;
      const data = ev.raw?.data;
      if (data && typeof data === "object" && !Array.isArray(data)) {
        const nested = (data as Record<string, unknown>).duration_ms;
        if (typeof nested === "number" && Number.isFinite(nested)) return nested;
      }
      return null;
    })();
    out.push({
      id: ev.id,
      role,
      title: ev.title,
      summary: ev.summary,
      timestamp: ev.timestamp ?? null,
      endTimestamp: loggedDuration != null || rawDuration != null ? ev.timestamp ?? null : null,
      durationMs: loggedDuration ?? rawDuration,
      phase: ev.phase ?? null,
      event: ev.event ?? null,
      kind: ev.kind,
      start: ev,
      end: null,
      error: false,
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
        const body = typeof b.text === "string" ? b.text : typeof b.content === "string" ? b.content : "";
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
  const parsed = parseLlmMessageContent(start.raw.messages ?? start.raw.text ?? start.raw.content);
  const endParsed = parseLlmMessageContent(
    end?.raw.text ?? end?.raw.content ?? end?.raw.messages ?? end?.raw.report,
  );

  const thinking =
    parsed.thinking ||
    endParsed.thinking ||
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
      if (next.role !== "tool") continue;
      tools.push({
        rowId: next.id,
        name: next.start.tool?.name || next.title || "tool",
        callId: next.start.tool?.tool_call_id || next.end?.tool?.tool_call_id || null,
        input: next.start.tool?.input ?? next.start.raw.input,
      });
    }
  }

  return {
    input: parsed.text,
    thinking,
    outputText: endParsed.text,
    finishReason,
    model,
    tools,
  };
}
